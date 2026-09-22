"""Pure, fail-closed integrity checks for Tau capture artifacts."""
from __future__ import annotations

import json
import math
import random
import subprocess
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


def git_commit(checkout: Path) -> str:
    """Return the full commit currently checked out at ``checkout``."""
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise RuntimeError("git rev-parse did not return a full commit")
    return commit


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _task_trial(task_id: Any, trial: Any) -> tuple[str, int] | None:
    if not isinstance(task_id, (str, int)) or isinstance(task_id, bool):
        return None
    if not isinstance(trial, int) or isinstance(trial, bool) or trial < 0:
        return None
    return str(task_id), trial


def validate_tau_results(
    path: Path,
    expected_task_ids: Iterable[str],
    *,
    num_trials: int = 1,
    require_complete: bool = True,
    expected_seed: int | None = None,
) -> dict[str, Any]:
    """Validate one unique, complete Tau simulation per expected task and trial."""
    expected_ids = [str(task_id) for task_id in expected_task_ids]
    expected = {(task_id, trial) for task_id in expected_ids for trial in range(num_trials)}
    errors: list[str] = []
    observed: dict[tuple[str, int], int] = {}
    native_failures = 0
    simulation_ids: set[str] = set()

    if len(expected_ids) != len(set(expected_ids)):
        errors.append("expected task ids are not unique")
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = None
        errors.append("results.json is missing or invalid")
    simulations = data.get("simulations") if isinstance(data, dict) else None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    top_level_task_ids = (
        [str(task.get("id")) for task in tasks]
        if isinstance(tasks, list) and all(isinstance(task, dict) and isinstance(task.get("id"), (str, int)) and not isinstance(task.get("id"), bool) for task in tasks)
        else None
    )
    if top_level_task_ids is None or len(top_level_task_ids) != len(set(top_level_task_ids)) or set(top_level_task_ids) != set(expected_ids):
        errors.append("top-level task set does not match expected task set")
    run_info = data.get("info") if isinstance(data, dict) else None
    if expected_seed is not None and (
        not isinstance(run_info, dict) or run_info.get("seed") != expected_seed
    ):
        errors.append("configured seed does not match expected seed")
    trial_seeds: list[int] | None = None
    if expected_seed is not None:
        generator = random.Random(expected_seed)
        trial_seeds = [generator.randint(0, 1_000_000) for _ in range(num_trials)]
    if not isinstance(simulations, list):
        simulations = []
        if data is not None:
            errors.append("simulations is not a list")

    for index, simulation in enumerate(simulations):
        if not isinstance(simulation, dict):
            errors.append(f"simulation {index} is not an object")
            continue
        cell = _task_trial(simulation.get("task_id"), simulation.get("trial"))
        if cell is None:
            errors.append(f"simulation {index} has invalid task/trial identity")
        else:
            observed[cell] = observed.get(cell, 0) + 1
        if trial_seeds is not None and (
            cell is None or simulation.get("seed") != trial_seeds[cell[1]]
        ):
            errors.append(f"simulation {index} has unexpected seed")

        simulation_id = simulation.get("id")
        if not isinstance(simulation_id, str) or not simulation_id.strip():
            errors.append(f"simulation {index} has no id")
        elif simulation_id in simulation_ids:
            errors.append("duplicate simulation id")
        else:
            simulation_ids.add(simulation_id)

        messages = simulation.get("messages")
        if not isinstance(messages, list) or not messages:
            errors.append(f"simulation {index} has an empty conversation")
        elif any(
            not isinstance(message, dict)
            or (
                message.get("role") == "tool"
                and message.get("content") is None
                and message.get("error") is None
            )
            for message in messages
        ):
            errors.append(f"simulation {index} has an invalid tool result")

        reward_info = simulation.get("reward_info")
        reward = reward_info.get("reward") if isinstance(reward_info, dict) else None
        full_reward = (
            isinstance(reward_info, dict)
            and isinstance(reward, (int, float))
            and not isinstance(reward, bool)
            and math.isfinite(reward)
        )
        if not full_reward:
            errors.append("missing full reward_info")
        elif reward < 1:
            native_failures += 1

    observed_set = set(observed)
    duplicates = [
        {"task_id": task_id, "trial": trial}
        for (task_id, trial), count in sorted(observed.items())
        if count > 1
    ]
    missing = [
        {"task_id": task_id, "trial": trial}
        for task_id, trial in sorted(expected - observed_set)
    ]
    unexpected = [
        {"task_id": task_id, "trial": trial}
        for task_id, trial in sorted(observed_set - expected)
    ]
    if duplicates:
        errors.append("duplicate task/trial simulations")
    if require_complete and missing:
        errors.append("expected task/trial simulations are missing")
    if unexpected:
        errors.append("unexpected task/trial simulations are present")
    return {
        "ok": not errors,
        "simulation_count": len(simulations),
        "expected_simulation_count": len(expected),
        "native_failure_count": native_failures,
        "missing_task_trials": missing,
        "unexpected_task_trials": unexpected,
        "duplicate_task_trials": duplicates,
        "errors": errors,
    }


def _log_files(path: Path) -> list[Path]:
    return sorted(path.glob("task_*/sim_*/llm_debug/*.json")) if path.is_dir() else []


def summarize_llm_logs(
    path: Path,
    *,
    expected_routes: dict[str, dict[str, str]] | None = None,
    max_cost_usd: float | None = None,
    require_complete_routes: bool = True,
) -> dict[str, Any]:
    """Aggregate unique OpenRouter generations and validate complete call provenance."""
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    generations: dict[str, dict[str, Any]] = {}
    routes: dict[str, dict[str, set[str]]] = {}
    prompt_tokens = completion_tokens = total_tokens = 0
    total_cost = Decimal("0")

    files = _log_files(path)
    if not files:
        errors.append("LLM call logs are missing")
    for file in files:
        try:
            record = json.loads(file.read_text())
        except json.JSONDecodeError:
            errors.append(f"invalid LLM call log: {file}")
            continue
        request = record.get("request") if isinstance(record, dict) else None
        response = record.get("response") if isinstance(record, dict) else None
        raw = response.get("raw_response") if isinstance(response, dict) else None
        if not isinstance(request, dict) or not isinstance(raw, dict):
            errors.append(f"incomplete LLM call log: {file}")
            continue
        requested = request.get("model")
        route_tag = request.get("requested_provider_tag")
        response_model = request.get("response_model", request.get("expected_served_model"))
        endpoint_revision = request.get("endpoint_revision", response_model)
        generation_id = raw.get("id")
        served = raw.get("model")
        provider = raw.get("provider")
        choices = raw.get("choices")
        usage = raw.get("usage")
        if not all(isinstance(value, str) and value.strip() for value in (requested, route_tag, response_model, endpoint_revision, generation_id, served, provider)):
            errors.append(f"LLM call has incomplete provenance: {file}")
            continue
        if served != response_model:
            errors.append(f"response model does not match requested route for {requested}")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("finish_reason"), str):
            errors.append(f"LLM call has no finish reason: {file}")
        if not isinstance(usage, dict):
            errors.append(f"LLM call has no token/cost metadata: {file}")
            continue
        counts = [usage.get(field) for field in ("prompt_tokens", "completion_tokens", "total_tokens")]
        if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
            errors.append(f"LLM call has invalid token metadata: {file}")
            continue
        cost = _nonnegative_decimal(usage.get("cost"))
        if cost is None:
            errors.append(f"LLM call has invalid cost metadata: {file}")
            continue
        if generation_id in generations:
            errors.append(f"duplicate generation id: {generation_id}")
            continue
        generations[generation_id] = record
        records.append(record)
        prompt_tokens += counts[0]
        completion_tokens += counts[1]
        total_tokens += counts[2]
        total_cost += cost
        route = routes.setdefault(
            requested,
            {"served": set(), "providers": set(), "tags": set(), "endpoint_revisions": set()},
        )
        route["served"].add(served)
        route["providers"].add(provider)
        route["tags"].add(route_tag)
        route["endpoint_revisions"].add(endpoint_revision)

    for requested, route in sorted(routes.items()):
        if len(route["served"]) != 1:
            errors.append(f"mixed served models for requested model {requested}")
        if len(route["providers"]) != 1:
            errors.append(f"mixed response providers for requested model {requested}")
        if len(route["tags"]) != 1:
            errors.append(f"mixed routing tags for requested model {requested}")
    if expected_routes is not None:
        expected_models = set(expected_routes)
        observed_models = set(routes)
        if require_complete_routes and expected_models - observed_models:
            errors.append("expected requested model is missing")
        if observed_models - expected_models:
            errors.append("unexpected requested model")
        for requested in sorted(expected_models & observed_models):
            route = routes[requested]
            expected = expected_routes[requested]
            if route["tags"] != {expected["provider"]}:
                errors.append(f"routing tag mismatch for requested model {requested}")
            expected_response = expected.get("response_model", expected.get("expected_served_model"))
            expected_revision = expected.get("endpoint_revision", expected_response)
            if route["served"] != {expected_response}:
                errors.append(f"response model mismatch for requested model {requested}")
            if route["endpoint_revisions"] != {expected_revision}:
                errors.append(f"endpoint revision mismatch for requested model {requested}")
    if max_cost_usd is not None:
        ceiling = _nonnegative_decimal(max_cost_usd)
        if ceiling is None:
            raise ValueError("max_cost_usd must be finite and nonnegative")
        if total_cost > ceiling:
            errors.append("LLM cost exceeds configured ceiling")
    return {
        "ok": not errors,
        "generation_count": len(generations),
        "requested_models": sorted(routes),
        "routes": {
            model: {key: sorted(values) for key, values in route.items()}
            for model, route in sorted(routes.items())
        },
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": float(total_cost),
        "errors": errors,
    }


_CAPTURE_CALL_NAMES = {
    "agent_response",
    "user_simulator_response",
    "nl_assertions_eval",
}


def _capture_artifact_files(
    artifacts: Path,
) -> tuple[list[Path], list[str]]:
    """Return only canonical Tau call logs and reject every other JSON path."""
    logs: list[Path] = []
    errors: list[str] = []
    if not artifacts.is_dir():
        return logs, errors
    for file in sorted(artifacts.rglob("*.json")):
        relative = file.relative_to(artifacts)
        parts = relative.parts
        is_log = (
            len(parts) == 4
            and parts[0].startswith("task_")
            and parts[1].startswith("sim_")
            and parts[2] == "llm_debug"
        )
        is_status = (
            len(parts) == 3
            and parts[0].startswith("task_")
            and parts[1].startswith("sim_")
            and parts[2] == "sim_status.json"
        )
        if is_log:
            logs.append(file)
        elif not is_status:
            errors.append("unexpected artifact JSON path")
    return logs, errors


def validate_tau_capture(
    run_dir: Path,
    *,
    expected_task_ids: Iterable[str],
    expected_tasks: list[dict[str, Any]],
    expected_role_models: dict[str, str],
    expected_routes: dict[str, dict[str, str]],
    expected_seed: int,
    max_cost_usd: float,
) -> dict[str, Any]:
    """Reconcile a fresh Tau result trace to every canonical raw-response log."""
    result_validation = validate_tau_results(
        run_dir / "results.json",
        expected_task_ids,
        expected_seed=expected_seed,
    )
    errors = list(result_validation["errors"])
    try:
        data = json.loads((run_dir / "results.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    actual_tasks = data.get("tasks") if isinstance(data, dict) else None
    actual_tasks_by_id = {
        str(task.get("id")): task
        for task in actual_tasks or []
        if isinstance(task, dict)
    }
    expected_tasks_by_id = {
        str(task.get("id")): task
        for task in expected_tasks
        if isinstance(task, dict)
    }
    if actual_tasks_by_id != expected_tasks_by_id:
        errors.append("top-level task payloads do not match expected tasks")
    simulations = data.get("simulations") if isinstance(data, dict) else []
    if not isinstance(simulations, list):
        simulations = []

    final_by_path: dict[tuple[str, str], dict[str, Any]] = {}
    final_by_id: dict[str, dict[str, Any]] = {}
    for simulation in simulations:
        if not isinstance(simulation, dict):
            continue
        task_id = simulation.get("task_id")
        simulation_id = simulation.get("id")
        if isinstance(task_id, (str, int)) and isinstance(simulation_id, str):
            final_by_path[(str(task_id), simulation_id)] = simulation
            final_by_id[simulation_id] = simulation

    artifacts = run_dir / "artifacts"
    log_files, path_errors = _capture_artifact_files(artifacts)
    errors.extend(path_errors)
    call_summary = summarize_llm_logs(
        artifacts,
        expected_routes=expected_routes,
        max_cost_usd=max_cost_usd,
        require_complete_routes=False,
    )
    errors.extend(call_summary["errors"])

    calls_by_simulation: dict[str, Counter[str]] = {
        simulation_id: Counter() for simulation_id in final_by_id
    }
    logged_raw: dict[str, dict[str, dict[str, Any]]] = {
        simulation_id: {
            "agent_response": {},
            "user_simulator_response": {},
        }
        for simulation_id in final_by_id
    }
    total_calls: Counter[str] = Counter()
    all_generation_ids: list[str] = []
    required_role_models = {
        "agent_response",
        "user_simulator_response",
        "nl_assertions_eval",
    }
    if set(expected_role_models) != required_role_models:
        raise ValueError("expected_role_models must define agent, user, and judge")

    for file in log_files:
        relative = file.relative_to(artifacts)
        task_id = relative.parts[0][len("task_") :]
        simulation_id = relative.parts[1][len("sim_") :]
        simulation = final_by_path.get((task_id, simulation_id))
        if simulation is None:
            errors.append(f"LLM log is not bound to a final simulation: {relative}")
            continue
        try:
            record = json.loads(file.read_text())
        except json.JSONDecodeError:
            continue
        call_name = record.get("call_name") if isinstance(record, dict) else None
        if call_name not in _CAPTURE_CALL_NAMES:
            errors.append(f"unexpected capture call name: {call_name!r}")
            continue
        total_calls[call_name] += 1
        calls_by_simulation[simulation_id][call_name] += 1
        request = record.get("request") if isinstance(record, dict) else None
        response = record.get("response")
        raw = response.get("raw_response") if isinstance(response, dict) else None
        generation_id = raw.get("id") if isinstance(raw, dict) else None
        if isinstance(generation_id, str) and generation_id.strip():
            all_generation_ids.append(generation_id)
        expected_model = expected_role_models[call_name]
        requested_model = request.get("model") if isinstance(request, dict) else None
        if not isinstance(expected_model, str) or requested_model != expected_model:
            errors.append(f"{call_name} log does not use its expected role model")
        if call_name == "nl_assertions_eval":
            continue
        if not isinstance(generation_id, str) or not generation_id.strip():
            continue
        logged_raw[simulation_id][call_name][generation_id] = raw

    for simulation_id, simulation in final_by_id.items():
        traced_raw: dict[str, dict[str, dict[str, Any]]] = {
            "agent_response": {},
            "user_simulator_response": {},
        }
        messages = simulation.get("messages")
        if not isinstance(messages, list):
            continue
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") not in {
                "assistant",
                "user",
            }:
                continue
            raw = message.get("raw_data")
            if raw is None and index == 0 and message.get("role") == "assistant":
                continue
            if not isinstance(raw, dict):
                errors.append("participant message has no raw response")
                continue
            generation_id = raw.get("id")
            if not isinstance(generation_id, str) or not generation_id.strip():
                errors.append("trace raw response has no generation id")
                continue
            call_name = (
                "agent_response"
                if message["role"] == "assistant"
                else "user_simulator_response"
            )
            if generation_id in traced_raw[call_name]:
                errors.append(f"duplicate trace generation id: {generation_id}")
            traced_raw[call_name][generation_id] = raw

        mismatch = False
        for call_name in ("agent_response", "user_simulator_response"):
            traced = traced_raw[call_name]
            logged = logged_raw[simulation_id][call_name]
            if set(traced) != set(logged):
                mismatch = True
                continue
            if any(traced[generation_id] != logged[generation_id] for generation_id in traced):
                mismatch = True
        if mismatch:
            errors.append("final message/log generation IDs do not match")

    for file in sorted(artifacts.glob("task_*/sim_*/sim_status.json")):
        relative = file.relative_to(artifacts)
        key = (
            relative.parts[0][len("task_") :],
            relative.parts[1][len("sim_") :],
        )
        if key not in final_by_path:
            errors.append(f"artifact status is not bound to a final simulation: {relative}")

    for simulation_id, simulation in final_by_id.items():
        task = expected_tasks_by_id.get(str(simulation.get("task_id")))
        criteria = task.get("evaluation_criteria") if isinstance(task, dict) else None
        basis = criteria.get("reward_basis") if isinstance(criteria, dict) else None
        assertions = criteria.get("nl_assertions") if isinstance(criteria, dict) else None
        expected_judge_calls = int(
            simulation.get("termination_reason") in {"agent_stop", "user_stop"}
            and isinstance(basis, list)
            and "NL_ASSERTION" in basis
            and isinstance(assertions, list)
            and bool(assertions)
        )
        if calls_by_simulation[simulation_id].get("nl_assertions_eval", 0) != expected_judge_calls:
            errors.append("judge call count does not match frozen task semantics")

    for required_call in ("agent_response", "user_simulator_response"):
        if total_calls.get(required_call, 0) == 0:
            errors.append(f"required {required_call} route is missing")

    per_simulation = {
        simulation_id: {
            call_name: counts.get(call_name, 0)
            for call_name in sorted(_CAPTURE_CALL_NAMES)
        }
        for simulation_id, counts in sorted(calls_by_simulation.items())
    }
    call_counts = {
        call_name: total_calls.get(call_name, 0)
        for call_name in sorted(_CAPTURE_CALL_NAMES)
    }
    return {
        "ok": not errors,
        "errors": errors,
        "tau_results": result_validation,
        "llm_calls": call_summary,
        "call_counts": call_counts,
        "per_simulation": per_simulation,
        "generation_ids": sorted(all_generation_ids),
    }


def ensure_capture_lock(run_dir: Path, config: dict[str, Any]) -> str:
    """Bind an output directory to one immutable capture configuration."""
    lock_path = run_dir / "capture-lock.json"
    existing = list(run_dir.iterdir()) if run_dir.exists() else []
    if not lock_path.exists():
        if existing:
            raise RuntimeError("capture directory has content but no capture lock")
        run_dir.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
        return "created"
    try:
        locked = json.loads(lock_path.read_text())
    except json.JSONDecodeError as error:
        raise RuntimeError("capture lock is invalid JSON") from error
    if locked != config:
        raise RuntimeError("capture configuration drift")
    return "matched"
