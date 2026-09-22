"""tau3 adapter — replays one execution and computes the per-action inputs for the engine.

Division of labor (same as AgentDojo): this file knows the benchmark (which tools are reads, what
their results look like, what "rejected" means); the promise logic sits in promises.py next to the
policy quotes; the engine (eval/predicates.py) only runs membership/threshold tests.

State is built ONLY from observations in this trace: successful entity reads and write results
replace the corresponding entity snapshot; flight searches supply statuses, and user turns feed
the id-mentions set. A refused call changes nothing. The captured boolean error flag is
authoritative when present; older exports fall back to their "Error: ..." result convention.
"""
from __future__ import annotations
import json, re

from . import promises

USER_ID_SHAPE = re.compile(r"\b[a-z]+_[a-z]+_\d+\b")     # tau2 user ids: mei_kovacs_8020


def is_rejected(result, captured_error=None) -> bool:
    if isinstance(captured_error, bool):
        return captured_error
    return str(result or "").startswith("Error")


def parse(result):
    if isinstance(result, (dict, list)):
        return result
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


class State:
    def __init__(self, turns_available: bool):
        self.auth_id = None            # retail: first successful find_user_id_* result
        self.orders = {}               # order_id -> latest successful entity result
        self.users = {}                # user_id -> latest successful entity result
        self.reservations = {}         # reservation_id -> latest successful entity result
        self.flight_obs = {}           # (flight_number, date) -> last observed status
        self.items_modified = set()    # retail orders with a successful modify_pending_order_items
        self.user_ids_mentioned = set()  # id-shaped strings the USER typed (airline auth arm)
        self.turns_available = turns_available


def _observe(st: State, call):
    """Apply one SUCCESSFUL call's effect on the observed-state store."""
    tool, args, res = call["tool"], call.get("args") or {}, call.get("result")
    if tool.startswith("find_user_id"):
        uid = str(res or "").strip()
        if uid and st.auth_id is None:
            st.auth_id = uid
    elif tool in ("search_direct_flight", "search_onestop_flight"):
        # direct results: [flight, ...] with date=null (the query's date applies);
        # onestop results: [[leg1, leg2], ...] — each leg carries its OWN date. Flatten one level.
        flights = parse(res)
        date = args.get("date")
        flat = []
        for item in flights if isinstance(flights, list) else []:
            flat.extend(item if isinstance(item, list) else [item])
        for f in flat:
            if isinstance(f, dict) and f.get("flight_number"):
                st.flight_obs[(f["flight_number"], f.get("date") or date)] = f.get("status")
    elif tool == "get_flight_status":
        s = parse(res)
        status = s.get("status") if isinstance(s, dict) else str(res or "").strip().lower()
        st.flight_obs[(args.get("flight_number"), args.get("date"))] = status
    if tool == "modify_pending_order_items" and args.get("order_id"):
        st.items_modified.add(args["order_id"])

    # Native write tools return the updated entity. Replace, never merge with stale fields,
    # and never reconstruct a postcondition from proposed args. An opaque successful result
    # invalidates that entity's old snapshot, since its current state is now unknown.
    entity_tools = (
        ("order_id", st.orders, {"get_order_details"} | (promises.RETAIL_WRITES - {"modify_user_address"})),
        ("user_id", st.users, {"get_user_details", "modify_user_address"}),
        ("reservation_id", st.reservations, {"get_reservation_details", "book_reservation"} | promises.AIRLINE_RES_WRITES),
    )
    for key, store, tools in entity_tools:
        if tool not in tools:
            continue
        entity = parse(res)
        if isinstance(entity, dict) and entity.get(key):
            store[entity[key]] = entity
        elif args.get(key) is not None:
            store[args[key]] = {}
    # Certificate creation adds a profile payment method but returns prose, not a profile.
    # Its old payment-method set must no longer be treated as a complete current allowlist.
    if tool == "send_certificate" and args.get("user_id") in st.users:
        st.users[args["user_id"]] = {k: v for k, v in st.users[args["user_id"]].items()
                                      if k != "payment_methods"}


def build_inputs(execution: dict, domain: str) -> dict:
    """Walk the conversation in order; for every call: evaluate the promise checks against the
    PRE-call state, then apply the call's effect. Returns the inputs dict for predicates.run()."""
    trace_by_seq = {c["seq"]: c for c in execution.get("trace") or []}
    turns = execution.get("turns") or []
    st = State(turns_available=bool(turns))
    scope_checks, mandate_checks, unsupported_checks, rejected = {}, {}, {}, set()

    def handle(call):
        seq = call["seq"]
        if is_rejected(call.get("result"), call.get("captured_error")):
            rejected.add(seq)              # no effect: unscoreable, and nothing observed from it
            return
        unsupported = []
        sc, mc = promises.checks_for(domain, st, call, unsupported)
        if sc:
            scope_checks[seq] = sc
        if mc:
            mandate_checks[seq] = mc
        if unsupported:
            unsupported_checks[seq] = unsupported
        _observe(st, call)

    seen = set()
    for t in turns:                        # conversation order: user text accrues before later calls
        if t["role"] == "user":
            st.user_ids_mentioned |= set(USER_ID_SHAPE.findall((t.get("text") or "").lower()))
        for seq in t.get("calls") or []:
            if seq in trace_by_seq:
                handle(trace_by_seq[seq])
                seen.add(seq)
    for seq in sorted(trace_by_seq):       # safety net: calls not covered by turns (old exports)
        if seq not in seen:
            handle(trace_by_seq[seq])

    return {"scope_checks": scope_checks, "mandate_checks": mandate_checks,
            "count_caps": promises.COUNT_CAPS.get(domain) or [], "rejected": rejected,
            "unsupported_checks": unsupported_checks}
