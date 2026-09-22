"""The predicate registry — the alternative to a DSL.

Promises select a predicate by ``spec_id`` and supply parameters as JSON. The registry
maps ids to implementations; the SAME ``aa_commons`` is imported by both the SDK and the
verifier, so there is exactly one implementation per id (single source of truth).
"""
from __future__ import annotations

from .ids import predicate_hash
from .predicate import PredicateSpec

_REGISTRY: dict[str, PredicateSpec] = {}
_HISTORICAL: list[PredicateSpec] = []


def _source(spec: PredicateSpec) -> str:
    import inspect
    return spec.source_bundle if spec.source_bundle is not None else inspect.getsource(spec.evaluate)


def register_historical(spec: PredicateSpec) -> PredicateSpec:
    """Retain the exact evaluator of a previously committed catalog version."""
    _HISTORICAL.append(spec)
    return spec


def resolve_hash(committed_hash: str) -> PredicateSpec | None:
    """Resolve current OR retired source by content, never silently upgrading an old promise."""
    for spec in [*_REGISTRY.values(), *_HISTORICAL]:
        if predicate_hash(_source(spec)) == committed_hash:
            return spec
    return None


def register(spec: PredicateSpec) -> PredicateSpec:
    if spec.spec_id in _REGISTRY:
        raise ValueError(f"predicate already registered: {spec.spec_id}")
    _REGISTRY[spec.spec_id] = spec
    return spec


def get(spec_id: str) -> PredicateSpec:
    if spec_id not in _REGISTRY:
        raise KeyError(f"unknown predicate: {spec_id}")
    return _REGISTRY[spec_id]


def predicate_source(spec_id: str) -> str:
    """The exact source of the predicate's evaluate fn — the auditable logic its hash binds."""
    return _source(get(spec_id))


def predicate_hash_for(spec_id: str) -> str:
    return predicate_hash(predicate_source(spec_id))


def all_specs() -> dict[str, PredicateSpec]:
    return dict(_REGISTRY)


def catalog() -> list[dict]:
    """The promise catalog — every registered predicate as a numbered, versioned, auditable entry
    (the ERC-style index; predicate_hash content-addresses the source)."""
    return [
        {"number": s.number, "spec_id": s.spec_id, "version": s.version,
         "statement": s.doc, "predicate_hash": predicate_hash_for(sid)}
        for sid, s in sorted(_REGISTRY.items(), key=lambda kv: kv[1].number)
    ]
