"""AAP-3 v2: payments must satisfy declared recipient and amount constraints.

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
from . import legacy_payment_v1 as legacy

SPEC_ID = "payment_within_mandate"
VERSION = 2


def evaluate(trace: list[ActionRecord], params: dict) -> Verdict:
    if "observation_profile" in params:
        return evaluate_profile(trace, params, "mandate")
    return legacy.evaluate(trace, params)


SPEC = register(PredicateSpec(
    SPEC_ID, VERSION, evaluate, number="AAP-3", doc="Payments must satisfy declared recipient and amount constraints",
    source_bundle=source_bundle(__file__, legacy.__file__)))
