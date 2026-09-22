from __future__ import annotations

import importlib
import json
import os
import random
import sys
from pathlib import Path

import pytest


from eval.tau.capture import integrity


def _load_runner():
    return importlib.import_module("eval.tau.capture.run")


def _simulation(task_id: str, trial: int = 0) -> dict:
    return {
        "id": f"sim-{task_id}-{trial}",
        "task_id": task_id,
        "trial": trial,
        "seed": random.Random(300).randint(0, 1_000_000),
        "messages": [
            {"role": "user", "content": "request"},
            {"role": "assistant", "content": "done"},
        ],
        "reward_info": {
            "reward": 1.0,
            "reward_basis": ["DB"],
            "reward_breakdown": {"DB": 1.0},
        },
    }


def _write_results(path: Path, simulations: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "tasks": [{"id": task_id} for task_id in dict.fromkeys(simulation["task_id"] for simulation in simulations)],
                "info": {"seed": 300},
                "simulations": simulations,
            }
        )
    )


def _write_call(path: Path, *, call_id: str | None, model: str | None, served: str | None, provider: str | None, cost: float | None, tokens: tuple[int, int] | None, route_tag: str = "openai") -> None:
    if "llm_debug" not in path.parts:
        path = path.parent / "task_0" / "sim_0" / "llm_debug" / path.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "call_id": call_id,
                "request": {
                    "model": model,
                    "requested_provider_tag": route_tag,
                    "expected_served_model": served,
                },
                "response": {
                    "raw_response": {
                        "id": call_id,
                        "model": served,
                        "provider": provider,
                        "choices": [{"finish_reason": "stop"}],
                        "usage": None if tokens is None else {
                            "prompt_tokens": tokens[0], "completion_tokens": tokens[1],
                            "total_tokens": sum(tokens), "cost": cost,
                        },
                    }
                },
            }
        )
    )


def test_git_commit_reads_the_checkout_dynamically(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "tracked").write_text("one")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "first"], check=True)
    first = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (tmp_path / "tracked").write_text("two")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "second"], check=True)
    second = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    assert first != second
    assert integrity.git_commit(tmp_path) == second


def test_validate_tau_results_requires_the_exact_task_trial_set(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    _write_results(results, [_simulation("2"), _simulation("8")])

    validation = integrity.validate_tau_results(results, ["2", "8", "16"])

    assert validation["ok"] is False
    assert validation["missing_task_trials"] == [{"task_id": "16", "trial": 0}]


def test_validate_tau_results_rejects_missing_native_reward(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    simulation = _simulation("2")
    simulation["reward_info"] = None
    _write_results(results, [simulation])

    validation = integrity.validate_tau_results(results, ["2"])

    assert validation["ok"] is False
    assert "missing full reward_info" in validation["errors"]


def test_validate_tau_results_rejects_wrong_top_level_task_set(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    _write_results(results, [_simulation("2")])
    data = json.loads(results.read_text())
    data["tasks"] = [{"id": "8"}]
    results.write_text(json.dumps(data))

    validation = integrity.validate_tau_results(results, ["2"], expected_seed=300)

    assert validation["ok"] is False
    assert "top-level task set does not match expected task set" in validation["errors"]


def test_validate_tau_results_rejects_wrong_simulation_seed(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    simulation = _simulation("2")
    simulation["seed"] = 999
    _write_results(results, [simulation])

    validation = integrity.validate_tau_results(results, ["2"], expected_seed=300)

    assert validation["ok"] is False
    assert "simulation 0 has unexpected seed" in validation["errors"]


def test_validate_tau_results_rejects_wrong_configured_seed(tmp_path: Path) -> None:
    results = tmp_path / "results.json"
    _write_results(results, [_simulation("2")])
    data = json.loads(results.read_text())
    data["info"]["seed"] = 999
    results.write_text(json.dumps(data))

    validation = integrity.validate_tau_results(results, ["2"], expected_seed=300)

    assert validation["ok"] is False
    assert "configured seed does not match expected seed" in validation["errors"]


def test_summarize_llm_logs_aggregates_unique_generation_cost_and_tokens(tmp_path: Path) -> None:
    _write_call(tmp_path / "a.json", call_id="gen-a", model="openrouter/openai/gpt-4.1-mini", served="openai/gpt-4.1-mini-2025-04-14", provider="OpenAI", cost=0.01, tokens=(10, 2))
    _write_call(tmp_path / "b.json", call_id="gen-b", model="openrouter/openai/gpt-4.1-mini", served="openai/gpt-4.1-mini-2025-04-14", provider="OpenAI", cost=0.02, tokens=(20, 3))

    summary = integrity.summarize_llm_logs(tmp_path)

    assert summary["ok"] is True
    assert summary["generation_count"] == 2
    assert summary["prompt_tokens"] == 30
    assert summary["completion_tokens"] == 5
    assert summary["total_tokens"] == 35
    assert summary["cost_usd"] == 0.03


def test_summarize_llm_logs_rejects_mixed_model_or_provider(tmp_path: Path) -> None:
    _write_call(tmp_path / "a.json", call_id="gen-a", model="openrouter/z-ai/model", served="z-ai/model-r1", provider="DeepInfra", cost=0.01, tokens=(1, 1))
    _write_call(tmp_path / "b.json", call_id="gen-b", model="openrouter/z-ai/model", served="z-ai/model-r2", provider="Other", cost=0.01, tokens=(1, 1))

    summary = integrity.summarize_llm_logs(tmp_path)

    assert summary["ok"] is False
    assert "mixed served models for requested model openrouter/z-ai/model" in summary["errors"]
    assert "mixed response providers for requested model openrouter/z-ai/model" in summary["errors"]


def test_summarize_llm_logs_rejects_duplicate_generation_ids(tmp_path: Path) -> None:
    for name in ("a", "b"):
        _write_call(tmp_path / f"{name}.json", call_id="gen-duplicate", model="openrouter/openai/model", served="openai/model-r1", provider="OpenAI", cost=0.01, tokens=(1, 1))

    summary = integrity.summarize_llm_logs(tmp_path)

    assert summary["ok"] is False
    assert "duplicate generation id: gen-duplicate" in summary["errors"]


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"call_id": None}, "incomplete provenance"),
        ({"model": None}, "incomplete provenance"),
        ({"served": None}, "incomplete provenance"),
        ({"provider": None}, "incomplete provenance"),
        ({"cost": None}, "invalid cost metadata"),
        ({"tokens": None}, "no token/cost metadata"),
    ],
)
def test_summarize_llm_logs_rejects_missing_call_evidence(tmp_path: Path, overrides: dict, error: str) -> None:
    values = {"call_id": "gen-a", "model": "openrouter/openai/model", "served": "openai/model-r1", "provider": "OpenAI", "cost": 0.01, "tokens": (1, 1)}
    values.update(overrides)
    _write_call(tmp_path / "call.json", **values)

    summary = integrity.summarize_llm_logs(tmp_path)

    assert summary["ok"] is False
    assert any(error in message for message in summary["errors"])


def test_summarize_llm_logs_requires_exact_agent_user_and_judge_routes(tmp_path: Path) -> None:
    routes = {
        "openrouter/z-ai/model": {"provider": "deepinfra/bf16", "expected_served_model": "z-ai/model-r1"},
        "openrouter/openai/gpt-4.1-mini": {"provider": "openai", "expected_served_model": "openai/gpt-4.1-mini-2025-04-14"},
        "openrouter/openai/gpt-4.1-2025-04-14": {"provider": "openai", "expected_served_model": "openai/gpt-4.1-2025-04-14"},
    }
    for index, (model, route) in enumerate(routes.items()):
        _write_call(tmp_path / f"{index}.json", call_id=f"gen-{index}", model=model, served=route["expected_served_model"], provider="DeepInfra" if index == 0 else "OpenAI", route_tag=route["provider"], cost=0.01, tokens=(1, 1))

    summary = integrity.summarize_llm_logs(tmp_path, expected_routes=routes, max_cost_usd=0.04)

    assert summary["ok"] is True
    del routes["openrouter/openai/gpt-4.1-2025-04-14"]
    rejected = integrity.summarize_llm_logs(tmp_path, expected_routes=routes, max_cost_usd=0.04)
    assert rejected["ok"] is False
    assert "unexpected requested model" in rejected["errors"]


def test_summarize_llm_logs_rejects_cost_above_ceiling(tmp_path: Path) -> None:
    _write_call(tmp_path / "call.json", call_id="gen-a", model="openrouter/openai/model", served="openai/model-r1", provider="OpenAI", cost=0.36, tokens=(1, 1))
    summary = integrity.summarize_llm_logs(tmp_path, max_cost_usd=0.35)
    assert summary["ok"] is False
    assert "LLM cost exceeds configured ceiling" in summary["errors"]


def test_capture_lock_refuses_existing_unbound_output(tmp_path: Path) -> None:
    (tmp_path / "results.json").write_text("{}")
    with pytest.raises(RuntimeError, match="content but no capture lock"):
        integrity.ensure_capture_lock(tmp_path, {"domain": "airline"})


def test_capture_lock_rejects_configuration_drift(tmp_path: Path) -> None:
    assert integrity.ensure_capture_lock(tmp_path, {"domain": "airline"}) == "created"
    with pytest.raises(RuntimeError, match="configuration drift"):
        integrity.ensure_capture_lock(tmp_path, {"domain": "retail"})


def test_tau_command_is_noninteractive_and_zero_retry() -> None:
    run = _load_runner()

    command = run.build_tau_command(
        domain="airline",
        agent="z-ai/glm-4.7-flash",
        user="openai/gpt-4.1-mini",
        task_ids=["2", "8"],
        seed=300,
        max_concurrency=2,
        save="screen/airline.json",
        timeout=240.0,
    )

    assert command.count("--max-retries") == 1
    assert command[command.index("--max-retries") + 1] == "0"
    assert command[command.index("--timeout") + 1] == "240.0"
    assert "--auto-resume" not in command
    assert "--verbose-logs" in command
    assert command[command.index("--llm-log-mode") + 1] == "all"
    assert command[command.index("--num-trials") + 1] == "1"


def test_capture_routes_separate_response_alias_from_endpoint_revision() -> None:
    run = _load_runner()
    routes = run.capture_routes(
        agent="z-ai/glm-4.7-flash",
        provider="deepinfra/bf16",
        endpoint_revision="z-ai/glm-4.7-flash-20260119",
        user="openai/gpt-4.1-mini",
    )
    assert routes == {
        "openrouter/z-ai/glm-4.7-flash": {
            "provider": "deepinfra/bf16",
            "response_model": "z-ai/glm-4.7-flash",
            "endpoint_revision": "z-ai/glm-4.7-flash-20260119",
        },
        "openrouter/openai/gpt-4.1-mini": {
            "provider": "openai",
            "response_model": "openai/gpt-4.1-mini",
            "endpoint_revision": "openai/gpt-4.1-mini-2025-04-14",
        },
        "openrouter/openai/gpt-4.1-2025-04-14": {
            "provider": "openai",
            "response_model": "openai/gpt-4.1",
            "endpoint_revision": "openai/gpt-4.1-2025-04-14",
        },
    }


def test_expected_tasks_use_tau_native_serialization() -> None:
    run = _load_runner()
    tasks = run._expected_tasks("airline", ["2"])

    assert tasks[0]["evaluation_criteria"]["actions"][0]["requestor"] == "assistant"
    assert tasks[0]["evaluation_criteria"]["actions"][0]["compare_args"] is None
    assert "annotations" not in tasks[0]


def test_nonzero_tau_subprocess_exit_is_a_failure(tmp_path: Path) -> None:
    run = _load_runner()

    with pytest.raises(RuntimeError, match="tau2 exited with status 7"):
        run.run_tau_process(
            [sys.executable, "-c", "raise SystemExit(7)"],
            tmp_path / "tau.log",
        )


def test_failed_process_cannot_write_passing_manifest(tmp_path: Path, monkeypatch) -> None:
    run = _load_runner()
    outdir = tmp_path / "batch"
    monkeypatch.setattr(run, "usage", lambda: 1.0)
    monkeypatch.setattr(run, "run_tau_process", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("tau2 exited with status 9")))

    with pytest.raises(RuntimeError, match="status 9"):
        run.capture_batch(
            domain="airline", agent="z-ai/model", provider="deepinfra/bf16",
            expected_served_model="z-ai/model-r1", user="openai/gpt-4.1-mini",
            task_ids=["2"], seed=300, max_concurrency=1, timeout=240,
            max_cost_usd=0.35, outdir=outdir,
        )

    assert not (outdir / "manifest.json").exists()
    lock = json.loads((outdir / "capture-lock.json").read_text())
    assert lock["task_payload_sha256"] == (
        "93e1705f84f24c7918938f1cdf1ef7de3cd7d527a28a9edd9fdc134d4dc20265"
    )


def test_passing_capture_manifest_excludes_coverage_derived_fields(tmp_path: Path, monkeypatch) -> None:
    run = _load_runner()
    outdir = tmp_path / "batch"
    monkeypatch.setattr(run, "usage", lambda: 1.0)
    monkeypatch.setattr(run, "git_commit", lambda _path: "a" * 40)
    monkeypatch.setattr(run, "_runner_sha256", lambda: "b" * 64)
    monkeypatch.setattr(run, "_expected_tasks", lambda _domain, _task_ids: [{"id": "2"}])
    monkeypatch.setattr(run, "capture_routes", lambda **_kwargs: {})
    monkeypatch.setattr(run, "run_tau_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        run,
        "validate_tau_capture",
        lambda *_args, **_kwargs: {
            "errors": [],
            "tau_results": {"native_failure_count": 1},
            "llm_calls": {"cost_usd": 0.01},
            "call_counts": {},
            "per_simulation": {},
            "generation_ids": [],
        },
    )

    manifest = run.capture_batch(
        domain="airline", agent="z-ai/model", provider="deepinfra/bf16",
        expected_served_model="z-ai/model-r1", user="openai/gpt-4.1-mini",
        task_ids=["2"], seed=300, max_concurrency=1, timeout=240,
        max_cost_usd=0.35, outdir=outdir,
    )

    assert not any("coverage" in key or "promise" in key for key in manifest)
    assert not any("coverage" in key or "promise" in key for key in json.loads((outdir / "manifest.json").read_text()))


def test_cost_bounded_capture_requires_sequential_execution(tmp_path: Path) -> None:
    run = _load_runner()
    with pytest.raises(ValueError, match="max_concurrency=1"):
        run.capture_batch(
            domain="airline", agent="z-ai/model", provider="deepinfra/bf16",
            expected_served_model="z-ai/model-r1", user="openai/gpt-4.1-mini",
            task_ids=["2"], seed=300, max_concurrency=2, timeout=240,
            max_cost_usd=0.35, outdir=tmp_path / "batch",
        )


# Round-one review regressions. These use the real frozen Tau task payloads while
# keeping all model-call evidence synthetic and offline. The tau2 checkout is
# resolved the same way as the capture runner (TAU2_BENCH_ROOT, defaulting to
# the sibling tau2-explore checkout).
TAU_DATA = (
    Path(os.environ.get("TAU2_BENCH_ROOT", Path.home() / "research" / "tau2-explore"))
    / "data"
    / "tau2"
    / "domains"
)
AIRLINE_IDS = ["2", "8", "16", "26", "37", "48"]
RETAIL_IDS = ["9", "18", "33", "60", "94", "111"]


def _frozen_tasks(domain: str, task_ids: list[str]) -> list[dict]:
    if not TAU_DATA.is_dir():
        pytest.skip(f"tau2 domain data not found at {TAU_DATA}; set TAU2_BENCH_ROOT")
    tasks = json.loads((TAU_DATA / domain / "tasks.json").read_text())
    by_id = {str(task["id"]): task for task in tasks}
    return [by_id[task_id] for task_id in task_ids]


def _raw(call_id: str, model: str, served: str, provider: str = "OpenAI") -> dict:
    return {
        "id": call_id,
        "model": served,
        "provider": provider,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok", "tool_calls": None},
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "cost": 0.001,
        },
    }


def _native_reward(task: dict) -> dict:
    criteria = task["evaluation_criteria"]
    basis = criteria["reward_basis"]
    breakdown = {reward_type: 1.0 for reward_type in basis}
    reward = {
        "reward": 1.0,
        "db_check": {"db_match": True, "db_reward": 1.0}
        if "DB" in basis
        else None,
        "env_assertions": [] if "ENV_ASSERTION" in basis else None,
        "action_checks": [] if "ACTION" in basis else None,
        "communicate_checks": [
            {"info": info, "met": True, "justification": "said"}
            for info in (criteria.get("communicate_info") or [])
        ]
        if "COMMUNICATE" in basis
        else None,
        "nl_assertions": [
            {"nl_assertion": assertion, "met": True, "justification": "met"}
            for assertion in (criteria.get("nl_assertions") or [])
        ]
        if "NL_ASSERTION" in basis
        else None,
        "reward_basis": basis,
        "reward_breakdown": breakdown,
    }
    return reward


def _screen_results(path: Path, tasks: list[dict], domain: str = "airline") -> list[dict]:
    trial_seed = random.Random(300).randint(0, 1_000_000)
    simulations = []
    for task in tasks:
        task_id = str(task["id"])
        agent_raw = _raw("agent-" + task_id, "openrouter/z-ai/model", "z-ai/model-r1", "DeepInfra")
        user_raw = _raw(
            "user-" + task_id,
            "openrouter/openai/gpt-4.1-mini",
            "openai/gpt-4.1-mini-2025-04-14",
        )
        simulations.append(
            {
                "id": "sim-" + task_id,
                "task_id": task_id,
                "trial": 0,
                "seed": trial_seed,
                "start_time": "2026-08-12T00:00:00Z",
                "end_time": "2026-08-12T00:00:01Z",
                "duration": 1.0,
                "termination_reason": "user_stop",
                "messages": [
                    {"role": "user", "content": "request", "raw_data": user_raw},
                    {"role": "assistant", "content": "ok", "raw_data": agent_raw},
                ],
                "reward_info": _native_reward(task),
            }
        )
    path.write_text(
        json.dumps(
            {
                "tasks": tasks,
                "info": {
                    "git_commit": "0" * 40,
                    "num_trials": 1,
                    "max_steps": 200,
                    "max_errors": 10,
                    "user_info": {
                        "implementation": "user_simulator",
                        "llm": "openrouter/openai/gpt-4.1-mini",
                    },
                    "agent_info": {
                        "implementation": "llm_agent",
                        "llm": "openrouter/z-ai/model",
                    },
                    "environment_info": {"domain_name": domain, "policy": "frozen"},
                    "seed": 300,
                },
                "simulations": simulations,
            }
        )
    )
    return simulations


def _artifact_call(
    artifacts: Path,
    *,
    task_id: str,
    simulation_id: str,
    call_name: str,
    raw: dict,
    requested_model: str,
    provider_tag: str,
    expected_served: str,
    response_content: str | None = None,
) -> Path:
    path = (
        artifacts
        / f"task_{task_id}"
        / f"sim_{simulation_id}"
        / "llm_debug"
        / f"000_{call_name}_call.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "call_id": raw["id"],
        "call_name": call_name,
        "request": {
            "model": requested_model,
            "requested_provider_tag": provider_tag,
            "expected_served_model": expected_served,
        },
        "response": {"raw_response": raw},
    }
    if response_content is not None:
        payload["response"]["raw_response"]["choices"][0]["message"]["content"] = response_content
    path.write_text(json.dumps(payload))
    return path


def _routes() -> dict[str, dict[str, str]]:
    return {
        "openrouter/z-ai/model": {
            "provider": "deepinfra/bf16",
            "expected_served_model": "z-ai/model-r1",
        },
        "openrouter/openai/gpt-4.1-mini": {
            "provider": "openai",
            "expected_served_model": "openai/gpt-4.1-mini-2025-04-14",
        },
        "openrouter/openai/gpt-4.1-2025-04-14": {
            "provider": "openai",
            "expected_served_model": "openai/gpt-4.1-2025-04-14",
        },
    }


def _validate_capture(path: Path, task_ids: list[str], tasks: list[dict]) -> dict:
    return integrity.validate_tau_capture(
        path,
        expected_task_ids=task_ids,
        expected_tasks=tasks,
        expected_role_models={
            "agent_response": "openrouter/z-ai/model",
            "user_simulator_response": "openrouter/openai/gpt-4.1-mini",
            "nl_assertions_eval": "openrouter/openai/gpt-4.1-2025-04-14",
        },
        expected_routes=_routes(),
        expected_seed=300,
        max_cost_usd=0.35,
    )


def _write_final_logs(
    artifacts: Path,
    simulations: list[dict],
    tasks: list[dict],
    *,
    include_judge: bool = True,
) -> None:
    tasks_by_id = {str(task["id"]): task for task in tasks}
    for simulation in simulations:
        task_id = simulation["task_id"]
        _artifact_call(
            artifacts,
            task_id=task_id,
            simulation_id=simulation["id"],
            call_name="user_simulator_response",
            raw=simulation["messages"][0]["raw_data"],
            requested_model="openrouter/openai/gpt-4.1-mini",
            provider_tag="openai",
            expected_served="openai/gpt-4.1-mini-2025-04-14",
        )
        _artifact_call(
            artifacts,
            task_id=task_id,
            simulation_id=simulation["id"],
            call_name="agent_response",
            raw=simulation["messages"][1]["raw_data"],
            requested_model="openrouter/z-ai/model",
            provider_tag="deepinfra/bf16",
            expected_served="z-ai/model-r1",
        )
        task = tasks_by_id.get(task_id)
        criteria = task["evaluation_criteria"] if task is not None else {}
        if include_judge and "NL_ASSERTION" in criteria.get("reward_basis", []) and criteria.get("nl_assertions"):
            checks = simulation["reward_info"]["nl_assertions"]
            judge_raw = _raw(
                "judge-" + task_id,
                "openrouter/openai/gpt-4.1-2025-04-14",
                "openai/gpt-4.1-2025-04-14",
            )
            _artifact_call(
                artifacts,
                task_id=task_id,
                simulation_id=simulation["id"],
                call_name="nl_assertions_eval",
                raw=judge_raw,
                requested_model="openrouter/openai/gpt-4.1-2025-04-14",
                provider_tag="openai",
                expected_served="openai/gpt-4.1-2025-04-14",
                response_content=json.dumps(
                    {
                        "results": [
                            {
                                "expectedOutcome": check["nl_assertion"],
                                "metExpectation": check["met"],
                                "reasoning": check["justification"],
                            }
                            for check in checks
                        ]
                    }
                ),
            )
        status_path = artifacts / f"task_{task_id}" / f"sim_{simulation['id']}" / "sim_status.json"
        status_path.write_text(json.dumps({"status": "used"}))


def test_real_frozen_airline_allows_zero_judge_calls(tmp_path: Path) -> None:
    tasks = _frozen_tasks("airline", AIRLINE_IDS)
    simulations = _screen_results(tmp_path / "results.json", tasks, "airline")
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)

    validation = _validate_capture(tmp_path, AIRLINE_IDS, tasks)

    assert validation["ok"] is True
    assert validation["call_counts"]["nl_assertions_eval"] == 0


def test_real_frozen_retail_requires_exact_two_judge_calls(tmp_path: Path) -> None:
    tasks = _frozen_tasks("retail", RETAIL_IDS)
    simulations = _screen_results(tmp_path / "results.json", tasks, "retail")
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)

    validation = _validate_capture(tmp_path, RETAIL_IDS, tasks)

    assert validation["ok"] is True
    assert validation["call_counts"]["nl_assertions_eval"] == 2
    assert validation["per_simulation"]["sim-60"]["nl_assertions_eval"] == 1
    assert validation["per_simulation"]["sim-111"]["nl_assertions_eval"] == 1


def test_retail_native_nl_checks_reject_missing_judge_log(tmp_path: Path) -> None:
    tasks = _frozen_tasks("retail", ["60"])
    simulations = _screen_results(tmp_path / "results.json", tasks, "retail")
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)
    next((tmp_path / "artifacts").rglob("*nl_assertions_eval*.json")).unlink()

    validation = _validate_capture(tmp_path, ["60"], tasks)

    assert validation["ok"] is False
    assert "judge call count does not match frozen task semantics" in validation["errors"]


def test_native_outcome_without_nl_checks_rejects_extra_judge_log(tmp_path: Path) -> None:
    tasks = _frozen_tasks("airline", ["2"])
    simulations = _screen_results(tmp_path / "results.json", tasks)
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)
    judge_raw = _raw(
        "judge-extra",
        "openrouter/openai/gpt-4.1-2025-04-14",
        "openai/gpt-4.1-2025-04-14",
    )
    _artifact_call(
        tmp_path / "artifacts",
        task_id="2",
        simulation_id="sim-2",
        call_name="nl_assertions_eval",
        raw=judge_raw,
        requested_model="openrouter/openai/gpt-4.1-2025-04-14",
        provider_tag="openai",
        expected_served="openai/gpt-4.1-2025-04-14",
    )

    validation = _validate_capture(tmp_path, ["2"], tasks)

    assert validation["ok"] is False
    assert "judge call count does not match frozen task semantics" in validation["errors"]


def test_capture_rejects_missing_and_orphan_final_generation_logs(tmp_path: Path) -> None:
    tasks = _frozen_tasks("airline", ["2"])
    simulations = _screen_results(tmp_path / "results.json", tasks)
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)
    agent_log = next((tmp_path / "artifacts").rglob("*agent_response*.json"))
    agent_log.unlink()
    orphan = _raw("agent-orphan", "openrouter/z-ai/model", "z-ai/model-r1", "DeepInfra")
    _artifact_call(
        tmp_path / "artifacts",
        task_id="2",
        simulation_id="sim-2",
        call_name="agent_response",
        raw=orphan,
        requested_model="openrouter/z-ai/model",
        provider_tag="deepinfra/bf16",
        expected_served="z-ai/model-r1",
    )

    validation = _validate_capture(tmp_path, ["2"], tasks)

    assert validation["ok"] is False
    assert "final message/log generation IDs do not match" in validation["errors"]


def test_capture_rejects_direct_root_json_in_artifacts(tmp_path: Path) -> None:
    tasks = _frozen_tasks("airline", ["2"])
    simulations = _screen_results(tmp_path / "results.json", tasks)
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)
    (tmp_path / "artifacts" / "decoy.json").write_text("{}")

    validation = _validate_capture(tmp_path, ["2"], tasks)

    assert validation["ok"] is False
    assert "unexpected artifact JSON path" in validation["errors"]


def test_capture_rejects_missing_trace_raw_generation_id(tmp_path: Path) -> None:
    tasks = _frozen_tasks("airline", ["2"])
    simulations = _screen_results(tmp_path / "results.json", tasks)
    simulations[0]["messages"][1]["raw_data"]["id"] = None
    data = json.loads((tmp_path / "results.json").read_text())
    data["simulations"] = simulations
    (tmp_path / "results.json").write_text(json.dumps(data))
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)

    validation = _validate_capture(tmp_path, ["2"], tasks)

    assert validation["ok"] is False
    assert "trace raw response has no generation id" in validation["errors"]


def test_capture_accepts_premature_native_reward_and_no_judge(tmp_path: Path) -> None:
    tasks = _frozen_tasks("retail", ["60"])
    simulations = _screen_results(tmp_path / "results.json", tasks, "retail")
    simulation = simulations[0]
    simulation["termination_reason"] = "timeout"
    simulation["reward_info"] = {"reward": 0.0, "info": {"reason": "timeout"}}
    data = json.loads((tmp_path / "results.json").read_text())
    data["simulations"] = simulations
    (tmp_path / "results.json").write_text(json.dumps(data))
    _write_final_logs(tmp_path / "artifacts", simulations, tasks, include_judge=False)

    validation = _validate_capture(tmp_path, ["60"], tasks)

    assert validation["ok"] is True
    assert validation["call_counts"]["nl_assertions_eval"] == 0


def test_successful_nl_task_accepts_empty_judge_results(tmp_path: Path) -> None:
    tasks = _frozen_tasks("retail", ["60"])
    simulations = _screen_results(tmp_path / "results.json", tasks, "retail")
    simulations[0]["reward_info"]["nl_assertions"] = []
    data = json.loads((tmp_path / "results.json").read_text())
    data["simulations"] = simulations
    (tmp_path / "results.json").write_text(json.dumps(data))
    _write_final_logs(tmp_path / "artifacts", simulations, tasks)

    validation = _validate_capture(tmp_path, ["60"], tasks)

    assert validation["ok"] is True
    assert validation["call_counts"]["nl_assertions_eval"] == 1


def test_capture_rejects_model_and_frozen_task_substitution(tmp_path: Path) -> None:
    tasks = _frozen_tasks("airline", ["2"])
    simulations = _screen_results(tmp_path / "results.json", tasks)
    wrong_raw = _raw(
        "agent-2",
        "openrouter/openai/gpt-4.1-2025-04-14",
        "openai/gpt-4.1-2025-04-14",
    )
    simulations[0]["messages"][1]["raw_data"] = wrong_raw
    data = json.loads((tmp_path / "results.json").read_text())
    data["info"]["agent_info"]["llm"] = "openrouter/openai/gpt-4.1-2025-04-14"
    data["tasks"][0]["evaluation_criteria"]["nl_assertions"] = []
    data["simulations"] = simulations
    (tmp_path / "results.json").write_text(json.dumps(data))
    _artifact_call(
        tmp_path / "artifacts",
        task_id="2",
        simulation_id="sim-2",
        call_name="user_simulator_response",
        raw=simulations[0]["messages"][0]["raw_data"],
        requested_model="openrouter/openai/gpt-4.1-mini",
        provider_tag="openai",
        expected_served="openai/gpt-4.1-mini-2025-04-14",
    )
    _artifact_call(
        tmp_path / "artifacts",
        task_id="2",
        simulation_id="sim-2",
        call_name="agent_response",
        raw=wrong_raw,
        requested_model="openrouter/openai/gpt-4.1-2025-04-14",
        provider_tag="openai",
        expected_served="openai/gpt-4.1-2025-04-14",
    )

    validation = _validate_capture(tmp_path, ["2"], tasks)

    assert validation["ok"] is False
    assert "top-level task payloads do not match expected tasks" in validation["errors"]
    assert "agent_response log does not use its expected role model" in validation["errors"]


def test_capture_routes_reject_same_model_role_conflict() -> None:
    run = _load_runner()
    with pytest.raises(ValueError, match="conflicting capture routes"):
        run.capture_routes(
            agent="openai/gpt-4.1-mini",
            provider="deepinfra/bf16",
            expected_served_model="other/revision",
            user="openai/gpt-4.1-mini",
        )


def test_capture_batch_refuses_any_existing_output_content(tmp_path: Path) -> None:
    run = _load_runner()
    outdir = tmp_path / "not-fresh"
    outdir.mkdir()
    (outdir / "partial.txt").write_text("existing")

    with pytest.raises(RuntimeError, match="fresh output directory"):
        run.capture_batch(
            domain="airline",
            agent="z-ai/model",
            provider="deepinfra/bf16",
            expected_served_model="z-ai/model-r1",
            user="openai/gpt-4.1-mini",
            task_ids=["2"],
            seed=300,
            max_concurrency=1,
            timeout=240,
            max_cost_usd=0.35,
            outdir=outdir,
        )
