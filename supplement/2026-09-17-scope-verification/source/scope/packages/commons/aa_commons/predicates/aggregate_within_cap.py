"""AAP-5 v2: action counts, sums and repetition sequences must satisfy declared bounds.

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
from . import legacy_aggregate_v1 as legacy

SPEC_ID = "aggregate_within_cap"
VERSION = 2


def evaluate(trace: list[ActionRecord], params: dict) -> Verdict:
    if "observation_profile" in params:
        return evaluate_profile(trace, params, "count")
    return legacy.evaluate(trace, params)


SPEC = register(PredicateSpec(
    SPEC_ID, VERSION, evaluate, number="AAP-5", doc="Action counts, sums and repetition sequences must satisfy declared bounds",
    source_bundle=source_bundle(__file__, legacy.__file__)))
