from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from eval.agentdojo import corpus
from eval.agentdojo.corpus import load_agentdojo_cases
from eval.dataset import EvaluationCohort, load_paper_main_v1


PROTOCOL_KEYS = {
    ("qwen3_30b", "workspace", "user_task_32", "important_instructions", "injection_task_0"),
    ("qwen3_30b", "workspace", "injection_task_5", "none", "none"),
    ("qwen3_30b", "travel", "user_task_4", "important_instructions", "injection_task_3"),
    ("qwen3_30b", "travel", "user_task_6", "important_instructions", "injection_task_2"),
    ("qwen3_30b", "travel", "user_task_12", "important_instructions", "injection_task_0"),
}


def test_real_corpus_census_and_protocol_ids_are_exact() -> None:
    rows = load_agentdojo_cases()

    assert len(rows) == len({row["case_id"] for row in rows}) == 2162
    assert Counter(row["model_id"] for row in rows) == {"glm47": 1081, "qwen3_30b": 1081}
    assert Counter(row["case_kind"] for row in rows) == {
        "attack": 1898,
        "benign": 194,
        "injection_baseline": 70,
    }
    assert Counter(row["protocol_status"] for row in rows) == {
        "graded": 2157,
        "protocol_error": 5,
    }
    assert {
        (
            row["model_id"],
            row["suite"],
            row["user_task_id"],
            row["attack_type"],
            row["injection_task_id"],
        )
        for row in rows
        if row["protocol_status"] == "protocol_error"
    } == PROTOCOL_KEYS


def test_benign_and_baseline_security_is_never_treated_as_a_verdict() -> None:
    rows = load_agentdojo_cases()
    for row in rows:
        if row["case_kind"] != "attack":
            assert row["stored_security"] is None
            assert row["raw_security"] is True or row["raw_security"] is None


def test_caller_supplied_cohort_is_reverified_and_membership_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = load_paper_main_v1()
    calls = 0

    def fresh_loader() -> EvaluationCohort:
        nonlocal calls
        calls += 1
        return verified

    monkeypatch.setattr(corpus, "load_paper_main_v1", fresh_loader)
    rows = load_agentdojo_cases(verified)

    assert calls == 1
    assert len(rows) == 2162


def test_forged_caller_supplied_membership_is_rejected() -> None:
    verified = load_paper_main_v1()
    forged = EvaluationCohort(
        cohort=replace(
            verified.cohort,
            accepted_shards=verified.cohort.accepted_shards[:-1],
        ),
        outer_sha256=verified.outer_sha256,
    )

    with pytest.raises(ValueError, match="caller-supplied cohort does not match freshly verified"):
        load_agentdojo_cases(forged)
