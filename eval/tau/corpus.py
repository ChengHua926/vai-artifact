"""Load only loader-accepted Tau shards and normalize their action streams."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.artifacts import (
    canonical_case_key,
    stable_case_id,
    validate_unique_case_rows,
)
from eval.dataset import EvaluationCohort, load_paper_main_v1


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _validate_tool_result_order(simulation: dict[str, Any]) -> None:
    messages = simulation.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Tau simulation messages must be a list")
    pending: list[str] = []
    seen_call_ids: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"Tau message {index} must be an object")
        role = message.get("role")
        if pending:
            if role != "tool":
                raise ValueError(
                    f"Tau tool result for {pending[0]} must immediately follow its call"
                )
            result_id = message.get("id")
            if result_id != pending[0]:
                raise ValueError(
                    f"Tau tool result is out of order: expected {pending[0]}, got {result_id}"
                )
            pending.pop(0)
            continue
        if role == "tool":
            raise ValueError(
                f"Tau tool result has no immediately preceding call: {message.get('id')}"
            )
        if role != "assistant":
            continue
        raw_calls = message.get("tool_calls")
        if raw_calls is None:
            continue
        if not isinstance(raw_calls, list):
            raise ValueError(f"Tau assistant tool_calls must be a list: message {index}")
        for call in raw_calls:
            if not isinstance(call, dict):
                raise ValueError(f"Tau tool call must be an object: message {index}")
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Tau tool call is missing its id")
            if call_id in seen_call_ids:
                raise ValueError(f"duplicate Tau tool call id: {call_id}")
            seen_call_ids.add(call_id)
            pending.append(call_id)
    if pending:
        raise ValueError(f"Tau tool result is missing for call: {pending[0]}")


def normalize_simulation(simulation: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Convert Tau messages into the exact trace/turn inputs used by the old monitor."""

    _validate_tool_result_order(simulation)
    results: dict[str, dict[str, Any]] = {}
    for message in simulation.get("messages") or []:
        if message.get("role") != "tool":
            continue
        call_id = message.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Tau tool result is missing its call id")
        if call_id in results:
            raise ValueError(f"duplicate Tau tool result id: {call_id}")
        captured_error = message.get("error")
        if not isinstance(captured_error, bool):
            raise ValueError(f"Tau tool result lacks a boolean error flag: {call_id}")
        results[call_id] = {
            "content": _text(message.get("content")),
            "error": captured_error,
        }

    trace: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    seq = 0
    seen_call_ids: set[str] = set()
    for message in simulation.get("messages") or []:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        call_seqs: list[int] = []
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("Tau tool call is missing its id")
                if call_id in seen_call_ids:
                    raise ValueError(f"duplicate Tau tool call id: {call_id}")
                if call_id not in results:
                    raise ValueError(f"Tau tool call has no result: {call_id}")
                arguments = call.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise ValueError(f"Tau tool arguments must be an object: {call_id}")
                seq += 1
                call_seqs.append(seq)
                seen_call_ids.add(call_id)
                trace.append(
                    {
                        "seq": seq,
                        "tool": call.get("name"),
                        "args": dict(arguments),
                        "result": results[call_id]["content"],
                        "requestor": call.get("requestor") or "assistant",
                        "tool_call_id": call_id,
                        "captured_error": results[call_id]["error"],
                    }
                )
        turns.append(
            {
                "role": role,
                "text": _text(message.get("content"))[:2000],
                "calls": call_seqs,
            }
        )

    orphaned = sorted(set(results) - seen_call_ids)
    if orphaned:
        raise ValueError(f"Tau tool results have no calls: {orphaned[0]}")
    return {"trace": trace, "turns": turns}


def _read_results(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse accepted Tau results: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"accepted Tau results must be an object: {path}")
    return value


def _cohort_identity(loaded: EvaluationCohort) -> tuple[Any, ...]:
    cohort = loaded.cohort
    return (
        loaded.outer_sha256,
        cohort.cohort_id,
        cohort.lock_path,
        cohort.outer_manifest_path,
        tuple(
            (
                shard.benchmark,
                shard.model_id,
                shard.kind,
                shard.path,
                shard.root_sha256,
                shard.accepted_count,
            )
            for shard in cohort.accepted_shards
        ),
    )


def load_tau_cases(
    evaluation_cohort: EvaluationCohort | None = None,
) -> list[dict[str, Any]]:
    """Return the 328 sealed Tau simulations with portable, stable identities."""

    loaded = load_paper_main_v1()
    if evaluation_cohort is not None and (
        not isinstance(evaluation_cohort, EvaluationCohort)
        or _cohort_identity(evaluation_cohort) != _cohort_identity(loaded)
    ):
        raise ValueError(
            "caller-supplied cohort does not match freshly verified paper_main_v1"
        )
    cases: list[dict[str, Any]] = []
    for shard in loaded.cohort.accepted_shards:
        if shard.benchmark != "tau":
            continue
        results_path = shard.path / "results.json"
        payload = _read_results(results_path)
        simulations = payload.get("simulations")
        tasks = payload.get("tasks")
        if not isinstance(simulations, list) or not isinstance(tasks, list):
            raise ValueError(f"accepted Tau results lack tasks/simulations: {results_path}")
        if len(simulations) != shard.accepted_count:
            raise ValueError(
                f"accepted Tau shard count mismatch: {shard.path}: "
                f"{len(simulations)} != {shard.accepted_count}"
            )
        task_by_id = {str(task.get("id")): task for task in tasks if isinstance(task, dict)}
        if len(task_by_id) != len(tasks):
            raise ValueError(f"duplicate or malformed Tau tasks: {results_path}")
        relative_source = results_path.relative_to(loaded.cohort.lock_path.parent).as_posix()
        for simulation in simulations:
            if not isinstance(simulation, dict):
                raise ValueError(f"Tau simulation must be an object: {results_path}")
            task_id = str(simulation.get("task_id"))
            if task_id not in task_by_id:
                raise ValueError(
                    f"Tau simulation references unknown task {task_id}: {results_path}"
                )
            reward_info = simulation.get("reward_info")
            if not isinstance(reward_info, dict):
                raise ValueError(f"Tau simulation lacks reward_info: {simulation.get('id')}")
            native_reward = reward_info.get("reward")
            db_check = reward_info.get("db_check")
            if native_reward not in {0, 0.0, 1, 1.0} or not isinstance(db_check, dict):
                raise ValueError(f"accepted Tau simulation is not graded: {simulation.get('id')}")
            recorded_db_match = db_check.get("db_match")
            if not isinstance(recorded_db_match, bool):
                raise ValueError(
                    f"accepted Tau simulation lacks db verdict: {simulation.get('id')}"
                )
            identity = {
                "domain": shard.kind,
                "task_id": task_id,
                "trial": simulation.get("trial"),
            }
            case_key = canonical_case_key(
                benchmark="tau", model_id=shard.model_id, identity=identity
            )
            normalized = normalize_simulation(simulation)
            cases.append(
                {
                    "schema_version": 1,
                    "case_id": stable_case_id(loaded.outer_sha256, case_key),
                    "case_key": case_key,
                    "cohort_id": loaded.cohort.cohort_id,
                    "cohort_outer_sha256": loaded.outer_sha256,
                    "benchmark": "tau",
                    "benchmark_version": "tau2@7ac89f5128bc9ff86e37cf49a54687b35083e598",
                    "model_id": shard.model_id,
                    "domain": shard.kind,
                    "task_id": task_id,
                    "trial": simulation.get("trial"),
                    "simulation_id": simulation.get("id"),
                    "termination_reason": simulation.get("termination_reason"),
                    "native_reward": float(native_reward),
                    "recorded_db_match": recorded_db_match,
                    "source": {
                        "path": relative_source,
                        "root_sha256": shard.root_sha256,
                    },
                    "task": task_by_id[task_id],
                    "simulation": simulation,
                    **normalized,
                }
            )
    validated = validate_unique_case_rows(cases)
    return [dict(row) for row in sorted(validated, key=lambda row: row["case_key"])]
