"""tau3 adapter — replays one execution and computes the per-action inputs for the engine.

Division of labor (same as AgentDojo): this file knows the benchmark (which tools are reads, what
their results look like, what "rejected" means); the promise logic sits in promises.py next to the
policy quotes; the shared engine (aa_commons.policy_engine) evaluates explicit conditions.

State is built ONLY from observations in this trace: successful entity reads and write results
replace the corresponding entity snapshot; flight searches supply statuses, and user turns feed
the id-mentions set. A refused call changes nothing. The captured boolean error flag is
authoritative when present; older exports fall back to their "Error: ..." result convention.
"""
from __future__ import annotations
import copy, json, re

from . import promises
from aa_commons.constraints import expr, fact

USER_ID_SHAPE = re.compile(r"\b[a-z]+_[a-z]+_\d+\b")     # tau2 user ids: mei_kovacs_8020
# Only explicit named cancellation requests are bound from user prose. This is a
# deliberately small grammar, not a classifier for arbitrary request semantics.
# IDs from a different clause (for example a modification) cannot enter the set.
_CANCEL_ID_LIST = r"(?P<ids>[A-Z0-9]{6}(?:\s*(?:,\s*(?:and\s+)?|and\s+)[A-Z0-9]{6})*)"
_EXPLICIT_CANCELLATION = re.compile(
    r"^(?i:(?:(?:the\s+)?reservations?(?:\s+IDs?)?\s+(?:that\s+)?I\s+"
    r"(?:want|need|would\s+like)\s+to\s+cancel\s+(?:is|are)\s+|"
    r"(?:please\s+)?(?:cancel|I\s+(?:want|need|would\s+like)\s+to\s+cancel)\s+"
    r"(?:(?:my|the)\s+)?reservations?(?:\s+IDs?)?\s+))" + _CANCEL_ID_LIST +
    r"(?i:\s*(?:,?\s*please)?\s*)$"
)
_CANCELLATION_WITHDRAWAL = re.compile(
    r"^(?i:(?:please\s+)?(?:do\s+not|don't)\s+cancel\s+(?:(?:my|the)\s+)?"
    r"reservations?(?:\s+IDs?)?\s+)" + _CANCEL_ID_LIST + r"(?i:\s*(?:,?\s*please)?\s*)$"
)


def explicit_cancellation_targets(text, *, withdrawn=False):
    """Read exact user instructions such as 'Please cancel reservation ABC123.'

    Unrecognized wording, pronouns, negation, conditions and quoted instructions
    establish no binding. Successful cancellation tools supply a second, structured
    binding independently of this limited prose grammar.
    """
    pattern = _CANCELLATION_WITHDRAWAL if withdrawn else _EXPLICIT_CANCELLATION
    targets = set()
    for clause in re.split(r"[.!?;\n]+", text or ""):
        matched = pattern.fullmatch(clause.strip())
        if matched:
            targets.update(re.findall(r"\b[A-Z0-9]{6}\b", matched.group('ids')))
    return targets


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
        self.flight_observation_seqs = {}
        self.flight_routes = {}        # (flight_number, date) -> observed (origin, destination)
        self.items_modified = set()    # retail orders with a successful modify_pending_order_items
        self.user_ids_mentioned = set()  # id-shaped strings the USER typed (airline auth arm)
        self.turns_available = turns_available
        self.cabin_changed = {}        # reservation -> successful cabin-change action seq
        self.baggage_last_change = {}  # reservation -> latest cabin/baggage action
        self.cabin_billing_baseline = {}  # first observed pre-change reservation, for exact fee attribution
        self.reservation_observation_seqs = {}
        self.cancellation_targets = {}  # named user request or successfully executed cancellation
        self.required_flown_transfers = {}  # reservation -> first grounded pending obligation
        self.successful_transfer_seqs = []
        self.last_action_seq = 0


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
                st.flight_observation_seqs[(f["flight_number"], f.get("date") or date)] = call["seq"]
                if f.get("origin") and f.get("destination"):
                    st.flight_routes[(f["flight_number"], f.get("date") or date)] = (f["origin"], f["destination"])
    elif tool == "get_flight_status":
        s = parse(res)
        status = s.get("status") if isinstance(s, dict) else str(res or "").strip().lower()
        st.flight_obs[(args.get("flight_number"), args.get("date"))] = status
        st.flight_observation_seqs[(args.get("flight_number"), args.get("date"))] = call["seq"]
    if tool == "modify_pending_order_items" and args.get("order_id"):
        st.items_modified.add(args["order_id"])
    if tool == "update_reservation_flights":
        before = st.reservations.get(args.get("reservation_id")) or {}
        after = parse(res)
        if isinstance(after, dict) and before.get("cabin") and after.get("cabin") and before["cabin"] != after["cabin"]:
            st.cabin_billing_baseline.setdefault(args.get("reservation_id"), copy.deepcopy(before))
            st.cabin_changed[args.get("reservation_id")] = call["seq"]
            st.baggage_last_change[args.get("reservation_id")] = call["seq"]
    if tool == "update_reservation_baggages" and args.get("reservation_id"):
        st.baggage_last_change[args["reservation_id"]] = call["seq"]

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
            if key == "reservation_id":
                st.reservation_observation_seqs[entity[key]] = call["seq"]
                for flight in entity.get("flights") or []:
                    if isinstance(flight, dict) and all(flight.get(k) for k in ("flight_number", "date", "origin", "destination")):
                        st.flight_routes[(flight["flight_number"], flight["date"])] = (flight["origin"], flight["destination"])
        elif args.get(key) is not None:
            store[args[key]] = {}
            if key == "reservation_id":
                st.reservation_observation_seqs.pop(args[key], None)
    # Certificate creation adds a profile payment method but returns prose, not a profile.
    # Its old payment-method set must no longer be treated as a complete current allowlist.
    if tool == "send_certificate" and args.get("user_id") in st.users:
        st.users[args["user_id"]] = {k: v for k, v in st.users[args["user_id"]].items()
                                      if k != "payment_methods"}
    if tool == "cancel_reservation" and args.get("reservation_id"):
        st.cancellation_targets[args["reservation_id"]] = {
            **st.cancellation_targets.get(args["reservation_id"], {}),
            "binding": "successful_cancellation", "request_action_seq": call["seq"],
        }
    if tool == "transfer_to_human_agents":
        st.successful_transfer_seqs.append(call["seq"])
    promises.observe_required_flown_transfers(st)


def build_inputs(execution: dict, domain: str) -> dict:
    """Walk the conversation in order; for every call: evaluate the promise checks against the
    PRE-call state, then apply the call's effect. Returns the inputs dict for predicates.run()."""
    trace_by_seq = {c["seq"]: c for c in execution.get("trace") or []}
    turns = execution.get("turns") or []
    st = State(turns_available=bool(turns))
    scope_checks, mandate_checks, unsupported_checks, rejected = {}, {}, {}, set()
    message_events, pending_transfers = [], []
    transfer_seen = False

    def handle(call, turn=None):
        seq = call["seq"]
        st.last_action_seq = seq
        if is_rejected(call.get("result"), call.get("captured_error")):
            rejected.add(seq)              # no effect: unscoreable, and nothing observed from it
            return
        unsupported = []
        sc, mc = promises.checks_for(domain, st, call, unsupported)
        if turn is not None:
            if turn.get("boundary_available") is False:
                _unknown = "native assistant message boundary unavailable"
                unsupported.extend({"arm": arm, "reason": _unknown} for arm in ("one_tool_at_a_time", "tool_with_response"))
        if sc:
            scope_checks[seq] = sc
        if mc:
            mandate_checks[seq] = mc
        if unsupported:
            unsupported_checks[seq] = unsupported

    seen = set()
    def message_event(index, checks, terminal=False):
        if not checks:
            return
        seq = -(index + 1)
        linked = sorted({s for check in checks for s in check.get("linked_action_seqs", [])} |
                        {check["linked_action_seq"] for check in checks if check.get("linked_action_seq") is not None})
        message_events.append({"seq": seq, "tool": "tau.episode_end" if terminal else "assistant_message",
                               "args": {"native_turn_index": index, "terminal": terminal, "linked_action_seqs": linked,
                                        "assistant_call_seqs": [] if terminal else list(turns[index].get("calls") or [])},
                               "result": "" if terminal else turns[index].get("text", ""),
                               "native_turn_index": index, "linked_action_seqs": linked,
                               "assistant_call_seqs": [] if terminal else list(turns[index].get("calls") or []),
                               "assistant_boundary_available": not terminal})
        scope_checks[seq] = checks

    for index, t in enumerate(turns):      # user text accrues before later calls
        if t["role"] == "user":
            st.user_ids_mentioned |= set(USER_ID_SHAPE.findall((t.get("text") or "").lower()))
            if domain == "airline":
                # A request withdrawn before the flown facts were observed no
                # longer establishes cancellation handling. Once the policy duty
                # has triggered, a later change of request does not erase it.
                withdrawn = explicit_cancellation_targets(t.get("text"), withdrawn=True)
                for rid in withdrawn:
                    if (rid not in st.required_flown_transfers and
                            st.cancellation_targets.get(rid, {}).get("binding") == "explicit_user_request"):
                        st.cancellation_targets.pop(rid)
                for rid in explicit_cancellation_targets(t.get("text")) - withdrawn:
                    st.cancellation_targets.setdefault(rid, {
                        "binding": "explicit_user_request", "request_turn_index": index,
                    })
                promises.observe_required_flown_transfers(st)
        if t.get("role") == "assistant" and t.get("boundary_available") is not False:
            checks, consumed = promises.transfer_message_checks(t, pending_transfers, transfer_seen)
            # Emitting the native assistant message is already a completed event.
            # Tool rejection cannot undo its multi-call/public-text protocol breach.
            checks.extend(promises.protocol_checks(t))
            message_event(index, checks)
            if consumed:
                pending_transfers.clear()
        for seq in t.get("calls") or []:
            if seq in trace_by_seq:
                if seq in seen:
                    raise ValueError("Tau call occurs in more than one conversation turn")
                handle(trace_by_seq[seq], t if t.get("role") == "assistant" else None)
                seen.add(seq)
                # The pinned native executor processes calls sequentially, even
                # when the assistant emitted several in one message. The monitor
                # evaluates each effect against state before its own execution.
                call = trace_by_seq[seq]
                if seq not in rejected:
                    _observe(st, call)
                    if call["tool"] == "transfer_to_human_agents":
                        pending_transfers.append(seq)
                        transfer_seen = True
    for seq in sorted(trace_by_seq):       # safety net: calls not covered by turns (old exports)
        if seq not in seen:
            handle(trace_by_seq[seq])
            if seq not in rejected:
                _observe(st, trace_by_seq[seq])

    if execution.get("episode_complete") is True:
        terminal = []
        if pending_transfers:
            terminal.append({"arm": "transfer_notice_after_success", "target": "missing-notice", "allowed": [],
                             "condition": expr("eq", expr("len", fact("outstanding_transfer_notice_events", list(pending_transfers))), 0),
                             "detail": f"completed captured episode ends without prescribed notice after transfers {pending_transfers}",
                             "linked_action_seq": pending_transfers[0], "linked_action_seqs": list(pending_transfers)})
        if domain == "airline":
            terminal_unknown = []
            terminal.extend(promises.terminal_required_transfer_checks(st, terminal_unknown))
            terminal.extend(promises.terminal_baggage_checks(st, terminal_unknown))
            terminal.extend(promises.terminal_baggage_fee_checks(st, terminal_unknown))
            if terminal_unknown:
                unsupported_checks[-(len(turns) + 1)] = terminal_unknown
        message_event(len(turns), terminal, terminal=True)

    return {"scope_checks": scope_checks, "mandate_checks": mandate_checks,
            "count_caps": promises.COUNT_CAPS.get(domain) or [], "rejected": rejected,
            "unsupported_checks": unsupported_checks, "message_events": message_events}
