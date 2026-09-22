from __future__ import annotations

import json

import pytest

from eval import predicates
from eval.reference_v2.tau.adapter import build_inputs


UID = "alice_smith_1"
FLIGHTS = [{"flight_number": "A1", "date": "2024-05-20"}]


def reservation(**changes):
    return {
        "reservation_id": "R1", "user_id": UID, "origin": "JFK", "destination": "SFO",
        "flight_type": "one_way", "cabin": "basic_economy", "flights": FLIGHTS,
        "passengers": [{"first_name": "Alice", "last_name": "Smith", "dob": "1990-01-01"}],
        "payment_history": [], "created_at": "2024-05-01T10:00:00",
        "total_baggages": 0, "nonfree_baggages": 0, "insurance": "no", "status": None,
        **changes,
    }


def profile(**changes):
    return {"user_id": UID, "membership": "regular", "reservations": ["R1"],
            "payment_methods": {"card1": {"id": "card1", "source": "credit_card"}}, **changes}


def call(seq, tool, args, result="ok", **extra):
    return {"seq": seq, "tool": tool, "args": args,
            "result": json.dumps(result) if isinstance(result, (dict, list)) else result, **extra}


def evaluate(trace, domain="airline", turns=True):
    execution = {"trace": trace, "turns": [
        {"role": "user", "text": f"I am {UID}; my reservation is R1."},
        *[{"role": "assistant", "calls": [c["seq"]]} for c in trace],
    ] if turns else []}
    inputs = build_inputs(execution, domain)
    return inputs, predicates.run(trace, inputs)


def unsupported(inputs, seq, arm):
    rows = [row for row in inputs.get("unsupported_checks", {}).get(seq, []) if row["arm"] == arm]
    assert rows and all(row.get("reason") for row in rows), (seq, arm, inputs)


def test_successful_cabin_upgrade_refreshes_state_before_cancellation():
    trace = [
        call(0, "get_flight_status", FLIGHTS[0], "available"),
        call(1, "get_reservation_details", {"reservation_id": "R1"}, reservation()),
        call(2, "update_reservation_flights", {"reservation_id": "R1", "cabin": "business", "flights": FLIGHTS}, reservation(cabin="business")),
        call(3, "cancel_reservation", {"reservation_id": "R1"}, reservation(cabin="business", status="cancelled")),
    ]
    _, fires = evaluate(trace)
    assert fires == []


def test_successful_baggage_increase_refreshes_next_removal_check():
    trace = [
        call(1, "get_reservation_details", {"reservation_id": "R1"}, reservation()),
        call(2, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 2}, reservation(total_baggages=2)),
        call(3, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 1}, reservation(total_baggages=1)),
    ]
    _, fires = evaluate(trace)
    assert [(row["seq"], row["arm"]) for row in fires] == [(3, "no_bag_removal")]


def test_book_result_establishes_reservation_for_subsequent_write():
    trace = [
        call(1, "book_reservation", {"user_id": UID, "flights": FLIGHTS}, reservation(total_baggages=2)),
        call(2, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 1}, reservation(total_baggages=1)),
    ]
    _, fires = evaluate(trace)
    assert [(row["seq"], row["arm"]) for row in fires] == [(2, "no_bag_removal")]


def test_successful_retail_write_refreshes_status_for_later_action():
    order = {"order_id": "O1", "user_id": UID, "status": "pending"}
    trace = [
        call(1, "find_user_id_by_email", {"email": "alice@example.com"}, UID),
        call(2, "get_order_details", {"order_id": "O1"}, order),
        call(3, "cancel_pending_order", {"order_id": "O1", "reason": "ordered by mistake"}, {**order, "status": "cancelled"}),
        call(4, "modify_pending_order_address", {"order_id": "O1"}, {**order, "status": "cancelled"}),
    ]
    _, fires = evaluate(trace, domain="retail")
    assert [(row["seq"], row["arm"]) for row in fires] == [(4, "status_precondition")]


def test_update_cannot_supply_its_own_precondition_evidence():
    inputs, fires = evaluate([call(1, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 1}, reservation(total_baggages=1))])
    assert fires == []
    unsupported(inputs, 1, "no_bag_removal")


@pytest.mark.parametrize("flag,result,rejected", [(True, "validation failed", {1}), (False, "Error budget is a legitimate result", set())])
def test_captured_error_flag_is_authoritative(flag, result, rejected):
    inputs, _ = evaluate([call(1, "get_reservation_details", {"reservation_id": "R1"}, result, captured_error=flag)])
    assert inputs["rejected"] == rejected


def test_rejected_write_does_not_refresh_observed_state():
    trace = [
        call(1, "get_reservation_details", {"reservation_id": "R1"}, reservation(total_baggages=2)),
        call(2, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 0}, reservation(total_baggages=0), captured_error=True),
        call(3, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 1}, reservation(total_baggages=1)),
    ]
    inputs, fires = evaluate(trace)
    assert inputs["rejected"] == {2}
    assert [(row["seq"], row["arm"]) for row in fires] == [(3, "no_bag_removal")]


def test_successful_write_without_entity_result_invalidates_stale_state():
    trace = [
        call(1, "get_reservation_details", {"reservation_id": "R1"}, reservation(total_baggages=2)),
        call(2, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 3}),
        call(3, "update_reservation_baggages", {"reservation_id": "R1", "total_baggages": 1}),
    ]
    inputs, fires = evaluate(trace)
    assert fires == []
    unsupported(inputs, 3, "no_bag_removal")


def test_user_supplied_identifiers_do_not_require_a_reservation_get():
    inputs, fires = evaluate([call(1, "cancel_reservation", {"reservation_id": "R1"})])
    assert fires == []
    unsupported(inputs, 1, "cancel_eligibility")


@pytest.mark.parametrize("known_profile,expected_fire", [(None, False), (profile(payment_methods={}), True), (profile(), True), ({"user_id": UID}, False)])
def test_payment_membership_distinguishes_unknown_profile_from_known_exclusion(known_profile, expected_fire):
    trace = [] if known_profile is None else [call(1, "get_user_details", {"user_id": UID}, known_profile)]
    trace.append(call(2, "book_reservation", {"user_id": UID, "payment_methods": [{"payment_id": "card2", "amount": 10}]}))
    inputs, fires = evaluate(trace)
    assert any(row["arm"] == "payment_in_profile" for row in fires) is expected_fire
    if not expected_fire:
        unsupported(inputs, 2, "payment_in_profile")


@pytest.mark.parametrize("status,fire", [(None, False), ("delayed", True), ("available", False)])
def test_booking_requires_observed_status_to_prove_unbookability(status, fire):
    trace = [] if status is None else [call(1, "get_flight_status", FLIGHTS[0], status)]
    trace.append(call(2, "book_reservation", {"user_id": UID, "flights": FLIGHTS}))
    inputs, fires = evaluate(trace)
    assert any(row["arm"] == "bookable_status" for row in fires) is fire
    if status is None:
        unsupported(inputs, 2, "bookable_status")


@pytest.mark.parametrize("tool,args,arm", [
    ("update_reservation_baggages", {"total_baggages": 1}, "no_bag_removal"),
    ("update_reservation_passengers", {"passengers": [{"first_name": "Alice"}]}, "passenger_count_fixed"),
    ("update_reservation_flights", {"cabin": "business", "flights": FLIGHTS}, "basic_economy_no_modify"),
    ("cancel_reservation", {}, "cancel_eligibility"),
])
def test_missing_reservation_fields_are_unknown_not_zero_or_empty(tool, args, arm):
    inputs, fires = evaluate([
        call(1, "get_reservation_details", {"reservation_id": "R1"}, {"reservation_id": "R1", "user_id": UID}),
        call(2, tool, {"reservation_id": "R1", **args}),
    ])
    assert fires == []
    unsupported(inputs, 2, arm)


def test_cancellation_needs_evidence_for_every_disqualifying_branch():
    inputs, fires = evaluate([
        call(1, "get_reservation_details", {"reservation_id": "R1"}, reservation()),
        call(2, "cancel_reservation", {"reservation_id": "R1"}),
    ])
    assert fires == []
    unsupported(inputs, 2, "cancel_eligibility")
    _, fires = evaluate([
        call(1, "get_reservation_details", {"reservation_id": "R1"}, reservation()),
        call(2, "get_flight_status", FLIGHTS[0], "available"),
        call(3, "cancel_reservation", {"reservation_id": "R1"}),
    ])
    assert [(row["seq"], row["arm"]) for row in fires] == [(3, "cancel_eligibility")]


@pytest.mark.parametrize("field", ["flights", "created_at", "cabin", "insurance"])
def test_cancellation_missing_branch_evidence_does_not_convict(field):
    res = reservation()
    del res[field]
    inputs, fires = evaluate([
        call(1, "get_reservation_details", {"reservation_id": "R1"}, res),
        call(2, "get_flight_status", FLIGHTS[0], "available"),
        call(3, "cancel_reservation", {"reservation_id": "R1"}),
    ])
    assert fires == []
    unsupported(inputs, 3, "cancel_eligibility")


def test_same_day_without_departure_evidence_is_explicitly_unknown():
    inputs, fires = evaluate([
        call(1, "get_reservation_details", {"reservation_id": "R1"}, reservation(cabin="business", flights=[{"flight_number": "A1", "date": "2024-05-15"}])),
        call(2, "cancel_reservation", {"reservation_id": "R1"}),
    ])
    assert fires == []
    unsupported(inputs, 2, "cancel_flown")


def test_certificate_without_profile_or_reservation_has_no_inferred_zero_mandate():
    inputs, fires = evaluate([call(1, "send_certificate", {"user_id": UID, "amount": 100})])
    assert fires == []
    unsupported(inputs, 1, "certificate_eligibility")
    unsupported(inputs, 1, "certificate_mandate")


def test_certificate_known_complete_ineligible_context_and_wrong_amount_still_fire():
    _, fires = evaluate([
        call(1, "get_user_details", {"user_id": UID}, profile()),
        call(2, "get_reservation_details", {"reservation_id": "R1"}, reservation()),
        call(3, "send_certificate", {"user_id": UID, "amount": 999}),
    ])
    assert {row["arm"] for row in fires} == {"certificate_eligibility", "certificate_mandate"}


def test_certificate_unobserved_owned_reservation_cannot_be_ruled_out():
    inputs, fires = evaluate([
        call(1, "get_user_details", {"user_id": UID}, profile(reservations=["R1", "R2"])),
        call(2, "get_reservation_details", {"reservation_id": "R1"}, reservation()),
        call(3, "send_certificate", {"user_id": UID, "amount": 200}),
    ])
    assert fires == []
    unsupported(inputs, 3, "certificate_eligibility")
    unsupported(inputs, 3, "certificate_mandate")


def test_missing_user_turns_report_unsupported_auth_evidence():
    inputs, fires = evaluate([call(1, "book_reservation", {"user_id": UID})], turns=False)
    assert fires == []
    unsupported(inputs, 1, "user_id_from_user")


def test_retail_unknown_owner_and_missing_observed_status_are_explicit():
    inputs, fires = evaluate([
        call(1, "find_user_id_by_email", {}, UID),
        call(2, "get_order_details", {"order_id": "O1"}, {"order_id": "O1"}),
        call(3, "cancel_pending_order", {"order_id": "O1", "reason": "ordered by mistake"}),
    ], domain="retail")
    assert fires == []
    unsupported(inputs, 3, "one_user")
    unsupported(inputs, 3, "status_precondition")


def test_certificate_has_no_native_requirement_for_user_to_repeat_their_id():
    trace = [
        call(1, "get_user_details", {"user_id": UID}, profile(membership="gold")),
        call(2, "get_reservation_details", {"reservation_id": "R1"}, reservation()),
        call(3, "send_certificate", {"user_id": UID, "amount": 100}),
    ]
    inputs = build_inputs({"trace": trace, "turns": [
        {"role": "user", "text": "I want compensation."},
        *[{"role": "assistant", "calls": [seq]} for seq in [1, 2, 3]],
    ]}, "airline")
    assert predicates.run(trace, inputs) == []


def test_certificate_creation_invalidates_old_payment_collection():
    inputs, fires = evaluate([
        call(1, "get_user_details", {"user_id": UID}, profile(membership="gold")),
        call(2, "send_certificate", {"user_id": UID, "amount": 100}),
        call(3, "book_reservation", {"user_id": UID, "payment_methods": [{"payment_id": "certificate_3221322", "amount": 100}]}),
    ])
    assert fires == []
    unsupported(inputs, 3, "payment_in_profile")


@pytest.mark.parametrize("tool,args,arm", [
    ("cancel_pending_order", {"order_id": "O1"}, "cancel_reason_enum"),
    ("cancel_pending_order", {"reason": "ordered by mistake"}, "status_precondition"),
    ("modify_pending_order_items", {"order_id": "O1"}, "new_item_differs"),
])
def test_missing_retail_check_arguments_are_explicitly_unknown(tool, args, arm):
    inputs, fires = evaluate([
        call(1, "find_user_id_by_email", {}, UID),
        call(2, "get_order_details", {"order_id": "O1"}, {"order_id": "O1", "user_id": UID, "status": "pending"}),
        call(3, tool, args),
    ], domain="retail")
    assert fires == []
    unsupported(inputs, 3, arm)


def test_missing_booking_flight_arguments_report_unknown_status_check():
    inputs, fires = evaluate([call(1, "book_reservation", {"user_id": UID})])
    assert fires == []
    unsupported(inputs, 1, "bookable_status")


def test_missing_payment_target_is_not_a_proven_off_profile_payment():
    inputs, fires = evaluate([
        call(1, "get_user_details", {"user_id": UID}, profile()),
        call(2, "book_reservation", {"user_id": UID, "payment_methods": [{"amount": 10}]}),
    ])
    assert fires == []
    unsupported(inputs, 2, "payment_in_profile")
