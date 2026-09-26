"""Logical state diffs and last-writer attribution for Tau DB replays."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_MISSING = object()


def _sort_key(value: Any) -> tuple[str, str]:
    return type(value).__name__, str(value)


def _append(
    rows: list[dict[str, Any]], path: tuple[str, ...], before: Any, after: Any
) -> None:
    rows.append(
        {
            "path": list(path),
            "before": None if before is _MISSING else before,
            "after": None if after is _MISSING else after,
            "before_present": before is not _MISSING,
            "after_present": after is not _MISSING,
        }
    )


def _diff(
    before: Any,
    after: Any,
    path: tuple[str, ...],
    rows: list[dict[str, Any]],
) -> None:
    if before is not _MISSING and after is not _MISSING and before == after:
        return

    # Lists are logical fields in both Tau schemas (items, flights, passengers,
    # payments, reservations). Never infer causality from coincident numeric indices.
    if isinstance(before, (list, tuple)) or isinstance(after, (list, tuple)):
        _append(rows, path, before, after)
        return

    if isinstance(before, Mapping) or isinstance(after, Mapping):
        left = before if isinstance(before, Mapping) else {}
        right = after if isinstance(after, Mapping) else {}
        keys = sorted(set(left) | set(right), key=_sort_key)
        if not keys:
            _append(rows, path, before, after)
            return
        for key in keys:
            _diff(
                left.get(key, _MISSING),
                right.get(key, _MISSING),
                path + (str(key),),
                rows,
            )
        return

    _append(rows, path, before, after)


def logical_diff(before: Any, after: Any) -> list[dict[str, Any]]:
    """Return deterministic changes, with every list treated as one logical value."""

    rows: list[dict[str, Any]] = []
    _diff(before, after, (), rows)
    return rows


def _overlap(left: list[str], right: list[str]) -> bool:
    shared = min(len(left), len(right))
    return left[:shared] == right[:shared]


def attribute_terminal_effects(
    gold_state: Any,
    actual_state: Any,
    call_deltas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attribute each persistent gold/actual divergence to its last actual writer."""

    effects: list[dict[str, Any]] = []
    for terminal in logical_diff(gold_state, actual_state):
        writer = None
        for call in call_deltas:
            if any(
                _overlap(change.get("path") or [], terminal["path"])
                for change in call.get("changes") or []
            ):
                writer = call
        effects.append(
            {
                "path": terminal["path"],
                "gold": terminal["before"],
                "actual": terminal["after"],
                "gold_present": terminal["before_present"],
                "actual_present": terminal["after_present"],
                "writer_seq": writer.get("seq") if writer else None,
                "writer_tool": writer.get("tool") if writer else None,
            }
        )
    return effects


def value_at(state: Any, path: list[str]) -> tuple[bool, Any]:
    """Read one logical projection path without conflating missing with null."""

    value = state
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            return False, None
        value = value[part]
    return True, value
