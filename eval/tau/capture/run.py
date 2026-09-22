#!/usr/bin/env python3
"""Run fail-closed Tau screen batches with exact OpenRouter provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .integrity import (
    ensure_capture_lock,
    git_commit,
    validate_tau_capture,
)


ROOT = Path(
    os.environ.get("TAU2_BENCH_ROOT", Path.home() / "research" / "tau2-explore")
)
TAU2 = str(ROOT / ".venv" / "bin" / "tau2")
PROTOTYPE_ROOT = Path(__file__).resolve().parents[3]
USER_MODEL = "openai/gpt-4.1-mini"
USER_ENDPOINT_REVISION = "openai/gpt-4.1-mini-2025-04-14"
USER_PROVIDER = "openai"
JUDGE_MODEL = "openai/gpt-4.1-2025-04-14"
JUDGE_RESPONSE_MODEL = "openai/gpt-4.1"
JUDGE_ENDPOINT_REVISION = "openai/gpt-4.1-2025-04-14"
JUDGE_PROVIDER = "openai"


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _key() -> str:
    _load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("set OPENROUTER_API_KEY")
    return key


def _curl(url: str) -> dict[str, Any]:
    result = subprocess.run(
        ["curl", "-fsS", url, "-H", f"Authorization: Bearer {_key()}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"OpenRouter usage request failed with status {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("OpenRouter usage response is invalid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError("OpenRouter usage response is not an object")
    return data


def usage() -> float:
    """Read account usage for provenance, never for exact batch-cost accounting."""
    value = _curl("https://openrouter.ai/api/v1/credits").get("data", {}).get(
        "total_usage"
    )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeError("OpenRouter account usage is unavailable")
    return float(value)


def capture_routes(
    *,
    agent: str,
    provider: str,
    user: str,
    endpoint_revision: str | None = None,
    expected_served_model: str | None = None,
) -> dict[str, dict[str, str]]:
    """Return the exact route map enforced by Tau's shared generate() choke point."""
    if user != USER_MODEL:
        raise ValueError(f"screen user model must be {USER_MODEL}")
    revision = endpoint_revision or expected_served_model
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("endpoint revision must be a nonempty string")
    routes: dict[str, dict[str, str]] = {}
    role_routes = (
        (agent, agent, provider, revision),
        (user, user, USER_PROVIDER, USER_ENDPOINT_REVISION),
        (JUDGE_MODEL, JUDGE_RESPONSE_MODEL, JUDGE_PROVIDER, JUDGE_ENDPOINT_REVISION),
    )
    for model, response_model, route_provider, route_revision in role_routes:
        requested_model = f"openrouter/{model}"
        route = {
            "provider": route_provider,
            "response_model": response_model,
            "endpoint_revision": route_revision,
        }
        if requested_model in routes and routes[requested_model] != route:
            raise ValueError(f"conflicting capture routes for {requested_model}")
        routes[requested_model] = route
    return routes


def build_tau_command(
    *,
    domain: str,
    agent: str,
    user: str,
    task_ids: list[str],
    seed: int,
    max_concurrency: int,
    save: str,
    timeout: float,
) -> list[str]:
    """Build the noninteractive, one-trial, zero-task-retry Tau invocation."""
    if not task_ids:
        raise ValueError("task_ids must be explicit and nonempty")
    return [
        TAU2,
        "run",
        "--domain",
        domain,
        "--agent",
        "llm_agent",
        "--agent-llm",
        f"openrouter/{agent}",
        "--user",
        "user_simulator",
        "--user-llm",
        f"openrouter/{user}",
        "--task-ids",
        *task_ids,
        "--num-trials",
        "1",
        "--seed",
        str(seed),
        "--max-concurrency",
        str(max_concurrency),
        "--timeout",
        str(timeout),
        "--max-retries",
        "0",
        "--verbose-logs",
        "--llm-log-mode",
        "all",
        "--log-level",
        "WARNING",
        "--save-to",
        save,
    ]


def run_tau_process(
    command: list[str], log_path: Path, *, env: dict[str, str] | None = None
) -> None:
    """Stream Tau output and fail before validation on any nonzero exit."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        command,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    with log_path.open("w") as log_file:
        for line in process.stdout:
            log_file.write(line)
            print(line, end="", flush=True)
    status = process.wait()
    if status:
        raise RuntimeError(f"tau2 exited with status {status}")


def _runner_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _expected_tasks(domain: str, task_ids: list[str]) -> list[dict[str, Any]]:
    task_path = ROOT / "data" / "tau2" / "domains" / domain / "tasks.json"
    tasks = json.loads(task_path.read_text())
    if not isinstance(tasks, list):
        raise RuntimeError("Tau task file is not a list")
    by_id = {
        str(task.get("id")): task
        for task in tasks
        if isinstance(task, dict) and task.get("id") is not None
    }
    if len(task_ids) != len(set(task_ids)) or any(str(task_id) not in by_id for task_id in task_ids):
        raise ValueError("task_ids must be unique frozen Tau tasks")
    selected = [by_id[str(task_id)] for task_id in task_ids]
    script = (
        "import json,sys; "
        "from tau2.data_model.tasks import Task; "
        "raw=json.load(sys.stdin); "
        "json.dump([Task.model_validate(task).model_dump(mode='json') for task in raw],sys.stdout)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    canonical = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), "-c", script],
        input=json.dumps(selected),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=30,
    )
    if canonical.returncode:
        raise RuntimeError("Tau task canonicalization failed")
    try:
        tasks = json.loads(canonical.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Tau task canonicalization returned invalid JSON") from error
    if not isinstance(tasks, list):
        raise RuntimeError("Tau task canonicalization did not return a list")
    return tasks


def _task_payload_sha256(tasks: list[dict[str, Any]]) -> str:
    payload = json.dumps(tasks, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def capture_batch(
    *,
    domain: str,
    agent: str,
    provider: str,
    expected_served_model: str,
    user: str,
    task_ids: list[str],
    seed: int,
    max_concurrency: int,
    timeout: float,
    max_cost_usd: float,
    outdir: Path,
) -> dict[str, Any]:
    """Capture, validate, and only then write a passing Tau batch manifest."""
    if max_concurrency != 1:
        raise ValueError("cost-bounded capture requires max_concurrency=1")
    if outdir.exists() and any(outdir.iterdir()):
        raise RuntimeError("capture requires a fresh output directory")
    routes = capture_routes(
        agent=agent,
        provider=provider,
        endpoint_revision=expected_served_model,
        user=user,
    )
    expected_tasks = _expected_tasks(domain, task_ids)
    role_models = {
        "agent_response": f"openrouter/{agent}",
        "user_simulator_response": f"openrouter/{user}",
        "nl_assertions_eval": f"openrouter/{JUDGE_MODEL}",
    }
    tau2_commit = git_commit(ROOT)
    prototype_commit = git_commit(PROTOTYPE_ROOT)
    runner_sha256 = _runner_sha256()
    config = {
        "agent": agent,
        "domain": domain,
        "response_model": agent,
        "endpoint_revision": expected_served_model,
        "max_concurrency": max_concurrency,
        "max_cost_usd": max_cost_usd,
        "num_trials": 1,
        "provider": provider,
        "prototype_commit": prototype_commit,
        "routes": routes,
        "runner_sha256": runner_sha256,
        "seed": seed,
        "task_ids": task_ids,
        "task_payload_sha256": _task_payload_sha256(expected_tasks),
        "tau2_commit": tau2_commit,
        "timeout": timeout,
        "user": user,
    }
    ensure_capture_lock(outdir, config)

    simulations_root = ROOT / "data" / "simulations"
    try:
        save = str(outdir.relative_to(simulations_root))
    except ValueError:
        save = str(outdir)
    command = build_tau_command(
        domain=domain,
        agent=agent,
        user=user,
        task_ids=task_ids,
        seed=seed,
        max_concurrency=max_concurrency,
        save=save,
        timeout=timeout,
    )
    account_usage_before = usage()
    started = time.time()
    env = os.environ.copy()
    env["TAU2_CAPTURE_ROUTES"] = json.dumps(routes, sort_keys=True)
    env["TAU2_CAPTURE_MAX_COST_USD"] = str(max_cost_usd)
    env["TAU2_CAPTURE_COST_SPENT_USD"] = "0"
    run_tau_process(command, outdir / "tau2.log", env=env)
    account_usage_after = usage()

    validation = validate_tau_capture(
        outdir,
        expected_task_ids=task_ids,
        expected_tasks=expected_tasks,
        expected_role_models=role_models,
        expected_routes=routes,
        expected_seed=seed,
        max_cost_usd=max_cost_usd,
    )
    results = validation["tau_results"]
    calls = validation["llm_calls"]
    errors = validation["errors"]
    if errors:
        raise RuntimeError("Tau batch integrity failed: " + "; ".join(errors))
    manifest = {
        "status": "PASS",
        "capture_config": config,
        "command": command,
        "tau2_commit": tau2_commit,
        "prototype_commit": prototype_commit,
        "runner_sha256": runner_sha256,
        "account_usage_before": account_usage_before,
        "account_usage_after": account_usage_after,
        "account_usage_delta": account_usage_after - account_usage_before,
        "account_usage_is_batch_cost": False,
        "exact_llm_cost_source": "unique generation IDs in complete llm_debug logs",
        "llm_calls": calls,
        "call_counts": validation["call_counts"],
        "per_simulation_calls": validation["per_simulation"],
        "generation_ids": validation["generation_ids"],
        "tau_results": results,
        "wall_seconds": round(time.time() - started, 3),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--endpoint-revision", required=True)
    parser.add_argument("--user", default=USER_MODEL)
    parser.add_argument("--domains", default="airline,retail")
    parser.add_argument("--seed", type=int, default=300)
    parser.add_argument("--task-ids", required=True)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--max-cost-usd", type=float, required=True)
    parser.add_argument("--run-id", required=True)
    arguments = parser.parse_args()

    task_ids = [task.strip() for task in arguments.task_ids.split(",") if task.strip()]
    domains = [domain.strip() for domain in arguments.domains.split(",") if domain.strip()]
    if len(domains) != 1:
        raise SystemExit("run exactly one domain batch so each cost ceiling is enforced independently")
    outdir = ROOT / "data" / "simulations" / arguments.run_id / f"{domains[0]}.json"
    manifest = capture_batch(
        domain=domains[0],
        agent=arguments.agent,
        provider=arguments.provider,
        expected_served_model=arguments.endpoint_revision,
        user=arguments.user,
        task_ids=task_ids,
        seed=arguments.seed,
        max_concurrency=arguments.max_concurrency,
        timeout=arguments.timeout,
        max_cost_usd=arguments.max_cost_usd,
        outdir=outdir,
    )
    print(
        f"[PASS] simulations={manifest['tau_results']['simulation_count']} "
        f"cost=${manifest['llm_calls']['cost_usd']:.6f} -> {outdir}",
        flush=True,
    )


if __name__ == "__main__":
    main()
