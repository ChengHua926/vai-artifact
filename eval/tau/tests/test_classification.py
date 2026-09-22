from __future__ import annotations

from eval.tau.evaluate import classify_failure_relation


def test_fired_response_only_failure_has_no_persistent_wrong_effect() -> None:
    assert classify_failure_relation(
        any_fire=True,
        relations={"no_persistent_wrong_effect"},
    ) == "no_persistent_wrong_effect"


def test_failure_relation_prefers_exact_then_same_writer() -> None:
    assert classify_failure_relation(
        any_fire=True,
        relations={"different_writer", "exact_protected_effect"},
    ) == "exact_protected_effect"
    assert classify_failure_relation(
        any_fire=True,
        relations={"different_writer", "same_writer_unproven"},
    ) == "same_writer_unproven"
    assert classify_failure_relation(any_fire=False, relations=set()) == "no_fire"
