"""aa_commons — the shared spine.

Trace schema, predicate interface, registry, and identifiers. Imported by BOTH the SDK
(provider side) and the verifier, guaranteeing one predicate implementation (invariant I3).
"""
from . import predicates  # noqa: F401  (importing registers the built-ins)
from . import registry, policy_profiles
from .ids import canonical_bytes, hash_obj, keccak_hex, params_hash, predicate_hash
from .params_spec import validate_params
from .predicate import PredicateSpec, Verdict
from .trace import ActionRecord, trace_from_json, trace_hash, trace_to_json

__all__ = [
    "ActionRecord",
    "validate_params",
    "trace_hash",
    "trace_to_json",
    "trace_from_json",
    "Verdict",
    "PredicateSpec",
    "registry",
    "predicate_hash",
    "params_hash",
    "hash_obj",
    "keccak_hex",
    "canonical_bytes",
]
