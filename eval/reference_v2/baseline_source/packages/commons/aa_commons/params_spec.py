"""Registration-time params validation — the cheap half of the malformed-promise defense.

A promise whose params don't fit its predicate crashes at adjudication, leaving the challenge
in limbo until the exit window. Two defenses compose: the SDK validates params here BEFORE
registering (catches honest mistakes), and the verifier rules a predicate that cannot be
evaluated against the provider (makes malformed-on-purpose self-slashing).

Deliberately a separate module: predicate files are untouched, and `predicate_hash` covers only
each predicate's ``evaluate`` source, so nothing here can move an on-chain hash. Validation is
permissive on unknown keys (harness adaptations add benign extras); it checks that the required
keys exist with usable types, and the handful of shape rules whose absence crashes ``evaluate``.
"""
from __future__ import annotations

import math

# spec_id -> {key: checker}; a checker returns True iff the value is usable by evaluate()
_list_of_str = lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)  # noqa: E731
_string = lambda v: isinstance(v, str)                                               # noqa: E731
# finite only: NaN passes every comparison as False, silently making a cap vacuous
_number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)  # noqa: E731

REQUIRED: dict[str, dict] = {
    "no_destructive_without_consent": {"destructive_tools": _list_of_str},
    "action_within_declared_scope": {"scoped_tools": _list_of_str, "allow_prefixes": _list_of_str},
    "payment_within_mandate": {"max_amount": _number, "merchant_allowlist": _list_of_str},
    "egress_within_allowlist": {"egress_tools": _list_of_str, "recipient_allowlist": _list_of_str},
    "aggregate_within_cap": {},   # both modes optional — but at least one must be enabled (below)
}

OPTIONAL: dict[str, dict] = {
    "no_destructive_without_consent": {"consent_tool": _string, "grant_tool": _string,
                                       "revoke_tool": _string, "match_key": _string, "scope_key": _string,
                                       "authorization_mode": lambda v: v in ("legacy", "invocation")},
    "action_within_declared_scope": {"match_key": _string, "grant_tool": _string, "revoke_tool": _string,
                                     "granted_result": _string, "scope_key": _string},
    "payment_within_mandate": {"pay_tool": _string, "recipient_key": _string, "amount_key": _string},
    "egress_within_allowlist": {"allow_prefixes": _list_of_str, "recipient_key": _string,
                                "grant_tool": _string, "revoke_tool": _string, "granted_result": _string},
    "aggregate_within_cap": {"counted_tools": _list_of_str, "summed_tools": _list_of_str,
                             "amount_key": _string, "max_count": _number, "max_sum": _number},
}


def validate_params(spec_id: str, params: dict) -> None:
    """Raise ValueError iff `params` would make `spec_id`'s evaluate unusable (or vacuous)."""
    if spec_id not in REQUIRED:
        from . import registry
        try:
            spec = registry.get(spec_id)
        except KeyError:
            raise ValueError(f"unknown predicate: {spec_id}") from None
        if spec.validate_params is None:
            raise ValueError(f"predicate has no parameter validator: {spec_id}")
        if not isinstance(params, dict):
            raise ValueError(f"{spec_id}: params must be a dict")
        spec.validate_params(params)
        return
    if not isinstance(params, dict):
        raise ValueError(f"{spec_id}: params must be a dict, got {type(params).__name__}")
    for key, ok in REQUIRED[spec_id].items():
        if key not in params:
            raise ValueError(f"{spec_id}: missing required param {key!r}")
        if not ok(params[key]):
            raise ValueError(f"{spec_id}: param {key!r} has unusable value {params[key]!r}")
    for key, ok in OPTIONAL[spec_id].items():
        if key in params and not ok(params[key]):
            raise ValueError(f"{spec_id}: param {key!r} has unusable value {params[key]!r}")
    if spec_id == "aggregate_within_cap" and params.get("max_count") is None and params.get("max_sum") is None:
        raise ValueError("aggregate_within_cap: neither max_count nor max_sum set — the promise is vacuous")
