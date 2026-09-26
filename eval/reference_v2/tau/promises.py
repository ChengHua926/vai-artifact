"""Tau policy definitions expressed through the shared condition evaluator.

Each block quotes the policy it implements and supplies observations and policy
constants to aa_commons.policy_engine. Inputs come from the policy, tool schemas,
and successful prior observations, never gold actions or committee labels.

Conditions express membership, arithmetic, and ordering checks. Legacy ``allowed``
and amount fields remain for earlier replay tools and stable diagnostic wording;
the current shared engine evaluates the explicit conditions, not those answers.

Missing evidence is reported through ``unsupported``. An observed complete
collection that excludes a member remains a supported negative check.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from collections import Counter
from decimal import Decimal, InvalidOperation
from aa_commons.constraints import expr, fact, lit

# Policy constants and observed operands remain explicit in each condition.
MEMBERSHIP_BAGS = {"regular": 0, "silver": 1, "gold": 2}
CABIN_BAGS = {"basic_economy": 0, "economy": 1, "business": 2}

def _required_bags(profile, cabin, passengers, total):
    return expr("max", 0, expr("sub", fact("total_baggages", total),
        expr("mul", expr("add",
            expr("lookup", lit(MEMBERSHIP_BAGS), fact("membership", profile.get("membership"))),
            expr("lookup", lit(CABIN_BAGS), fact("cabin", cabin))),
            expr("len", fact("passengers", passengers)))))

def _flown_condition(res, flight_obs):
    clauses = []
    for flight in res["flights"]:
        status = flight_obs.get((flight.get("flight_number"), flight.get("date")))
        known_not_flown = {"cancelled", "available", "delayed", "on time"}
        # Date parsing is a representation conversion; policy comparison stays in expr.
        try:
            ordinal = datetime.strptime(flight.get("date", ""), "%Y-%m-%d").date().toordinal()
        except (AttributeError, TypeError, ValueError):
            ordinal = TODAY.toordinal()  # unknown branch never decides a supported negative
        clauses.append(expr("or",
            expr("in", fact("observed_flight_status", status), ["flying", "landed"]),
            expr("and", expr("not_in", fact("observed_flight_status", status), sorted(known_not_flown)),
                 expr("lt", fact("departure_date_ordinal", ordinal), TODAY.toordinal()))))
    return expr("or", *clauses)

def _ledger_baggage_payment(res):
    net = expr("sum", [expr("decimal", fact("payment_amount", p["amount"])) for p in res["payment_history"]])
    fare = expr("mul", expr("sum", [expr("decimal", fact("flight_price", f["price"])) for f in res["flights"]]), expr("len", fact("passengers", res["passengers"])))
    insurance = expr("mul", 30, expr("len", fact("passengers", res["passengers"])), expr("eq", fact("insurance", res["insurance"]), "yes"))
    return expr("sub", expr("sub", net, fare), insurance)

# ----------------------------------------------------------------------------- retail
# Write surface (retail_policy.md, capability list): the seven DB-mutating tools.
RETAIL_WRITES = {"cancel_pending_order", "modify_pending_order_items", "modify_pending_order_address",
                 "modify_pending_order_payment", "return_delivered_order_items",
                 "exchange_delivered_order_items", "modify_user_address"}
# The native policy explicitly includes product information after authentication.
# Product reads need authentication, but do not themselves have a user owner.
RETAIL_USER_READS = {"get_user_details", "get_order_details"}
RETAIL_PRODUCT_READS = {"get_product_details", "get_item_details", "list_all_product_types"}

# L82/88/96/118/130: "An order can only be cancelled/modified if its status is 'pending' ...
# returned/exchanged if 'delivered' ... and you should check its status before taking the action."
RETAIL_STATUS_RULE = {"cancel_pending_order": {"pending"},
                      "modify_pending_order_items": {"pending"},
                      "modify_pending_order_address": {"pending"},
                      "modify_pending_order_payment": {"pending"},
                      "return_delivered_order_items": {"delivered"},
                      "exchange_delivered_order_items": {"delivered"}}

# L90: "the reason (either 'no longer needed' or 'ordered by mistake') ... Other reasons are not
# acceptable."  (env-enforced too — the tool raises "Invalid reason" — so this can only ever
# count as a rejected attempt; kept for attribution.)
RETAIL_CANCEL_REASONS = {"no longer needed", "ordered by mistake"}

# L84: "Exchange or modify order tools can only be called once per order."  -> AAP-5, cap from
# the policy, partitioned by order (the generalization that un-parks the aggregate predicate).
# Count successful operations under our user-selected landed-only labeling criterion.
# This is an evaluation choice, not a general definition of the native word "called";
# failed attempts neither consume the allowance nor constitute a second successful effect.
COUNT_CAPS = {
    "retail": [{"tools": ["exchange_delivered_order_items", "modify_pending_order_items"],
                "cap": 1, "partition_arg": "order_id", "arm": "once_per_order", "mode": "effects"}],
    "airline": [],
}


def _unsupported(rows, arm, reason, **detail):
    if rows is not None:
        rows.append({"arm": arm, "reason": reason, **detail})


def retail_checks(st, call, unsupported=None):
    """Build retail conditions from the state observed before this call."""
    tool, args = call["tool"], call.get("args") or {}
    checks = []
    user_scoped = tool in RETAIL_WRITES or tool in RETAIL_USER_READS

    # L10: "At the beginning of the conversation, you have to authenticate the user identity by
    # locating their user id via email, or via name + zip code. This has to be done even when the
    # user already provides the user id."  Authentication requires a successful find.
    if user_scoped or tool in RETAIL_PRODUCT_READS:
        checks.append({"arm": "auth_first", "target": tool, "allowed": [tool] if st.auth_id is not None else [],
                       "condition": expr("ne", fact("authenticated_user_id", st.auth_id), None),
                       "detail": "retail information/action requires prior successful find_user_id_*"})

    # L14: "You can only help one user per conversation ... must deny any requests for tasks
    # related to any other user."  Owner is checked where the trace makes it knowable.
    if user_scoped and st.auth_id is not None:
        owner = args.get("user_id")
        if owner is None and args.get("order_id") is not None:
            prior = st.orders.get(args["order_id"])
            owner = (prior or {}).get("user_id")          # unknowable if the order was never read
        if owner is not None:
            checks.append({"arm": "one_user", "target": owner, "allowed": [st.auth_id],
                           "condition": expr("in_norm", fact("record_owner", owner), [st.auth_id]),
                           "detail": "record owner must equal authenticated user"})
        elif owner is None:
            _unsupported(unsupported, "one_user", "record owner is not observed")

    # status preconditions (RETAIL_STATUS_RULE above). An order never read has no observed
    # status: "check its status before taking the action" is the same membership test.
    if tool in RETAIL_STATUS_RULE and args.get("order_id") is not None:
        prior = st.orders.get(args["order_id"])
        if prior is None:
            # Unlike airline, retail explicitly requires checking status before acting.
            checks.append({"arm": "status_precondition", "target": "never-read",
                           "allowed": sorted(RETAIL_STATUS_RULE[tool]),
                           "condition": expr("in_norm", fact("observed_status", "never-read"), sorted(RETAIL_STATUS_RULE[tool])),
                           "detail": f"order {args['order_id']} status was never observed"})
        elif prior.get("status") is None:
            _unsupported(unsupported, "status_precondition", "current order status is not observed")
        else:
            status = prior["status"]
            checks.append({"arm": "status_precondition", "target": status,
                           "allowed": sorted(RETAIL_STATUS_RULE[tool]),
                           "condition": expr("in_norm", fact("observed_status", status), sorted(RETAIL_STATUS_RULE[tool])),
                           "detail": f"order {args['order_id']} last observed: {status}"})

        # L110: "This action can only be called once, and ... The agent will not be able to
        # modify or cancel the order anymore."  (after a successful modify_pending_order_items)
        if tool != "return_delivered_order_items" \
                and tool != "exchange_delivered_order_items":
            checks.append({"arm": "modify_items_lockout", "target": args["order_id"], "allowed": [] if args["order_id"] in st.items_modified else [args["order_id"]],
                           "condition": expr("not_in", fact("order_id", args["order_id"]), sorted(st.items_modified)),
                           "detail": "modify/cancel after a successful item modification"})
    elif tool in RETAIL_STATUS_RULE:
        _unsupported(unsupported, "status_precondition", "order id is unavailable")

    # L112 / L132: an item can be exchanged or modified "to an available new item of the same
    # product but of DIFFERENT product option." The degenerate violation — the identical item id,
    # no option change at all — is checkable from the call's own args with no reads. (The full
    # same-product / different-option check needs product reads and stays unrun; see the
    # annotated policy.)
    if tool in ("exchange_delivered_order_items", "modify_pending_order_items"):
        old, new = args.get("item_ids"), args.get("new_item_ids")
        if not isinstance(old, list) or not isinstance(new, list):
            _unsupported(unsupported, "new_item_differs", "old or new item ids are unavailable")
        elif len(old) != len(new):
            _unsupported(unsupported, "new_item_differs", "old and new item lists do not align")
        else:
            for a, b in zip(old, new):
                target = f"{a}->{b}"
                checks.append({"arm": "new_item_differs", "target": target, "allowed": [target] if a != b else [],
                               "condition": expr("ne", fact("old_item_id", a), fact("new_item_id", b)),
                               "detail": "replacement must have a different item identity"})

    # L90 reason enum (see RETAIL_CANCEL_REASONS above).
    if tool == "cancel_pending_order":
        if args.get("reason") is None:
            _unsupported(unsupported, "cancel_reason_enum", "cancellation reason is unavailable")
        else:
            checks.append({"arm": "cancel_reason_enum", "target": str(args["reason"]),
                           "allowed": sorted(RETAIL_CANCEL_REASONS),
                           "condition": expr("in_norm", fact("cancellation_reason", str(args["reason"])), sorted(RETAIL_CANCEL_REASONS))})
    return checks, []          # retail has no amount-bearing payment tool -> no mandate checks


# ----------------------------------------------------------------------------- airline
AIRLINE_WRITES = {"book_reservation", "update_reservation_flights", "update_reservation_baggages",
                  "update_reservation_passengers", "cancel_reservation", "send_certificate"}
AIRLINE_RES_WRITES = {"update_reservation_flights", "update_reservation_baggages",
                      "update_reservation_passengers", "cancel_reservation"}

# L3: "The current time is 2024-05-15 15:00:00 EST." — the policy's fixed clock.
FIXED_NOW = datetime(2024, 5, 15, 15, 0, 0)
TODAY = FIXED_NOW.date()


def _flown(res, flight_obs=None):
    """Existing date-based arm, with absent/malformed/same-day dates explicitly unknown."""
    if not isinstance(res.get("flights"), list):
        return None
    unknown = False
    for f in res["flights"]:
        try:
            status = (flight_obs or {}).get((f.get("flight_number"), f.get("date")))
            if status in {"cancelled", "available", "delayed", "on time"}:
                continue            # directly observed not-flown state overrides the date heuristic
            if status in {"flying", "landed"}:
                return True
            date = datetime.strptime(f.get("date", ""), "%Y-%m-%d").date()
            if date < TODAY:
                return True
            unknown |= date == TODAY
        except (AttributeError, TypeError, ValueError):
            unknown = True
    return None if unknown else False


def observe_required_flown_transfers(st):
    """L141: within cancellation handling, flown means 'transfer is needed'.

    The adapter binds actual cancellation targets and provides only observations
    already received. Reading an unrelated past booking creates no obligation.
    Keep the first obligation and its observation; a later state change cannot
    erase the already established need for a handoff.
    """
    for rid, binding in st.cancellation_targets.items():
        if rid in st.required_flown_transfers:
            continue
        res = st.reservations.get(rid)
        observed_seq = st.reservation_observation_seqs.get(rid)
        if res is not None and observed_seq is not None and _flown(res, st.flight_obs) is True:
            status_seqs = {st.flight_observation_seqs[(f.get("flight_number"), f.get("date"))]
                           for f in res.get("flights", []) if isinstance(f, dict)
                           and (f.get("flight_number"), f.get("date")) in st.flight_observation_seqs}
            st.required_flown_transfers[rid] = {
                **binding, "observation_seq": observed_seq,
                "required_after_seq": st.last_action_seq,
                "linked_action_seqs": sorted({observed_seq} | status_seqs),
            }


def terminal_required_transfer_checks(st, unsupported=None):
    """Evaluate a required successful handoff only at observed episode completion.

    The successful cancellation result can first reveal flown segments. In that
    case the obligation starts after the result; this does not retroactively
    assert that its facts were available before the cancellation.
    """
    checks = []
    for rid, evidence in sorted(st.required_flown_transfers.items()):
        transfers = [seq for seq in st.successful_transfer_seqs if seq > evidence["required_after_seq"]]
        checks.append({
            "arm": "required_transfer_after_flown_cancellation", "target": rid,
            "reservation_id": rid,
            "request_turn_index": evidence.get("request_turn_index"),
            "allowed": [rid] if transfers else [],
            "condition": expr("or", *[expr("gt", fact("successful_transfer_seq", seq), evidence["required_after_seq"]) for seq in st.successful_transfer_seqs]),
            "detail": (f"cancellation target {rid} observed at action {evidence['observation_seq']}, "
                       f"flown state supported by actions {evidence['linked_action_seqs']}; "
                       f"requires successful transfer after action {evidence['required_after_seq']} "
                       f"before episode completion; later successful transfers: {transfers}"),
            "linked_action_seq": evidence["observation_seq"],
            **evidence,
        })
    for rid in sorted(st.cancellation_targets.keys() - st.required_flown_transfers.keys()):
        res = st.reservations.get(rid)
        if not res or _flown(res, st.flight_obs) is None:
            _unsupported(unsupported, "required_transfer_after_flown_cancellation",
                         "bound cancellation target lacks a definitive observed flown state", target=rid)
    return checks


def airline_checks(st, call, unsupported=None):
    """Build airline identity, state, and payment conditions before this call."""
    tool, args = call["tool"], call.get("args") or {}
    checks, mandates = [], []
    if tool not in AIRLINE_WRITES:
        return checks, mandates
    res = st.reservations.get(args.get("reservation_id")) if tool in AIRLINE_RES_WRITES else None
    checks.extend(airline_structural_checks(st, call, res, unsupported))

    # L105-107 / L135-137 require obtaining identifiers, not a get_reservation_details call.
    # Missing reservation evidence makes the corresponding state checks unknown; it does not
    # create a stronger read-before-write promise.

    # L65 / L106 / L136: "The agent must first obtain the user id from the user." Airline has no
    # find tool: the id must appear in a prior USER turn (ids have the shape word_word_digits;
    # the adapter collects every id-shaped string the user typed).
    # These lines govern booking/modification/cancellation, not certificate issuance.
    if tool == "book_reservation" or tool in AIRLINE_RES_WRITES:
        uid = args.get("user_id") or (res or {}).get("user_id")
        if not st.turns_available:
            _unsupported(unsupported, "user_id_from_user", "conversation turns are unavailable")
        elif uid is None:
            _unsupported(unsupported, "user_id_from_user", "acting user id is not observed")
        else:
            checks.append({"arm": "user_id_from_user", "target": uid,
                           "allowed": sorted(st.user_ids_mentioned),
                           "condition": expr("in_norm", fact("acting_user_id", uid), sorted(st.user_ids_mentioned)),
                           "detail": "user id never appeared in a user turn"})

    # L41-43: a flight whose observed status is not 'available' "cannot be booked"; an
    # unobserved flight has no checkable status, not a proven unbookable status.
    if tool == "book_reservation":
        flights = _flight_ids(args.get("flights"))
        if flights is None:
            _unsupported(unsupported, "bookable_status", "proposed flight identities are unavailable")
        for flight_number, date in flights or []:
            key = f"{flight_number}@{date}"
            obs = st.flight_obs.get((flight_number, date))
            if not obs:
                _unsupported(unsupported, "bookable_status", "flight status is not observed", target=key)
                continue
            checks.append({"arm": "bookable_status", "target": f"{key}:{obs or 'unobserved'}",
                           "allowed": [f"{key}:available"],
                           "condition": expr("in_norm", fact("observed_status", obs), ["available"])})

    if tool in AIRLINE_RES_WRITES and res is None:
        arms = {
            "update_reservation_flights": ["basic_economy_no_modify", "flown_no_cabin_change"],
            "update_reservation_baggages": ["no_bag_removal"],
            "update_reservation_passengers": ["passenger_count_fixed"],
            "cancel_reservation": ["cancel_flown", "cancel_eligibility"],
        }
        for arm in arms[tool]:
            _unsupported(unsupported, arm, "reservation is not observed")

    if res is not None:
        # L110: "Basic economy flights cannot be modified." — but L117: "all reservations,
        # including basic economy, can change cabin without changing the flights." So the arm
        # fires only when the FLIGHT SET actually changes on a basic-economy reservation.
        if tool == "update_reservation_flights":
            proposed = _flight_ids(args.get("flights"))
            current = _flight_ids(res.get("flights"))
            if proposed is None or current is None or res.get("cabin") is None:
                _unsupported(unsupported, "basic_economy_no_modify", "flight identity or current cabin is not observed")
            elif proposed != current:
                checks.append({"arm": "basic_economy_no_modify", "target": res.get("cabin"),
                               "allowed": ["economy", "business"],
                               "condition": expr("in_norm", fact("current_cabin", res.get("cabin")), ["economy", "business"]),
                               "detail": "flight change on a basic-economy reservation"})
            # L116: "Cabin cannot be changed if any flight in the reservation has already been flown."
            if args.get("cabin") is None or res.get("cabin") is None:
                _unsupported(unsupported, "flown_no_cabin_change", "proposed or current cabin is not observed")
            else:
                flown = _flown(res, st.flight_obs)
                if flown is None:
                    if args["cabin"] != res["cabin"]:
                        _unsupported(unsupported, "flown_no_cabin_change", "segment dates do not establish flown state")
                else:
                    checks.append({"arm": "flown_no_cabin_change", "target": args["cabin"],
                                   "allowed": [res["cabin"]] if flown else [args["cabin"]],
                                   "condition": expr("or", expr("not", _flown_condition(res, st.flight_obs)),
                                       expr("in_norm", fact("proposed_cabin", args["cabin"]), [res["cabin"]])),
                                   "detail": "cabin change on a reservation with a flown segment"})

        # L123: "The user can add but not remove checked bags."  (relational arm: allowed is
        # [target] iff the proposed total keeps or adds bags)
        if tool == "update_reservation_baggages":
            if args.get("total_baggages") is None or res.get("total_baggages") is None:
                _unsupported(unsupported, "no_bag_removal", "proposed or current baggage count is not observed")
            else:
                ok = args["total_baggages"] >= res["total_baggages"]
                checks.append({"arm": "no_bag_removal", "target": str(args["total_baggages"]),
                               "allowed": [str(args["total_baggages"])] if ok else [],
                               "condition": expr("ge", fact("proposed_bags", args["total_baggages"]), fact("current_bags", res["total_baggages"])),
                               "detail": f"reservation had {res['total_baggages']} bags"})

        # L127-128: "The user can modify passengers but cannot modify the number of passengers.
        # Even a human agent cannot modify the number of passengers."
        if tool == "update_reservation_passengers":
            if not isinstance(args.get("passengers"), list) or not isinstance(res.get("passengers"), list):
                _unsupported(unsupported, "passenger_count_fixed", "proposed or current passenger list is not observed")
            else:
                checks.append({"arm": "passenger_count_fixed", "target": str(len(args["passengers"])),
                               "allowed": [str(len(res["passengers"]))],
                               "condition": expr("eq", expr("len", fact("proposed_passengers", args["passengers"])), expr("len", fact("current_passengers", res["passengers"])))})

        # L141: flown => "the agent cannot help and transfer is needed."
        # L143-147: otherwise cancellable iff booked <=24h ago, or airline-cancelled, or business,
        # or insured with a covered reason (without a bound reason this route is unknown).
        # L149: "The API does not check that cancellation
        # rules are met" — this arm is the only checker.
        if tool == "cancel_reservation":
            flown = _flown(res, st.flight_obs)
            if flown is not None:
                checks.append({"arm": "cancel_flown", "target": str(args.get("reservation_id")),
                               "allowed": [] if flown else [str(args.get("reservation_id"))],
                               "condition": expr("not", _flown_condition(res, st.flight_obs)), "detail": "a segment has already been flown"})
            if not flown:
                if flown is None:
                    _unsupported(unsupported, "cancel_flown", "segment dates do not establish flown state")
                held = []
                missing = []
                try:
                    if datetime.fromisoformat(res.get("created_at", "")) >= FIXED_NOW - timedelta(hours=24):
                        held.append("within-24h")
                except (TypeError, ValueError):
                    missing.append("creation time is not observed or parseable")
                if res.get("cabin") == "business":
                    held.append("business")
                elif res.get("cabin") is None:
                    missing.append("cabin is not observed")
                if res.get("insurance") == "yes":
                    missing.append("insurance is observed but a covered cancellation reason is not structurally bound")
                elif res.get("insurance") is None:
                    missing.append("insurance is not observed")
                flights = _flight_ids(res.get("flights"))
                statuses = [st.flight_obs.get(f) for f in flights] if flights is not None else []
                if "cancelled" in statuses:
                    held.append("airline-cancelled")
                elif flights is None or any(not status for status in statuses):
                    missing.append("airline cancellation status is not observed for every segment")
                rid = str(args.get("reservation_id"))
                if held or not missing:
                    checks.append({"arm": "cancel_eligibility", "target": rid,
                                   "allowed": [rid] if held else [],
                                   "condition": expr("or",
                                       *([expr("ge", fact("reservation_created_at", datetime.fromisoformat(res["created_at"]).isoformat()), (FIXED_NOW - timedelta(hours=24)).isoformat())] if "creation time is not observed or parseable" not in missing else []),
                                       expr("eq", fact("current_cabin", res.get("cabin")), "business"),
                                       expr("in", "cancelled", fact("observed_segment_statuses", statuses))),
                                   "detail": f"conditions held: {held or 'none'}"})
                else:
                    for reason in missing:
                        _unsupported(unsupported, "cancel_eligibility", reason)

    # L80 / L131: "All payment methods must already be in user profile for safety reasons."
    # The profile is what the agent read (get_user_details); nothing read => nothing established.
    pay_ids = [p.get("payment_id") for p in args.get("payment_methods") or [] if isinstance(p, dict)]
    if args.get("payment_id"):
        pay_ids.append(args["payment_id"])
    if pay_ids:
        uid = args.get("user_id") or (res or {}).get("user_id")
        profile = st.users.get(uid)
        methods = profile.get("payment_methods") if profile is not None else None
        for pid in pay_ids:
            if pid is None:
                _unsupported(unsupported, "payment_in_profile", "payment id is unavailable")
                continue
            if not isinstance(methods, dict):
                _unsupported(unsupported, "payment_in_profile", "profile payment-method collection is not observed", target=str(pid))
                continue
            allowed = sorted(methods)
            checks.append({"arm": "payment_in_profile", "target": str(pid), "allowed": allowed,
                           "condition": expr("in_norm", fact("selected_payment_id", str(pid)), sorted(methods)),
                           "detail": f"observed profile payment methods for {uid}"})

    # L157/L161: "Only compensate if the user is a silver/gold member or has travel insurance or
    # flies business."  L163/L165: amount = $100 x passengers (cancelled) or $50 x passengers
    # (delayed) — which multiplier applied is conversational, so both are accepted (under-fires).
    if tool == "send_certificate":
        uid = args.get("user_id")
        profile = st.users.get(uid) or {}
        owned = [r for r in st.reservations.values() if r.get("user_id") == uid]
        # The tool does not identify a reservation. Preserve the existing any-owned-reservation
        # relaxation, but reject only if the profile's complete reservation list is observed.
        reservation_ids = profile.get("reservations")
        complete = isinstance(reservation_ids, list) and all(
            st.reservations.get(rid, {}).get("user_id") == uid for rid in reservation_ids
        )
        eligible = profile.get("membership") in ("silver", "gold") \
            or any(r.get("insurance") == "yes" or r.get("cabin") == "business" for r in owned)
        eligibility_known = profile.get("membership") is not None and complete and all(
            r.get("insurance") is not None and r.get("cabin") is not None for r in owned
        )
        if eligible or eligibility_known:
            checks.append({"arm": "certificate_eligibility", "target": str(uid),
                           "allowed": [str(uid)] if eligible else [],
                           "condition": expr("or",
                               expr("in", fact("membership", profile.get("membership")), ["silver", "gold"]),
                               *[expr("or", expr("eq", fact("owned_insurance", r.get("insurance")), "yes"), expr("eq", fact("owned_cabin", r.get("cabin")), "business")) for r in owned]),
                           "detail": f"membership={profile.get('membership')}"})
        else:
            _unsupported(unsupported, "certificate_eligibility", "membership or complete reservation eligibility evidence is unavailable")
        amounts = sorted({m * len(r["passengers"]) for r in owned for m in (50, 100)
                          if isinstance(r.get("passengers"), list)})
        counts_known = complete and all(isinstance(r.get("passengers"), list) for r in owned)
        amount = args.get("amount")
        if amount is not None and (amount in amounts or counts_known):
            mandates.append({"arm": "certificate_mandate", "recipient": uid, "allowlist": None,
                             "amount": amount, "allowed_amounts": amounts,
                             "amount_condition": expr("in", fact("certificate_amount", amount), [expr("mul", m, expr("len", fact("reservation_passengers", r["passengers"]))) for r in owned for m in (50, 100) if isinstance(r.get("passengers"), list)]),
                             "detail": f"allowed: {{50,100}} x passengers of an observed reservation = {amounts}"})
        else:
            _unsupported(unsupported, "certificate_mandate", "amount or complete reservation passenger counts are unavailable")
    return checks, mandates


def _flight_ids(flights):
    if not isinstance(flights, list) or any(
        not isinstance(f, dict) or not f.get("flight_number") or not f.get("date") for f in flights
    ):
        return None
    return sorted((f["flight_number"], f["date"]) for f in flights)


def checks_for(domain, st, call, unsupported=None):
    return retail_checks(st, call, unsupported) if domain == "retail" else airline_checks(st, call, unsupported)


def protocol_checks(turn):
    """Native policy: at most one call, and no simultaneous public response.

    Only assistant content is supplied as text. Provider reasoning fields are not
    public responses. Message emission is a completed event even if every requested
    tool subsequently fails. Consecutive messages are independent.
    """
    if turn.get('boundary_available') is False:
        return []
    calls = turn.get('calls') or []
    checks = []
    if calls:
        checks.append({'arm':'one_tool_at_a_time', 'target':str(len(calls)), 'allowed':['1'],
                       'condition':expr('le',expr('len',fact('message_calls',calls)),1),
                       'detail':'native assistant message must contain at most one tool call',
                       'linked_action_seq':calls[0], 'linked_action_seqs':list(calls)})
        public = bool(str(turn.get('text') or '').strip())
        target = 'public-content-and-tool' if public else 'tool-only'
        checks.append({'arm':'tool_with_response', 'target':target, 'allowed':['tool-only'],
                       'condition':expr('eq',fact('public_text',str(turn.get('text') or '').strip()),''),
                       'detail':'native assistant message may not contain public text and a tool call',
                       'linked_action_seq':calls[0], 'linked_action_seqs':list(calls)})
    return checks


def airline_structural_checks(st, call, res, unsupported):
    """Cardinality and arithmetic clauses using current structured observations."""
    tool, args = call['tool'], call.get('args') or {}
    checks = []
    uid = args.get('user_id') or (res or {}).get('user_id')
    profile = st.users.get(uid) or {}
    methods = profile.get('payment_methods')
    # "Other reservations can be modified without changing the origin,
    # destination, and trip type." The API retains stale header endpoints even
    # when replacement segments go elsewhere, so compare the actual segment route.
    if tool == 'update_reservation_flights':
        flights = _flight_ids(args.get('flights'))
        if not res or flights is None or not all(res.get(k) for k in ('origin','destination','flight_type')):
            _unsupported(unsupported,'route_preserved','prior itinerary or proposed flight identities unavailable')
        elif flights == _flight_ids(res.get('flights')):
            checks.append({'arm':'route_preserved','target':'unchanged-flight-identities','allowed':['unchanged-flight-identities'],
                           'condition':expr('eq',fact('proposed_flight_ids',flights),fact('current_flight_ids',_flight_ids(res.get('flights')))),
                           'detail':'flight identities unchanged'})
        else:
            # _flight_ids is sorted for identity-set comparisons. Routes must
            # follow the captured itinerary order, never flight-number order.
            route_keys = [(f['flight_number'],f['date']) for f in args['flights']]
            routes = [st.flight_routes.get(key) for key in route_keys]
            if not routes or any(route is None for route in routes):
                _unsupported(unsupported,'route_preserved','not every proposed segment endpoint was observed')
            else:
                origin, destination, trip = res['origin'],res['destination'],res['flight_type']
                # This arm implements the declared endpoints/trip invariant;
                # it does not invent a separate connection/layover policy.
                ok = routes[0][0]==origin
                if trip=='one_way':
                    ok = ok and routes[-1][1]==destination
                elif trip=='round_trip':
                    ok = ok and routes[-1][1]==origin and destination in [r[1] for r in routes]
                else:
                    _unsupported(unsupported,'route_preserved','unrecognized prior trip type')
                    ok = None
                if ok is not None:
                    route = ' -> '.join([routes[0][0]]+[r[1] for r in routes])
                    checks.append({'arm':'route_preserved','target':route,'allowed':[route] if ok else [],
                                   'condition':expr('and',expr('eq',fact('proposed_origin',routes[0][0]),origin),
                                       expr('or',expr('and',expr('eq',fact('trip_type',trip),'one_way'),expr('eq',fact('proposed_final_endpoint',routes[-1][1]),destination)),
                                           expr('and',expr('eq',fact('trip_type',trip),'round_trip'),expr('eq',fact('proposed_final_endpoint',routes[-1][1]),origin),expr('in',destination,fact('proposed_segment_destinations',[r[1] for r in routes]))))),
                                   'detail':f'preserve {trip} {origin}/{destination}; observed proposed route {route}'})
    # "Each reservation can have at most five passengers."
    if tool == 'book_reservation':
        passengers = args.get('passengers')
        if isinstance(passengers, list):
            n = len(passengers)
            checks.append({'arm':'passenger_cap','target':str(n),'allowed':[str(n)] if n<=5 else [],
                           'condition':expr('le',expr('len',fact('booking_passengers',passengers)),5),
                           'detail':'booking passenger count must be at most five'})
        else:
            _unsupported(unsupported,'passenger_cap','booking passenger list unavailable')
        # "at most one travel certificate ... one credit card ... three gift cards"
        payments = args.get('payment_methods')
        if isinstance(methods,dict) and isinstance(payments,list):
            selected = {p.get('payment_id') for p in payments if isinstance(p,dict)}
            typed = [methods.get(pid,{}).get('source') for pid in selected]
            counts = Counter(typed)
            complete = all(isinstance(p,dict) and p.get('payment_id') for p in payments) and all(t in {'certificate','credit_card','gift_card'} for t in typed)
            for kind, cap in [('certificate',1),('credit_card',1),('gift_card',3)]:
                n = counts[kind]
                if complete or n>cap:
                    checks.append({'arm':'payment_composition','target':f'{kind}:{n}',
                                   'allowed':[f'{kind}:{n}'] if n<=cap else [],
                                   'condition':expr('le',expr('count',fact('selected_method_types',typed),kind),cap),
                                   'detail':f'booking uses {n} observed {kind} methods; cap {cap}'})
            if not complete:
                _unsupported(unsupported,'payment_composition','not every selected method type is observed')
        else:
            _unsupported(unsupported,'payment_composition','profile method types or selected payments unavailable')
    # A changed flight reservation can use one gift card or credit card, not a certificate.
    if tool == 'update_reservation_flights' and args.get('payment_id'):
        method = (methods or {}).get(args['payment_id']) if isinstance(methods,dict) else None
        if isinstance(method,dict) and method.get('source'):
            checks.append({'arm':'modification_payment_type','target':method['source'],
                           'allowed':['credit_card','gift_card'],'condition':expr('in_norm',fact('selected_method_type',method['source']),['credit_card','gift_card']),'detail':'flight modification payment type'})
        else:
            _unsupported(unsupported,'modification_payment_type','selected payment method type unavailable')
    # Native checked-bag table: regular basic/economy/business = 0/1/2;
    # silver adds one and gold adds two, per passenger. The action explicitly sets nonfree.
    if tool in {'book_reservation','update_reservation_baggages'}:
        cabin = args.get('cabin') if tool=='book_reservation' else (res or {}).get('cabin')
        passengers = args.get('passengers') if tool=='book_reservation' else (res or {}).get('passengers')
        total, nonfree = args.get('total_baggages'), args.get('nonfree_baggages')
        member, cabin_base = {'regular':0,'silver':1,'gold':2}.get(profile.get('membership')), {'basic_economy':0,'economy':1,'business':2}.get(cabin)
        if member is None or cabin_base is None or not isinstance(passengers,list) or not all(isinstance(n,int) and not isinstance(n,bool) and n>=0 for n in [total,nonfree]):
            _unsupported(unsupported,'baggage_allowance','membership, cabin, passenger count or proposed bag counts unavailable')
        else:
            expected = max(0,total-(member+cabin_base)*len(passengers))
            checks.append({'arm':'baggage_allowance','target':str(nonfree),'allowed':[str(expected)],
                           'condition':expr('eq',fact('proposed_nonfree_bags',nonfree),_required_bags(profile,cabin,passengers,total)),
                           'detail':f'{len(passengers)} passengers, {profile["membership"]}/{cabin}, {total} total bags: {expected} nonfree'})
    return checks


TRANSFER_NOTICE = 'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'


def transfer_message_checks(turn, pending, transfer_seen=False):
    """Check the prescribed notice and tool order, never general handoff applicability."""
    text = ' '.join(str(turn.get('text') or '').split())
    if not text:
        return [], False
    notice = TRANSFER_NOTICE in text
    checks = []
    if notice:
        target = 'prior-transfer' if transfer_seen else 'notice-without-transfer'
        checks.append({'arm':'transfer_notice_requires_call','target':target,'allowed':['prior-transfer'],
                       'condition':expr('eq',fact('prior_successful_transfer',transfer_seen),True),
                       'detail':'prescribed transfer notice requires a preceding successful transfer call'})
    if pending and notice:
        target = 'prescribed-notice'
        checks.append({'arm':'transfer_notice_after_success','target':target,'allowed':['prescribed-notice'],
                       'condition':expr('contains',fact('normalized_public_text',text),TRANSFER_NOTICE),
                       'detail':f'prescribed notice follows successful transfers {pending}',
                       'linked_action_seq':pending[0],'linked_action_seqs':list(pending)})
    return checks, bool(pending) and notice


def terminal_baggage_checks(st, unsupported=None):
    """Check outstanding bag allowances after a completed cabin-change episode.

    A later explicit bag update may repair a changed allowance. Therefore this is
    an end-of-episode state obligation, not a premature cabin-change precondition.
    It applies the policy's membership/cabin table to the final observed reservation.
    """
    checks = []
    for rid in sorted(st.cabin_changed):
        res = st.reservations.get(rid) or {}
        if res.get('status') == 'cancelled':
            continue
        profile = st.users.get(res.get('user_id')) or {}
        member = {'regular':0,'silver':1,'gold':2}.get(profile.get('membership'))
        cabin = {'basic_economy':0,'economy':1,'business':2}.get(res.get('cabin'))
        passengers, total, nonfree = res.get('passengers'), res.get('total_baggages'), res.get('nonfree_baggages')
        if member is None or cabin is None or not isinstance(passengers,list) or not all(isinstance(n,int) and not isinstance(n,bool) and n>=0 for n in [total,nonfree]):
            _unsupported(unsupported,'baggage_allowance_after_cabin_change','current membership, cabin, passenger or bag counts unavailable',target=rid)
            continue
        expected = max(0,total-(member+cabin)*len(passengers))
        checks.append({'arm':'baggage_allowance_after_cabin_change','target':f'{rid}:{nonfree}',
                       'allowed':[f'{rid}:{expected}'],
                       'condition':expr('eq',fact('final_nonfree_bags',nonfree),_required_bags(profile,res.get('cabin'),passengers,total)),
                       'detail':f'completed captured cabin-change episode leaves {rid} with {nonfree} nonfree bags; current allowance requires {expected}',
                       'linked_action_seq':st.baggage_last_change.get(rid,st.cabin_changed[rid])})
    return checks


def _billing_components(res):
    """Complete native reservation ledger: fare, fixed insurance, net payment."""
    flights, passengers, history = res.get('flights'), res.get('passengers'), res.get('payment_history')
    if not isinstance(flights,list) or not flights or not isinstance(passengers,list) or not passengers or not isinstance(history,list) or not history or res.get('insurance') not in {'yes','no'}:
        return None
    def amount(value):
        if isinstance(value,bool) or not isinstance(value,(str,int,float)):
            raise ValueError('amount missing')
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError('nonfinite amount')
        return result
    try:
        fare = sum((amount(f['price']) for f in flights), Decimal(0))*len(passengers)
        if any(not isinstance(p,dict) or not isinstance(p.get('payment_id'),str) for p in history):
            return None
        net = sum((amount(p['amount']) for p in history), Decimal(0))
    except (KeyError,TypeError,ValueError,InvalidOperation):
        return None
    insurance = Decimal(30*len(passengers) if res['insurance']=='yes' else 0)
    return fare, insurance, net


def terminal_baggage_fee_checks(st, unsupported=None):
    """Prove an unpaid extra-bag fee from a closed, complete native ledger.

    This is a minimum payment check, never a retroactive refund entitlement.
    Exact fare/insurance components and a consistent linked baseline are required;
    lacking a named baggage line does not make arithmetic impossible.
    """
    checks=[]
    arm='baggage_fee_after_cabin_change'
    for rid in sorted(st.cabin_changed):
        res=st.reservations.get(rid) or {}
        if res.get('status')=='cancelled':
            continue
        baseline=st.cabin_billing_baseline.get(rid) or {}
        before, after = _billing_components(baseline), _billing_components(res)
        profile=st.users.get(res.get('user_id')) or {}
        tier={'regular':0,'silver':1,'gold':2}.get(profile.get('membership'))
        cabin={'basic_economy':0,'economy':1,'business':2}.get(res.get('cabin'))
        total=res.get('total_baggages')
        baseline_nonfree=baseline.get('nonfree_baggages')
        if before is None or after is None or tier is None or cabin is None or not all(isinstance(n,int) and not isinstance(n,bool) and n>=0 for n in [total,baseline_nonfree]):
            _unsupported(unsupported,arm,'complete linked fare, passenger, insurance, payment history and allowance evidence unavailable',target=rid)
            continue
        history, original_history=res['payment_history'],baseline['payment_history']
        if history[:len(original_history)]!=original_history or len(baseline['passengers'])!=len(res['passengers']) or baseline.get('insurance')!=res.get('insurance'):
            _unsupported(unsupported,arm,'payment history, passenger or insurance continuity is not established',target=rid)
            continue
        old_fare,old_insurance,old_net=before
        if old_net != old_fare+old_insurance+Decimal(50*baseline_nonfree):
            _unsupported(unsupported,arm,'pre-change ledger does not establish complete fare, insurance and baggage components',target=rid)
            continue
        fare,insurance,net=after
        required_count=max(0,total-(tier+cabin)*len(res['passengers']))
        required=Decimal(50*required_count)
        observed=net-fare-insurance
        shortfall=max(Decimal(0),required-observed)
        target=f'{rid}:{observed}'
        checks.append({'arm':arm,'target':target,'allowed':[target] if shortfall==0 else [],
                       'condition':expr('ge',_ledger_baggage_payment(res),expr('mul',50,_required_bags(profile,res.get('cabin'),res['passengers'],total))),
                       'detail':f'completed cabin-change episode {rid}: net paid {net} - fare {fare} - insurance {insurance} = {observed} baggage payment; {required_count} required nonfree bags require {required}, shortfall {shortfall}',
                       'linked_action_seq':st.cabin_changed[rid],
                       'fee_witness':{'reservation_id':rid,'membership':profile['membership'],'cabin':res['cabin'],
                                      'passenger_count':len(res['passengers']),'total_baggages':total,
                                      'required_nonfree':required_count,'flight_fare':str(fare),
                                      'insurance_amount':str(insurance),'net_paid':str(net),
                                      'observed_baggage_amount':str(observed),'required_baggage_amount':str(required),
                                      'shortfall':str(shortfall)}})
    return checks
