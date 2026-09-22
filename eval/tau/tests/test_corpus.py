from __future__ import annotations

from dataclasses import replace

import pytest

from eval.dataset import EvaluationCohort, load_paper_main_v1
from eval.tau import corpus
from eval.tau.corpus import load_tau_cases, normalize_simulation


def test_normalize_simulation_pairs_results_and_preserves_turn_order() -> None:
    simulation = {
        "messages": [
            {"role": "user", "content": "my id is alice_smith_1"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "get_user_details",
                        "arguments": {"user_id": "alice_smith_1"},
                        "requestor": "assistant",
                    },
                    {
                        "id": "c2",
                        "name": "send_certificate",
                        "arguments": {"user_id": "alice_smith_1", "amount": 999},
                        "requestor": "assistant",
                    },
                ],
            },
            {
                "role": "tool",
                "id": "c1",
                "content": '{"user_id":"alice_smith_1"}',
                "error": False,
            },
            {"role": "tool", "id": "c2", "content": "Certificate added", "error": False},
        ]
    }

    normalized = normalize_simulation(simulation)

    assert normalized["trace"] == [
        {
            "seq": 1,
            "tool": "get_user_details",
            "args": {"user_id": "alice_smith_1"},
            "result": '{"user_id":"alice_smith_1"}',
            "requestor": "assistant",
            "tool_call_id": "c1",
            "captured_error": False,
        },
        {
            "seq": 2,
            "tool": "send_certificate",
            "args": {"user_id": "alice_smith_1", "amount": 999},
            "result": "Certificate added",
            "requestor": "assistant",
            "tool_call_id": "c2",
            "captured_error": False,
        },
    ]
    assert normalized["turns"] == [
        {"role": "user", "text": "my id is alice_smith_1", "calls": []},
        {"role": "assistant", "text": "", "calls": [1, 2]},
    ]


@pytest.mark.parametrize(
    "messages",
    [
        [
            {"role": "tool", "id": "c1", "content": "ok", "error": False},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "name": "get_order_details", "arguments": {}}
                ],
            },
        ],
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "name": "first", "arguments": {}},
                    {"id": "c2", "name": "second", "arguments": {}},
                ],
            },
            {"role": "tool", "id": "c2", "content": "second", "error": False},
            {"role": "tool", "id": "c1", "content": "first", "error": False},
        ],
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "name": "get_order_details", "arguments": {}}
                ],
            },
            {"role": "user", "content": "interposed"},
            {"role": "tool", "id": "c1", "content": "ok", "error": False},
        ],
    ],
)
def test_normalize_simulation_rejects_nonadjacent_or_out_of_order_results(
    messages: list[dict],
) -> None:
    with pytest.raises(ValueError, match="Tau tool result"):
        normalize_simulation({"messages": messages})


def test_paper_main_tau_loader_is_exhaustive_and_model_separated() -> None:
    cases = load_tau_cases()

    assert len(cases) == 328
    assert len({case["case_id"] for case in cases}) == 328
    assert len({case["case_key"] for case in cases}) == 328
    assert {case["benchmark"] for case in cases} == {"tau"}
    assert {
        model: sum(case["model_id"] == model for case in cases)
        for model in ("glm47", "qwen3_30b")
    } == {"glm47": 164, "qwen3_30b": 164}
    assert {
        (model, domain): sum(
            case["model_id"] == model and case["domain"] == domain for case in cases
        )
        for model in ("glm47", "qwen3_30b")
        for domain in ("airline", "retail")
    } == {
        ("glm47", "airline"): 50,
        ("glm47", "retail"): 114,
        ("qwen3_30b", "airline"): 50,
        ("qwen3_30b", "retail"): 114,
    }
    assert all(case["native_reward"] in {0.0, 1.0} for case in cases)
    assert all(isinstance(case["recorded_db_match"], bool) for case in cases)
    assert all("source_path" not in case for case in cases)

    expected_membership = {
        (model, domain, str(task_id), 0)
        for model in ("glm47", "qwen3_30b")
        for domain, count in (("airline", 50), ("retail", 114))
        for task_id in range(count)
    }
    assert {
        (case["model_id"], case["domain"], case["task_id"], case["trial"])
        for case in cases
    } == expected_membership


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
    cases = load_tau_cases(verified)

    assert calls == 1
    assert len(cases) == 328


def test_forged_caller_supplied_membership_is_rejected() -> None:
    verified = load_paper_main_v1()
    removed_tau_shard = False
    accepted_shards = []
    for shard in verified.cohort.accepted_shards:
        if shard.benchmark == "tau" and not removed_tau_shard:
            removed_tau_shard = True
            continue
        accepted_shards.append(shard)
    assert removed_tau_shard
    forged = EvaluationCohort(
        cohort=replace(
            verified.cohort,
            accepted_shards=tuple(accepted_shards),
        ),
        outer_sha256=verified.outer_sha256,
    )

    with pytest.raises(
        ValueError, match="caller-supplied cohort does not match freshly verified"
    ):
        load_tau_cases(forged)
