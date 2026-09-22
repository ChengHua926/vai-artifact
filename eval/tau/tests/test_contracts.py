from __future__ import annotations

import pytest

from eval.tau.contracts import effect_contract_manifest, exact_links_for_fire


def _state(reservation: dict) -> dict:
    return {"agent": {"reservations": {"R1": reservation}}, "user": None}


def _effect(path: list[str], gold, actual, seq: int = 4) -> dict:
    return {
        "path": path,
        "gold": gold,
        "actual": actual,
        "gold_present": True,
        "actual_present": True,
        "writer_seq": seq,
        "writer_tool": "update_reservation_baggages",
    }


@pytest.mark.parametrize(
    "arm",
    ["auth_first", "read_before_write", "user_id_from_user", "payment_in_profile"],
)
def test_process_and_provenance_arms_are_never_exact(arm: str) -> None:
    state = _state({"reservation_id": "R1", "total_baggages": 2})
    call = {
        "seq": 4,
        "tool": "update_reservation_baggages",
        "args": {"reservation_id": "R1", "total_baggages": 1},
        "accepted": True,
        "before_state": state,
        "after_state": _state({"reservation_id": "R1", "total_baggages": 1}),
    }

    assert exact_links_for_fire(
        {"seq": 4, "arm": arm},
        call=call,
        terminal_effects=[
            _effect(["agent", "reservations", "R1", "total_baggages"], 2, 1)
        ],
        actual_final=call["after_state"],
        gold_final=state,
    ) == []


def test_baggage_removal_exactly_links_to_persistent_wrong_total() -> None:
    before = _state({"reservation_id": "R1", "total_baggages": 2})
    after = _state({"reservation_id": "R1", "total_baggages": 1})
    effect = _effect(
        ["agent", "reservations", "R1", "total_baggages"], 2, 1
    )

    links = exact_links_for_fire(
        {"seq": 4, "arm": "no_bag_removal"},
        call={
            "seq": 4,
            "tool": "update_reservation_baggages",
            "args": {"reservation_id": "R1", "total_baggages": 1},
            "accepted": True,
            "before_state": before,
            "after_state": after,
        },
        terminal_effects=[effect],
        actual_final=after,
        gold_final=before,
    )

    assert links == [
        {
            "arm": "no_bag_removal",
            "seq": 4,
            "tool": "update_reservation_baggages",
            "protected_projection": "reservation.total_baggages",
            "effect_paths": [["agent", "reservations", "R1", "total_baggages"]],
        }
    ]


def test_basic_economy_flight_change_uses_flight_identity_not_list_indices() -> None:
    old = [{"flight_number": "A1", "date": "2024-05-20", "price": 10}]
    new = [{"flight_number": "B2", "date": "2024-05-20", "price": 20}]
    before = _state({"reservation_id": "R1", "cabin": "basic_economy", "flights": old})
    after = _state({"reservation_id": "R1", "cabin": "basic_economy", "flights": new})
    effect = {
        **_effect(["agent", "reservations", "R1", "flights"], old, new),
        "writer_tool": "update_reservation_flights",
    }

    links = exact_links_for_fire(
        {"seq": 4, "arm": "basic_economy_no_modify"},
        call={
            "seq": 4,
            "tool": "update_reservation_flights",
            "args": {
                "reservation_id": "R1",
                "cabin": "basic_economy",
                "flights": [{"flight_number": "B2", "date": "2024-05-20"}],
            },
            "accepted": True,
            "before_state": before,
            "after_state": after,
        },
        terminal_effects=[effect],
        actual_final=after,
        gold_final=before,
    )

    assert links[0]["protected_projection"] == "reservation.flight_identity_multiset"
    assert links[0]["effect_paths"] == [["agent", "reservations", "R1", "flights"]]


def test_unobserved_bookable_status_cannot_become_an_exact_effect_claim() -> None:
    before = {"agent": {"reservations": {}, "flights": {}}, "user": None}
    after = {
        "agent": {
            "reservations": {"NEW": {"reservation_id": "NEW"}},
            "flights": {},
        },
        "user": None,
    }
    effect = {
        "path": ["agent", "reservations", "NEW", "reservation_id"],
        "gold": None,
        "actual": "NEW",
        "gold_present": False,
        "actual_present": True,
        "writer_seq": 1,
        "writer_tool": "book_reservation",
    }

    assert exact_links_for_fire(
        {"seq": 1, "arm": "bookable_status", "targets": ["A1@2024-05-20:unobserved"]},
        call={
            "seq": 1,
            "tool": "book_reservation",
            "args": {"flights": [{"flight_number": "A1", "date": "2024-05-20"}]},
            "accepted": True,
            "before_state": before,
            "after_state": after,
        },
        terminal_effects=[effect],
        actual_final=after,
        gold_final=before,
    ) == []


def test_contract_manifest_marks_process_and_provenance_arms_unprovable() -> None:
    manifest = {row["arm"]: row for row in effect_contract_manifest()}

    assert set(manifest) == {
        "auth_first",
        "basic_economy_no_modify",
        "bookable_status",
        "cancel_eligibility",
        "cancel_flown",
        "cancel_reason_enum",
        "certificate_eligibility",
        "certificate_mandate",
        "flown_no_cabin_change",
        "modify_items_lockout",
        "new_item_differs",
        "no_bag_removal",
        "once_per_order",
        "one_user",
        "passenger_count_fixed",
        "payment_in_profile",
        "read_before_write",
        "status_precondition",
        "user_id_from_user",
    }
    assert {
        arm for arm, row in manifest.items() if row["exact_status"] == "never_exact"
    } == {"auth_first", "read_before_write", "user_id_from_user", "payment_in_profile"}


def test_identical_exchange_links_to_gold_different_new_item() -> None:
    before_order = {
        "exchange_items": None,
        "exchange_new_items": None,
        "status": "delivered",
    }
    actual_order = {
        "exchange_items": ["old"],
        "exchange_new_items": ["old"],
        "status": "exchange requested",
    }
    gold_order = {
        "exchange_items": ["old"],
        "exchange_new_items": ["new"],
        "status": "exchange requested",
    }
    before = {"agent": {"orders": {"#W1": before_order}}, "user": None}
    after = {"agent": {"orders": {"#W1": actual_order}}, "user": None}
    gold = {"agent": {"orders": {"#W1": gold_order}}, "user": None}
    effect = {
        "path": ["agent", "orders", "#W1", "exchange_new_items"],
        "gold": ["new"],
        "actual": ["old"],
        "gold_present": True,
        "actual_present": True,
        "writer_seq": 4,
        "writer_tool": "exchange_delivered_order_items",
    }

    links = exact_links_for_fire(
        {"seq": 4, "arm": "new_item_differs", "targets": ["old"]},
        call={
            "seq": 4,
            "tool": "exchange_delivered_order_items",
            "args": {
                "order_id": "#W1",
                "item_ids": ["old"],
                "new_item_ids": ["old"],
            },
            "accepted": True,
            "before_state": before,
            "after_state": after,
        },
        terminal_effects=[effect],
        actual_final=after,
        gold_final=gold,
    )

    assert links[0]["protected_projection"] == "order.old_new_item_multisets"
    assert links[0]["effect_paths"] == [
        ["agent", "orders", "#W1", "exchange_new_items"]
    ]


def test_certificate_mandate_links_only_the_created_amount_field() -> None:
    user_id = "u_1"
    before = {
        "agent": {
            "users": {user_id: {"payment_methods": {}}},
            "reservations": {
                "R1": {"user_id": user_id, "passengers": [{}, {}]},
            },
        },
        "user": None,
    }
    cert = {"id": "certificate_1", "source": "certificate", "amount": 999}
    after = {
        "agent": {
            "users": {user_id: {"payment_methods": {"certificate_1": cert}}},
            "reservations": before["agent"]["reservations"],
        },
        "user": None,
    }
    effects = [
        {
            "path": ["agent", "users", user_id, "payment_methods", "certificate_1", field],
            "gold": None,
            "actual": value,
            "gold_present": False,
            "actual_present": True,
            "writer_seq": 7,
            "writer_tool": "send_certificate",
        }
        for field, value in cert.items()
    ]

    links = exact_links_for_fire(
        {"seq": 7, "arm": "certificate_mandate", "targets": ["amount"]},
        call={
            "seq": 7,
            "tool": "send_certificate",
            "args": {"user_id": user_id, "amount": 999},
            "accepted": True,
            "before_state": before,
            "after_state": after,
        },
        terminal_effects=effects,
        actual_final=after,
        gold_final=before,
    )

    assert links[0]["effect_paths"] == [
        ["agent", "users", user_id, "payment_methods", "certificate_1", "amount"]
    ]


def test_certificate_mandate_is_not_exact_when_gold_amount_is_also_disallowed() -> None:
    user_id = "u_1"
    before = {
        "agent": {
            "users": {user_id: {"payment_methods": {}}},
            "reservations": {
                "R1": {"user_id": user_id, "passengers": [{}]},
            },
        },
        "user": None,
    }
    actual_certificate = {
        "id": "certificate_1",
        "source": "certificate",
        "amount": 150,
    }
    gold_certificate = {
        "id": "certificate_1",
        "source": "certificate",
        "amount": 200,
    }
    after = {
        "agent": {
            "users": {
                user_id: {"payment_methods": {"certificate_1": actual_certificate}}
            },
            "reservations": before["agent"]["reservations"],
        },
        "user": None,
    }
    gold = {
        "agent": {
            "users": {
                user_id: {"payment_methods": {"certificate_1": gold_certificate}}
            },
            "reservations": before["agent"]["reservations"],
        },
        "user": None,
    }
    effect = {
        "path": [
            "agent",
            "users",
            user_id,
            "payment_methods",
            "certificate_1",
            "amount",
        ],
        "gold": 200,
        "actual": 150,
        "gold_present": True,
        "actual_present": True,
        "writer_seq": 7,
        "writer_tool": "send_certificate",
    }

    assert exact_links_for_fire(
        {"seq": 7, "arm": "certificate_mandate", "targets": ["amount"]},
        call={
            "seq": 7,
            "tool": "send_certificate",
            "args": {"user_id": user_id, "amount": 150},
            "accepted": True,
            "before_state": before,
            "after_state": after,
        },
        terminal_effects=[effect],
        actual_final=after,
        gold_final=gold,
    ) == []
