"""Mechanical arm-to-state contracts for exact Tau causal links."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable

from .effects import value_at


UNPROVABLE_ARMS = frozenset(
    {"auth_first", "read_before_write", "user_id_from_user", "payment_in_profile"}
)
FIXED_NOW = datetime(2024, 5, 15, 15, 0, 0)
TODAY = FIXED_NOW.date()

_RETAIL_STATUS = {
    "cancel_pending_order": {"pending"},
    "modify_pending_order_items": {"pending"},
    "modify_pending_order_address": {"pending"},
    "modify_pending_order_payment": {"pending"},
    "return_delivered_order_items": {"delivered"},
    "exchange_delivered_order_items": {"delivered"},
}

_PROJECTIONS = {
    "auth_first": "process_ordering",
    "basic_economy_no_modify": "reservation.flight_identity_multiset",
    "bookable_status": "booking.creation_and_seat_decrement",
    "cancel_eligibility": "reservation.status",
    "cancel_flown": "reservation.status",
    "cancel_reason_enum": "order.cancel_reason",
    "certificate_eligibility": "user.certificate_existence",
    "certificate_mandate": "user.created_certificate_amount",
    "flown_no_cabin_change": "reservation.cabin",
    "modify_items_lockout": "tool_target_state_patch",
    "new_item_differs": "order.old_new_item_multisets",
    "no_bag_removal": "reservation.total_baggages",
    "once_per_order": "tool_target_state_patch",
    "one_user": "tool_target_state_patch",
    "passenger_count_fixed": "reservation.passenger_count",
    "payment_in_profile": "observed_profile_provenance",
    "read_before_write": "process_ordering",
    "status_precondition": "tool_target_state_patch",
    "user_id_from_user": "conversation_provenance",
}


def effect_contract_manifest() -> list[dict[str, str]]:
    """Describe every compiled arm's maximum mechanically supportable claim."""

    return [
        {
            "schema_version": 1,
            "arm": arm,
            "protected_projection": projection,
            "exact_status": "never_exact" if arm in UNPROVABLE_ARMS else "conditional",
        }
        for arm, projection in sorted(_PROJECTIONS.items())
    ]


def _prefix(path: list[str], root: list[str]) -> bool:
    return path[: len(root)] == root


def _writer_effects(
    terminal_effects: list[dict[str, Any]], seq: int, roots: list[list[str]]
) -> list[dict[str, Any]]:
    return [
        effect
        for effect in terminal_effects
        if effect.get("writer_seq") == seq
        and any(_prefix(effect.get("path") or [], root) for root in roots)
    ]


def _same_projection(left: Any, right: Any, transform: Callable[[Any], Any]) -> bool:
    return transform(left) == transform(right)


def _at(state: dict[str, Any], path: list[str]) -> Any:
    present, value = value_at(state, path)
    return value if present else None


def _gold_preserves_before(
    effects: list[dict[str, Any]], before_state: dict[str, Any], gold_state: dict[str, Any]
) -> bool:
    for effect in effects:
        before_present, before = value_at(before_state, effect["path"])
        gold_present, gold = value_at(gold_state, effect["path"])
        if before_present != gold_present or before != gold:
            return False
    return True


def _link(
    arm: str,
    call: dict[str, Any],
    projection: str,
    effects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not effects:
        return []
    return [
        {
            "arm": arm,
            "seq": call["seq"],
            "tool": call["tool"],
            "protected_projection": projection,
            "effect_paths": [effect["path"] for effect in effects],
        }
    ]


def _flight_ids(value: Any) -> Counter[tuple[str | None, str | None]]:
    return Counter(
        (flight.get("flight_number"), flight.get("date"))
        for flight in (value or [])
        if isinstance(flight, dict)
    )


def _item_ids(value: Any) -> Counter[str | None]:
    return Counter(
        item.get("item_id") if isinstance(item, dict) else item for item in (value or [])
    )


def _flown(reservation: dict[str, Any]) -> bool:
    for flight in reservation.get("flights") or []:
        try:
            if datetime.strptime(flight.get("date", ""), "%Y-%m-%d").date() < TODAY:
                return True
        except (AttributeError, TypeError, ValueError):
            continue
    return False


def _cancel_eligible(state: dict[str, Any], reservation: dict[str, Any]) -> bool:
    try:
        if datetime.fromisoformat(reservation.get("created_at", "")) >= (
            FIXED_NOW - timedelta(hours=24)
        ):
            return True
    except (TypeError, ValueError):
        pass
    if reservation.get("cabin") == "business" or reservation.get("insurance") == "yes":
        return True
    for flight in reservation.get("flights") or []:
        status = _at(
            state,
            [
                "agent",
                "flights",
                str(flight.get("flight_number")),
                "dates",
                str(flight.get("date")),
                "status",
            ],
        )
        if str(status).lower() == "cancelled":
            return True
    return False


def _retail_patch_links(
    arm: str,
    call: dict[str, Any],
    terminal_effects: list[dict[str, Any]],
    gold_final: dict[str, Any],
) -> list[dict[str, Any]]:
    args = call.get("args") or {}
    roots: list[list[str]] = []
    if args.get("order_id") is not None:
        roots.append(["agent", "orders", str(args["order_id"])])
    if args.get("user_id") is not None:
        roots.append(["agent", "users", str(args["user_id"])])
    effects = _writer_effects(terminal_effects, call["seq"], roots)
    if not effects or not _gold_preserves_before(effects, call["before_state"], gold_final):
        return []
    return _link(arm, call, "tool_target_state_patch", effects)


def _new_certificates(
    before: dict[str, Any], after: dict[str, Any], user_id: str
) -> dict[str, Any]:
    before_methods = _at(before, ["agent", "users", user_id, "payment_methods"]) or {}
    after_methods = _at(after, ["agent", "users", user_id, "payment_methods"]) or {}
    return {
        key: value
        for key, value in after_methods.items()
        if key not in before_methods
        and isinstance(value, dict)
        and value.get("source") == "certificate"
    }


def _certificate_allowed_amounts(state: dict[str, Any], user_id: str) -> set[int]:
    reservations = _at(state, ["agent", "reservations"]) or {}
    return {
        multiplier * len(reservation.get("passengers") or [])
        for reservation in reservations.values()
        if isinstance(reservation, dict)
        and reservation.get("user_id") == user_id
        and reservation.get("passengers")
        for multiplier in (50, 100)
    }


def exact_links_for_fire(
    fire: dict[str, Any],
    *,
    call: dict[str, Any] | None,
    terminal_effects: list[dict[str, Any]],
    actual_final: dict[str, Any],
    gold_final: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return exact links only when the arm's protected DB fact is mechanically proven."""

    arm = fire.get("arm")
    if (
        arm in UNPROVABLE_ARMS
        or call is None
        or fire.get("seq") != call.get("seq")
        or not call.get("accepted")
    ):
        return []
    seq = call["seq"]
    tool = call.get("tool")
    args = call.get("args") or {}
    before = call.get("before_state") or {}
    after = call.get("after_state") or {}

    if arm == "one_user":
        return _retail_patch_links(arm, call, terminal_effects, gold_final)

    if arm == "status_precondition":
        order_id = str(args.get("order_id"))
        status = _at(before, ["agent", "orders", order_id, "status"])
        if tool not in _RETAIL_STATUS or status in _RETAIL_STATUS[tool]:
            return []
        return _retail_patch_links(arm, call, terminal_effects, gold_final)

    if arm in {"modify_items_lockout", "once_per_order"}:
        return _retail_patch_links(arm, call, terminal_effects, gold_final)

    if arm == "new_item_differs":
        if not any(
            old == new
            for old, new in zip(args.get("item_ids") or [], args.get("new_item_ids") or [])
        ):
            return []
        if tool != "exchange_delivered_order_items":
            return []
        order_id = str(args.get("order_id"))
        old_path = ["agent", "orders", order_id, "exchange_items"]
        new_path = ["agent", "orders", order_id, "exchange_new_items"]
        actual_old = _item_ids(_at(after, old_path))
        actual_new = _item_ids(_at(after, new_path))
        gold_old = _item_ids(_at(gold_final, old_path))
        gold_new = _item_ids(_at(gold_final, new_path))
        # DB stores these lists sorted, so exact pairing is unavailable. Requiring
        # disjoint gold multisets is conservative proof that every gold new item differs.
        if (
            actual_old != actual_new
            or actual_new == gold_new
            or sum(gold_old.values()) != sum(gold_new.values())
            or bool(gold_old & gold_new)
        ):
            return []
        roots = [old_path, new_path]
        effects = _writer_effects(terminal_effects, seq, roots)
        if not effects:
            return []
        return _link(arm, call, "order.old_new_item_multisets", effects)

    if arm == "cancel_reason_enum":
        order_id = str(args.get("order_id"))
        path = ["agent", "orders", order_id, "cancel_reason"]
        effects = _writer_effects(terminal_effects, seq, [path])
        if args.get("reason") in {"no longer needed", "ordered by mistake"}:
            return []
        if not effects or not _gold_preserves_before(effects, before, gold_final):
            return []
        return _link(arm, call, "order.cancel_reason", effects)

    if arm == "bookable_status":
        targets = [str(target) for target in fire.get("targets") or []]
        if not targets or any(target.endswith(":unobserved") for target in targets):
            return []
        # Tau's tool rejects every truly unavailable flight. Keep the contract explicit
        # in case a future pinned implementation accepts one.
        for target in targets:
            flight_date, _, observed = target.rpartition(":")
            flight_number, _, date = flight_date.partition("@")
            actual_status = _at(
                before,
                ["agent", "flights", flight_number, "dates", date, "status"],
            )
            if str(actual_status) != observed or observed == "available":
                return []
        effects = _writer_effects(
            terminal_effects,
            seq,
            [["agent", "reservations"], ["agent", "users"], ["agent", "flights"]],
        )
        if not effects:
            return []
        return _link(arm, call, "booking.creation_and_seat_decrement", effects)

    reservation_id = str(args.get("reservation_id"))
    reservation_path = ["agent", "reservations", reservation_id]
    before_reservation = _at(before, reservation_path) or {}

    if arm == "basic_economy_no_modify":
        path = reservation_path + ["flights"]
        effects = _writer_effects(terminal_effects, seq, [path])
        before_value = _at(before, path)
        after_value = _at(after, path)
        actual_value = _at(actual_final, path)
        gold_value = _at(gold_final, path)
        if (
            before_reservation.get("cabin") != "basic_economy"
            or _same_projection(before_value, after_value, _flight_ids)
            or _same_projection(actual_value, gold_value, _flight_ids)
            or not _same_projection(before_value, gold_value, _flight_ids)
        ):
            return []
        return _link(arm, call, "reservation.flight_identity_multiset", effects)

    if arm == "flown_no_cabin_change":
        path = reservation_path + ["cabin"]
        effects = _writer_effects(terminal_effects, seq, [path])
        if not _flown(before_reservation) or _at(before, path) == _at(after, path):
            return []
        if _at(gold_final, path) != _at(before, path):
            return []
        return _link(arm, call, "reservation.cabin", effects)

    if arm == "no_bag_removal":
        path = reservation_path + ["total_baggages"]
        effects = _writer_effects(terminal_effects, seq, [path])
        before_count, after_count = _at(before, path), _at(after, path)
        if (
            not isinstance(before_count, int)
            or not isinstance(after_count, int)
            or after_count >= before_count
        ):
            return []
        if _at(gold_final, path) != before_count:
            return []
        return _link(arm, call, "reservation.total_baggages", effects)

    if arm == "passenger_count_fixed":
        path = reservation_path + ["passengers"]
        effects = _writer_effects(terminal_effects, seq, [path])
        before_count = len(_at(before, path) or [])
        after_count = len(_at(after, path) or [])
        if before_count == after_count or len(_at(gold_final, path) or []) != before_count:
            return []
        return _link(arm, call, "reservation.passenger_count", effects)

    if arm in {"cancel_flown", "cancel_eligibility"}:
        path = reservation_path + ["status"]
        effects = _writer_effects(terminal_effects, seq, [path])
        violation = _flown(before_reservation) if arm == "cancel_flown" else (
            not _flown(before_reservation) and not _cancel_eligible(before, before_reservation)
        )
        if (
            not violation
            or _at(after, path) != "cancelled"
            or _at(gold_final, path) != _at(before, path)
        ):
            return []
        return _link(arm, call, "reservation.status", effects)

    if arm in {"certificate_eligibility", "certificate_mandate"}:
        user_id = str(args.get("user_id"))
        new_certificates = _new_certificates(before, after, user_id)
        if not new_certificates:
            return []
        roots = [
            ["agent", "users", user_id, "payment_methods", certificate_id]
            for certificate_id in new_certificates
        ]
        effects = _writer_effects(terminal_effects, seq, roots)
        if arm == "certificate_eligibility":
            user = _at(before, ["agent", "users", user_id]) or {}
            reservations = (_at(before, ["agent", "reservations"]) or {}).values()
            eligible = user.get("membership") in {"silver", "gold"} or any(
                isinstance(reservation, dict)
                and reservation.get("user_id") == user_id
                and (
                    reservation.get("insurance") == "yes"
                    or reservation.get("cabin") == "business"
                )
                for reservation in reservations
            )
            if eligible or any(
                _at(gold_final, root) is not None and _at(before, root) is None for root in roots
            ):
                return []
            return _link(arm, call, "user.certificate_existence", effects)

        allowed = _certificate_allowed_amounts(before, user_id)
        amount = args.get("amount")
        if amount in allowed:
            return []
        for root in roots:
            gold_certificate = _at(gold_final, root)
            if gold_certificate is not None and (
                not isinstance(gold_certificate, dict)
                or gold_certificate.get("amount") not in allowed
            ):
                return []
        amount_effects = [effect for effect in effects if effect["path"][-1:] == ["amount"]]
        return _link(arm, call, "user.created_certificate_amount", amount_effects)

    return []
