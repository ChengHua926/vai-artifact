"""AAP-2 v4: actions must satisfy declared scope and state constraints.

Original parameters retain their original behavior. Structured parameters select
an exact source-bound observation profile and one declared rule. Shared checking
logic evaluates the resulting operands; profile code defines domain observations
and policy applicability. Previous commitments resolve to the historical version.
"""
from ..predicate import PredicateSpec, Verdict
from ..registry import register
from ..trace import ActionRecord
from ..policy_profiles import evaluate_profile
from ..policy_catalog import source_bundle
from . import legacy_scope_v3 as legacy

SPEC_ID = "action_within_declared_scope"
VERSION = 4


def evaluate(trace: list[ActionRecord], params: dict) -> Verdict:
    if "observation_profile" in params:
        return evaluate_profile(trace, params, "scope")
    return legacy.evaluate(trace, params)


SPEC = register(PredicateSpec(
    SPEC_ID, VERSION, evaluate, number="AAP-2", doc="Actions must satisfy declared scope and state constraints",
    source_bundle=source_bundle(__file__, legacy.__file__)))
