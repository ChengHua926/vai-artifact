"""tau3 adapter — replays one execution and computes the per-action inputs for the engine.

Division of labor (same as AgentDojo): this file knows the benchmark (which tools are reads, what
their results look like, what "rejected" means); the promise logic sits in promises.py next to the
policy quotes; the engine (eval/predicates.py) only runs membership/threshold tests.

State is built ONLY from what the agent itself did in this trace: successful reads populate the
entity store (last get_order_details per order, last get_reservation_details per reservation, last
get_user_details per user, flight-status observations from searches), user turns feed the
id-mentions set. A call the environment refused ("Error: ..." result) changes nothing — it is
recorded in `rejected` so the engine skips it (the effects-only rule).
"""
from __future__ import annotations
import json, re

from . import promises

USER_ID_SHAPE = re.compile(r"\b[a-z]+_[a-z]+_\d+\b")     # tau2 user ids: mei_kovacs_8020


def is_rejected(result) -> bool:
    return str(result or "").startswith("Error")


def parse(result):
    try:
        return json.loads(result)
    except (TypeError, ValueError):
        return None


class State:
    def __init__(self, turns_available: bool):
        self.auth_id = None            # retail: first successful find_user_id_* result
        self.orders = {}               # order_id -> last get_order_details result
        self.users = {}                # user_id -> last get_user_details result
        self.reservations = {}         # reservation_id -> last get_reservation_details result
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
    elif tool == "get_order_details":
        o = parse(res)
        if isinstance(o, dict) and o.get("order_id"):
            st.orders[o["order_id"]] = o
    elif tool == "get_user_details":
        u = parse(res)
        if isinstance(u, dict) and u.get("user_id"):
            st.users[u["user_id"]] = u
    elif tool == "get_reservation_details":
        r = parse(res)
        if isinstance(r, dict) and r.get("reservation_id"):
            st.reservations[r["reservation_id"]] = r
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
                st.flight_obs[(f["flight_number"], f.get("date") or date)] = f.get("status", "available")
    elif tool == "get_flight_status":
        s = parse(res)
        status = s.get("status") if isinstance(s, dict) else str(res or "").strip().lower()
        st.flight_obs[(args.get("flight_number"), args.get("date"))] = status
    elif tool == "modify_pending_order_items" and args.get("order_id"):
        st.items_modified.add(args["order_id"])


def build_inputs(execution: dict, domain: str) -> dict:
    """Walk the conversation in order; for every call: evaluate the promise checks against the
    PRE-call state, then apply the call's effect. Returns the inputs dict for predicates.run()."""
    trace_by_seq = {c["seq"]: c for c in execution.get("trace") or []}
    turns = execution.get("turns") or []
    st = State(turns_available=bool(turns))
    scope_checks, mandate_checks, rejected = {}, {}, set()

    def handle(call):
        seq = call["seq"]
        if is_rejected(call.get("result")):
            rejected.add(seq)              # no effect: unscoreable, and nothing observed from it
            return
        sc, mc = promises.checks_for(domain, st, call)
        if sc:
            scope_checks[seq] = sc
        if mc:
            mandate_checks[seq] = mc
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
            "count_caps": promises.COUNT_CAPS.get(domain) or [], "rejected": rejected}
