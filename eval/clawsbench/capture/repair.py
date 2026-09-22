#!/usr/bin/env python3
"""Auditable repair and sealing for the completed ClawsBench Standard60 run."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CAPTURE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CAPTURE_DIR))
if not __package__:
    sys.path.insert(0, str(CAPTURE_DIR.parents[2]))

from eval.clawsbench.sealed_corpus import (  # noqa: E402
    attempt_directories,
    resolve_canonical_rollout,
)


def _load_capture_run() -> Any:
    """Load the sibling runner without colliding with analysis/run.py."""
    module_name = "clawsbench_capture_run"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, CAPTURE_DIR / "run.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the ClawsBench capture runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


run = _load_capture_run()


RUN_ROOT = Path(
    os.environ.get("CLAWSBENCH_CORPUS_ROOT", run.CLAWS_DIR / "corpus")
)
ORIGINAL_JOB = RUN_ROOT / "standard60_v1"
REPAIR_ROOT = RUN_ROOT / "repair"
REPAIR_JOB_NAME = "standard60_repair_v1"
REPAIR_TASKS = ("multi-doc-embedded-override",)
PHASE_DRIFT_PREFIX = "verifier mutated "
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def corrected_capture_validation(report: dict[str, Any]) -> dict[str, Any]:
    """Separate phase drift annotations from actual capture failures."""
    corrected = copy.deepcopy(report)
    original_errors = list(corrected.get("errors") or [])
    annotations = [
        error for error in original_errors if error.startswith(PHASE_DRIFT_PREFIX)
    ]
    corrected["errors"] = [
        error for error in original_errors if error not in annotations
    ]
    corrected["phase_drift_annotations"] = annotations
    corrected["ok"] = not corrected["errors"]
    return corrected


def classify_post_terminal_activity(
    terminal: dict[str, Any],
    post_verifier: dict[str, Any],
) -> dict[str, Any]:
    """Classify action-log entries added after the terminal snapshot."""
    terminal_entries = list(terminal.get("entries") or [])
    post_entries = list(post_verifier.get("entries") or [])
    if post_entries[: len(terminal_entries)] != terminal_entries:
        raise RuntimeError("post-verifier action log is not an extension of terminal log")
    new_entries = post_entries[len(terminal_entries) :]
    mutating = [
        entry
        for entry in new_entries
        if str(entry.get("method") or "").upper() in MUTATING_METHODS
    ]
    reads = [entry for entry in new_entries if entry not in mutating]
    return {
        "terminal_entry_count": len(terminal_entries),
        "post_verifier_entry_count": len(post_entries),
        "new_entry_count": len(new_entries),
        "read_entry_count": len(reads),
        "mutating_entry_count": len(mutating),
        "read_entries": reads,
        "mutating_entries": mutating,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_file(path: Path) -> str:
    return _sha256(path)


def execution_runner_sha256(path: Path) -> str:
    """Hash the runner as executed, ignoring only its later directory move."""
    source = path.read_text()
    resolver_import = (
        "if __package__:\n"
        "    from .paths import resolve_research_root\n"
        "else:\n"
        "    from paths import resolve_research_root\n\n\n"
    )
    if source.count(resolver_import) != 1:
        raise RuntimeError("cannot normalize the research-root resolver import")
    source = source.replace(resolver_import, "")
    relocated = (
        "CAPTURE_DIR = Path(__file__).resolve().parent\n"
        "CLAWS_DIR = CAPTURE_DIR.parent\n"
        "PROTOTYPE_ROOT = CLAWS_DIR.parents[1]\n"
        "RESEARCH_ROOT = resolve_research_root(PROTOTYPE_ROOT)\n"
        "BENCHFLOW_ROOT = RESEARCH_ROOT / \"benchflow\"\n"
        "ENV0_ROOT = RESEARCH_ROOT / \"env0\"\n"
        "META_PATH = CAPTURE_DIR / \"meta/assistant_v1.md\"\n"
        "CAPTURE_SCRIPT = CAPTURE_DIR / \"capture_runtime.py\"\n"
    )
    original = (
        "CLAWS_DIR = Path(__file__).resolve().parent\n"
        "PROTOTYPE_ROOT = CLAWS_DIR.parents[1]\n"
        "RESEARCH_ROOT = CLAWS_DIR.parents[2]\n"
        "BENCHFLOW_ROOT = RESEARCH_ROOT / \"benchflow\"\n"
        "ENV0_ROOT = RESEARCH_ROOT / \"env0\"\n"
        "META_PATH = CLAWS_DIR / \"meta/assistant_v1.md\"\n"
        "CAPTURE_SCRIPT = CLAWS_DIR / \"capture_runtime.py\"\n"
    )
    if source.count(relocated) != 1:
        raise RuntimeError("cannot normalize the relocated runner source")
    relocated_run_root = (
        "def _run_root(mode: str) -> Path:\n"
        "    return CLAWS_DIR / (\"corpus-smoke\" if mode == \"smoke\" else \"corpus\")\n"
    )
    original_run_root = (
        "def _run_root(mode: str) -> Path:\n"
        "    suffix = \"smoke\" if mode == \"smoke\" else \"standard60\"\n"
        "    return PROTOTYPE_ROOT / f\"eval/corpus/clawsbench_glm52_{suffix}\"\n"
    )
    normalized = source.replace(relocated, original)
    if normalized.count(relocated_run_root) != 1:
        raise RuntimeError("cannot normalize the relocated run root")
    normalized = normalized.replace(relocated_run_root, original_run_root)
    return hashlib.sha256(normalized.encode()).hexdigest()


def assert_base_run_lock(run_root: Path = RUN_ROOT) -> dict[str, Any]:
    lock_path = run_root / "run-lock.json"
    if not lock_path.is_file():
        raise RuntimeError(f"base run lock is missing: {lock_path}")
    lock = json.loads(lock_path.read_text())
    expected_runner = execution_runner_sha256(Path(run.__file__))
    if lock.get("runner_sha256") != expected_runner:
        raise RuntimeError(
            "locked runner does not match the relocated run.py after path normalization; "
            "repair must use the original execution source"
        )
    if (
        lock.get("task_count") != 60
        or lock.get("tasks") != run.load_standard60()
        or lock.get("model") != run.MODEL
    ):
        raise RuntimeError("base run lock is not the expected Standard60 GLM-5.2 run")
    return lock


def replay_email_ambiguous_verifier(
    rollout: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Replay the deterministic canonical verifier from terminal captures."""
    task = "email-ambiguous-cleanup"
    if not rollout.name.startswith(f"{task}__"):
        raise RuntimeError(f"unexpected verifier-replay rollout: {rollout}")
    input_dir = rollout / "artifacts/env0/terminal/mock-gmail"
    inputs = {
        "state": input_dir / "state.json",
        "diff": input_dir / "diff.json",
        "action_log": input_dir / "action_log.json",
    }
    for name, path in inputs.items():
        if not path.is_file():
            raise RuntimeError(f"missing verifier replay input {name}: {path}")
    evaluator = run.ENV0_ROOT / f"tasks/{task}/verifier/evaluate.py"
    output_dir.mkdir(parents=True, exist_ok=True)
    reward_path = output_dir / "reward.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(evaluator),
            "--state",
            str(inputs["state"]),
            "--diff",
            str(inputs["diff"]),
            "--action-log",
            str(inputs["action_log"]),
            "--output",
            str(reward_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not reward_path.is_file():
        raise RuntimeError(
            "email verifier replay failed: "
            f"{(completed.stderr or completed.stdout)[-2000:]}"
        )
    reward = json.loads(reward_path.read_text())
    replay = {
        "schema_version": 1,
        "ok": True,
        "task": task,
        "source_rollout": str(rollout),
        "evaluator": str(evaluator),
        "evaluator_sha256": _sha256(evaluator),
        "input_sha256": {name: _sha256(path) for name, path in inputs.items()},
        "reward": reward,
        "method": "canonical verifier replay from pre-verifier terminal captures",
    }
    run.atomic_json(output_dir / "replay.json", replay)
    return replay


def make_quiesced_recorded_rollout_class(base_class: type) -> type:
    """Return a recorded rollout that kills the agent before terminal capture."""

    class QuiescedRecordedRollout(base_class):
        async def verify(self):
            await self.disconnect()
            return await super().verify()

    QuiescedRecordedRollout.__name__ = "QuiescedRecordedRollout"
    return QuiescedRecordedRollout


def _single_rollout(job_dir: Path, task: str) -> Path:
    matches = sorted(job_dir.glob(f"{task}__*"))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one original rollout for {task}; found {matches}"
        )
    return matches[0]


def analyze_standard60(
    run_root: Path = RUN_ROOT,
    *,
    recovery_root: Path | None = None,
) -> dict[str, Any]:
    """Classify every original Standard60 attempt without changing it."""
    tasks = run.load_standard60()
    original_job = run_root / "standard60_v1"
    recovery_root = recovery_root or run_root / "repair/recovered-verifiers"
    accepted: dict[str, Any] = {}
    pending: list[str] = []
    pending_details: dict[str, Any] = {}
    phase_drift_tasks: list[str] = []
    mutation_tasks: list[str] = []
    recovered_tasks: list[str] = []

    for task in tasks:
        rollout = _single_rollout(original_job, task)
        task_dir = run.ENV0_ROOT / "tasks" / task
        base_report = run.validate_rollout_capture(
            rollout,
            expected_task=task,
            services=run.active_services(task_dir),
            expected_prompt=run.task_prompt(task_dir),
        )
        corrected = corrected_capture_validation(base_report)
        if corrected["phase_drift_annotations"]:
            phase_drift_tasks.append(task)

        post_terminal_activity: dict[str, Any] = {}
        for service in run.active_services(task_dir):
            terminal_log = (
                rollout
                / "artifacts/env0/terminal"
                / service
                / "action_log.json"
            )
            post_log = (
                rollout
                / "artifacts/env0/post_verifier"
                / service
                / "action_log.json"
            )
            if terminal_log.is_file() and post_log.is_file():
                activity = classify_post_terminal_activity(
                    json.loads(terminal_log.read_text()),
                    json.loads(post_log.read_text()),
                )
                post_terminal_activity[service] = activity
                if activity["mutating_entry_count"] and task not in mutation_tasks:
                    mutation_tasks.append(task)

        if corrected["ok"]:
            result = json.loads((rollout / "result.json").read_text())
            accepted[task] = {
                "source": "original",
                "rollout": str(rollout),
                "validation": corrected,
                "reward": (result.get("rewards") or {}).get("reward"),
                "post_terminal_activity": post_terminal_activity,
            }
            continue

        if task == "email-ambiguous-cleanup":
            expected_errors = {
                "missing verifier/reward.json",
                "result has no verifier reward",
                "result has verifier_error",
            }
            if set(corrected["errors"]) != expected_errors:
                raise RuntimeError(
                    f"unexpected email verifier failure shape: {corrected['errors']}"
                )
            replay = replay_email_ambiguous_verifier(
                rollout,
                recovery_root / task,
            )
            accepted[task] = {
                "source": "original_with_verifier_replay",
                "rollout": str(rollout),
                "validation": {
                    **corrected,
                    "ok": True,
                    "errors": [],
                    "recovered_errors": sorted(expected_errors),
                },
                "reward": replay["reward"]["reward"],
                "verifier_replay": replay,
                "post_terminal_activity": post_terminal_activity,
            }
            recovered_tasks.append(task)
            continue

        if task in REPAIR_TASKS:
            pending.append(task)
            pending_details[task] = {
                "rollout": str(rollout),
                "validation": corrected,
            }
            continue

        raise RuntimeError(f"unclassified invalid task {task}: {corrected['errors']}")

    return {
        "schema_version": 1,
        "task_count": len(tasks),
        "accepted_original_count": len(accepted),
        "accepted_original": accepted,
        "pending_rerun": pending,
        "pending_details": pending_details,
        "phase_drift_tasks": sorted(phase_drift_tasks),
        "post_terminal_mutation_tasks": sorted(mutation_tasks),
        "recovered_verifier_tasks": sorted(recovered_tasks),
    }


def _parse_coverage(value: Any) -> tuple[int, int]:
    if not isinstance(value, str) or "/" not in value:
        raise RuntimeError(f"invalid cost coverage: {value!r}")
    priced_text, requested_text = value.split("/", 1)
    return int(priced_text), int(requested_text)


def collect_attempted_cost(run_root: Path = RUN_ROOT) -> dict[str, Any]:
    """Sum provider cost across original and repair attempts."""
    attempts: list[dict[str, Any]] = []
    for job_name in ("standard60_v1", REPAIR_JOB_NAME):
        job_dir = run_root / job_name
        if not job_dir.is_dir():
            continue
        for rollout in sorted(path for path in job_dir.iterdir() if path.is_dir()):
            stats_path = rollout / "artifacts/openrouter-generations.json"
            if not stats_path.is_file():
                attempts.append(
                    {
                        "rollout": str(rollout),
                        "cost_usd": None,
                        "cost_coverage": "0/0",
                        "cost_complete": False,
                    }
                )
                continue
            stats = json.loads(stats_path.read_text())
            attempts.append(
                {
                    "rollout": str(rollout),
                    "cost_usd": stats.get("total_cost_usd"),
                    "cost_coverage": stats.get("cost_coverage", "0/0"),
                    "cost_complete": stats.get("cost_complete"),
                }
            )
    priced_generations = 0
    requested_generations = 0
    total_cost = 0.0
    priced_attempts = 0
    zero_generation_attempts = 0
    for attempt in attempts:
        priced, requested = _parse_coverage(attempt["cost_coverage"])
        priced_generations += priced
        requested_generations += requested
        value = attempt["cost_usd"]
        if isinstance(value, (int, float)):
            total_cost += float(value)
        if requested == 0:
            zero_generation_attempts += 1
        elif attempt["cost_complete"] is True and priced == requested:
            priced_attempts += 1
    return {
        "attempt_count": len(attempts),
        "priced_attempt_count": priced_attempts,
        "zero_generation_attempt_count": zero_generation_attempts,
        "total_cost_usd": round(total_cost, 8),
        "cost_coverage": f"{priced_generations}/{requested_generations}",
        "total_cost_is_lower_bound": priced_generations != requested_generations,
        "attempts": attempts,
    }


def assemble_seal(
    analysis: dict[str, Any],
    repair_candidates: dict[str, Any],
    *,
    attempted_cost: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a fail-closed canonical selection without editing attempts."""
    expected_tasks = run.load_standard60()
    candidates = dict(analysis["accepted_original"])
    overlap = set(candidates) & set(repair_candidates)
    if overlap:
        raise RuntimeError(f"repair candidates overlap accepted originals: {overlap}")
    candidates.update(repair_candidates)
    if set(candidates) != set(expected_tasks):
        missing = sorted(set(expected_tasks) - set(candidates))
        extra = sorted(set(candidates) - set(expected_tasks))
        raise RuntimeError(f"cannot seal corpus; missing={missing}, extra={extra}")

    tasks: dict[str, Any] = {}
    canonical_cost = 0.0
    priced_generations = 0
    requested_generations = 0
    canonical_cost_complete = True
    for task in expected_tasks:
        candidate = candidates[task]
        validation = candidate["validation"]
        if validation.get("ok") is not True:
            raise RuntimeError(f"canonical candidate is invalid for {task}")
        cost = validation.get("cost_usd")
        if not isinstance(cost, (int, float)):
            raise RuntimeError(f"canonical candidate has no cost for {task}")
        priced, requested = _parse_coverage(validation.get("cost_coverage"))
        canonical_cost += float(cost)
        priced_generations += priced
        requested_generations += requested
        if validation.get("cost_complete") is not True or priced != requested:
            canonical_cost_complete = False
        tasks[task] = {
            "task": task,
            "source": candidate["source"],
            "rollout": candidate["rollout"],
            "reward": candidate["reward"],
            "cost_usd": float(cost),
            "cost_complete": validation.get("cost_complete"),
            "cost_coverage": validation.get("cost_coverage"),
            "phase_drift_annotations": validation.get(
                "phase_drift_annotations", []
            ),
            "post_terminal_activity": candidate.get(
                "post_terminal_activity", {}
            ),
            **(
                {"verifier_replay": candidate["verifier_replay"]}
                if candidate.get("verifier_replay")
                else {}
            ),
        }

    return {
        "schema_version": 1,
        "benchmark": "ClawsBench Standard60",
        "model": run.MODEL,
        "task_count": len(expected_tasks),
        "canonical_count": len(tasks),
        "tasks": tasks,
        "canonical_cost_usd": round(canonical_cost, 8),
        "canonical_cost_coverage": (
            f"{priced_generations}/{requested_generations}"
        ),
        "canonical_cost_is_lower_bound": not canonical_cost_complete,
        "attempted_cost": attempted_cost,
        "phase_drift_tasks": analysis["phase_drift_tasks"],
        "post_terminal_mutation_tasks": analysis[
            "post_terminal_mutation_tasks"
        ],
        "recovered_verifier_tasks": analysis["recovered_verifier_tasks"],
        "repair_rerun_tasks": sorted(repair_candidates),
        "excluded_original_attempts": analysis["pending_details"],
        "selection_policy": (
            "Preserve every complete original trajectory; replay deterministic "
            "verifiers from captured terminal state; rerun only attempts that "
            "never reached a usable agent execution."
        ),
    }


def validate_seal(
    seal: dict[str, Any],
    *,
    require_paths: bool = True,
    run_root: Path | None = None,
) -> dict[str, Any]:
    expected = run.load_standard60()
    tasks = seal.get("tasks")
    if not isinstance(tasks, dict) or set(tasks) != set(expected):
        raise RuntimeError("seal task set does not equal Standard60")
    if seal.get("task_count") != 60 or seal.get("canonical_count") != 60:
        raise RuntimeError("seal does not contain 60 canonical tasks")
    rollout_paths = [str(tasks[task].get("rollout") or "") for task in expected]
    if len(set(rollout_paths)) != len(rollout_paths):
        raise RuntimeError("duplicate canonical rollout in seal")
    if require_paths:
        root = Path(run_root if run_root is not None else RUN_ROOT).resolve()
        attempts = attempt_directories(root)
        attempt_roots = {path.resolve() for path in attempts}
        for task in expected:
            resolve_canonical_rollout(
                root,
                task,
                tasks[task],
                attempt_roots,
            )
    for task in expected:
        entry = tasks[task]
        if entry.get("task") != task:
            raise RuntimeError(f"seal task identity mismatch for {task}")
        if not isinstance(entry.get("reward"), (int, float)):
            raise RuntimeError(f"seal reward is absent for {task}")
        if not isinstance(entry.get("cost_usd"), (int, float)):
            raise RuntimeError(f"seal cost is absent for {task}")
        if entry.get("cost_complete") is not True:
            raise RuntimeError(f"seal cost is incomplete for {task}")
        priced, requested = _parse_coverage(entry.get("cost_coverage"))
        if priced != requested:
            raise RuntimeError(f"seal cost coverage is incomplete for {task}")
    return {
        "ok": True,
        "task_count": len(tasks),
        "unique_rollout_count": len(set(rollout_paths)),
    }


def _repair_candidates() -> dict[str, Any]:
    candidates: dict[str, Any] = {}
    job_dir = RUN_ROOT / REPAIR_JOB_NAME
    if not job_dir.is_dir():
        return candidates
    for task in REPAIR_TASKS:
        valid: list[tuple[Path, dict[str, Any]]] = []
        for rollout in sorted(job_dir.glob(f"{task}__*")):
            report = validate_repair_rollout(rollout)
            if report["ok"]:
                valid.append((rollout, report))
        if len(valid) > 1:
            raise RuntimeError(f"multiple valid repair candidates for {task}")
        if valid:
            rollout, report = valid[0]
            candidates[task] = {
                "source": "repair_rerun",
                "rollout": str(rollout),
                "validation": report,
                "reward": report["reward"],
                "post_terminal_activity": report["post_terminal_activity"],
            }
    return candidates


def seal_corpus() -> dict[str, Any]:
    base_lock = assert_base_run_lock()
    repair_lock_path = REPAIR_ROOT / "repair-lock.json"
    if not repair_lock_path.is_file():
        raise RuntimeError("repair lock is missing; run --run-missing first")
    run.ensure_run_lock(repair_lock_path, _repair_spec(base_lock))
    analysis = analyze_standard60(
        RUN_ROOT,
        recovery_root=REPAIR_ROOT / "recovered-verifiers",
    )
    repair_candidates = _repair_candidates()
    seal = assemble_seal(
        analysis,
        repair_candidates,
        attempted_cost=collect_attempted_cost(RUN_ROOT),
    )
    seal["sealed_at"] = run.utc_now()
    seal["provenance"] = {
        "base_run_lock": str(RUN_ROOT / "run-lock.json"),
        "base_run_lock_sha256": _sha256(RUN_ROOT / "run-lock.json"),
        "repair_lock": str(repair_lock_path),
        "repair_lock_sha256": _sha256(repair_lock_path),
        "repair_script": str(Path(__file__)),
        "repair_script_sha256": _sha256(Path(__file__)),
        "standard60_manifest_sha256": run.STANDARD60_SHA256,
        "meta_sha256": run.META_SHA256,
    }
    validation = validate_seal(seal)
    seal_path = RUN_ROOT / "sealed-corpus.json"
    run.atomic_json(seal_path, seal)
    rewards = [float(entry["reward"]) for entry in seal["tasks"].values()]
    summary = {
        "schema_version": 1,
        "ok": True,
        "task_count": 60,
        "canonical_count": 60,
        "mean_reward": round(sum(rewards) / len(rewards), 8),
        "canonical_cost_usd": seal["canonical_cost_usd"],
        "canonical_cost_coverage": seal["canonical_cost_coverage"],
        "attempt_count": seal["attempted_cost"]["attempt_count"],
        "attempted_cost_usd": seal["attempted_cost"]["total_cost_usd"],
        "recovered_verifier_tasks": seal["recovered_verifier_tasks"],
        "repair_rerun_tasks": seal["repair_rerun_tasks"],
        "post_terminal_mutation_tasks": seal[
            "post_terminal_mutation_tasks"
        ],
        "seal": str(seal_path),
        "seal_sha256": _sha256(seal_path),
        "validation": validation,
        "sealed_at": seal["sealed_at"],
    }
    run.atomic_json(RUN_ROOT / "sealed-summary.json", summary)
    run.atomic_json(
        REPAIR_ROOT / "seal-validation.json",
        {
            **validation,
            "seal": str(seal_path),
            "seal_sha256": _sha256(seal_path),
        },
    )
    return summary


def dry_run_plan() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="clawsbench-repair-dry-run-") as temporary:
        analysis = analyze_standard60(
            RUN_ROOT,
            recovery_root=Path(temporary) / "recovery",
        )
    return {
        "mode": "standard60_repair",
        "accepted_original_count": analysis["accepted_original_count"],
        "pending_rerun": analysis["pending_rerun"],
        "model": run.MODEL,
        "concurrency": 1,
        "quiesce_before_terminal_capture": True,
        "would_start_docker": False,
        "would_call_openrouter": False,
    }


def _repair_spec(base_lock: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "purpose": "replace attempts that never reached a usable agent rollout",
        "base_run_lock_sha256": _sha256(RUN_ROOT / "run-lock.json"),
        "base_runner_sha256": base_lock["runner_sha256"],
        "repair_script_sha256": _sha256(Path(__file__)),
        "tasks": list(REPAIR_TASKS),
        "model": run.MODEL,
        "agent": "openclaw",
        "concurrency": 1,
        "automatic_retries": 0,
        "quiesce_before_terminal_capture": True,
        "meta_sha256": run.META_SHA256,
        "skill_mode": "no-skill",
    }


def validate_repair_rollout(rollout: Path) -> dict[str, Any]:
    task = rollout.name.split("__", 1)[0]
    if task not in REPAIR_TASKS:
        raise RuntimeError(f"unexpected repair task: {task}")
    task_dir = run.ENV0_ROOT / "tasks" / task
    base_report = run.validate_rollout_capture(
        rollout,
        expected_task=task,
        services=run.active_services(task_dir),
        expected_prompt=run.task_prompt(task_dir),
    )
    corrected = corrected_capture_validation(base_report)
    activity: dict[str, Any] = {}
    for service in run.active_services(task_dir):
        terminal_log = (
            rollout / "artifacts/env0/terminal" / service / "action_log.json"
        )
        post_log = (
            rollout
            / "artifacts/env0/post_verifier"
            / service
            / "action_log.json"
        )
        if terminal_log.is_file() and post_log.is_file():
            service_activity = classify_post_terminal_activity(
                json.loads(terminal_log.read_text()),
                json.loads(post_log.read_text()),
            )
            activity[service] = service_activity
            if service_activity["mutating_entry_count"]:
                corrected["errors"].append(
                    f"post-terminal mutating activity in {service}"
                )
    corrected["post_terminal_activity"] = activity
    corrected["ok"] = not corrected["errors"]
    run.atomic_json(rollout / "repair-validation.json", corrected)
    return corrected


def _valid_repair_rollouts(openrouter_key: str) -> list[Path]:
    job_dir = RUN_ROOT / REPAIR_JOB_NAME
    if not job_dir.is_dir():
        return []
    valid: list[Path] = []
    for rollout in sorted(job_dir.glob(f"{REPAIR_TASKS[0]}__*")):
        run.reconcile_openrouter_generations(rollout, openrouter_key)
        if validate_repair_rollout(rollout)["ok"]:
            valid.append(rollout)
    if len(valid) > 1:
        raise RuntimeError(f"multiple valid repair rollouts exist: {valid}")
    return valid


async def _run_missing_async(openrouter_key: str) -> dict[str, Any]:
    from benchflow.environment.manifest import load_manifest
    from benchflow.sandbox.docker import DockerSandbox

    run.register_pinned_openclaw()
    base_recorded = run.make_recorded_rollout_class()
    quiesced_recorded = make_quiesced_recorded_rollout_class(base_recorded)
    DockerSandbox.set_build_concurrency(1)
    environment_manifest = load_manifest(run.ENVIRONMENT_MANIFEST)

    valid = _valid_repair_rollouts(openrouter_key)
    if not valid:
        await run._run_task(
            task_name=REPAIR_TASKS[0],
            run_root=RUN_ROOT,
            job_name=REPAIR_JOB_NAME,
            environment_manifest=environment_manifest,
            RecordedRollout=quiesced_recorded,
            openrouter_key=openrouter_key,
            concurrency=1,
        )
        valid = _valid_repair_rollouts(openrouter_key)
    if len(valid) != 1:
        raise RuntimeError(
            "targeted repair did not produce one capture-valid rollout; "
            "rerun the same repair command to try only the missing task"
        )
    report = json.loads((valid[0] / "repair-validation.json").read_text())
    summary = {
        "schema_version": 1,
        "task_count": 1,
        "valid_count": 1,
        "task": REPAIR_TASKS[0],
        "rollout": str(valid[0]),
        "reward": report.get("reward"),
        "cost_usd": report.get("cost_usd"),
        "cost_complete": report.get("cost_complete"),
        "cost_coverage": report.get("cost_coverage"),
        "quiesced_before_terminal_capture": True,
    }
    run.atomic_json(REPAIR_ROOT / "repair-summary.json", summary)
    return summary


def run_missing() -> dict[str, Any]:
    base_lock = assert_base_run_lock()
    analysis = analyze_standard60(
        RUN_ROOT,
        recovery_root=REPAIR_ROOT / "recovered-verifiers",
    )
    if analysis["pending_rerun"] != list(REPAIR_TASKS):
        raise RuntimeError(
            f"repair target drift: expected {REPAIR_TASKS}, "
            f"found {analysis['pending_rerun']}"
        )
    run._assert_local_benchflow()
    run._docker_preflight()
    run.configure_logging(REPAIR_ROOT)
    run._ensure_base_image_identity(RUN_ROOT / "run-lock.json")
    repair_lock = _repair_spec(base_lock)
    status = run.ensure_run_lock(REPAIR_ROOT / "repair-lock.json", repair_lock)
    key = run.load_openrouter_key()
    provider = run.openrouter_preflight(key)
    run.atomic_json(REPAIR_ROOT / "openrouter-preflight.json", provider)
    print(
        f"Repair lock {status}; OpenRouter remaining "
        f"${provider['remaining_usd']:.2f}",
        flush=True,
    )
    summary = asyncio.run(_run_missing_async(key))
    leaked = run.find_secret_occurrences(REPAIR_ROOT, key)
    if leaked:
        raise RuntimeError(f"OpenRouter key leaked into repair artifacts: {leaked}")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-missing", action="store_true")
    parser.add_argument("--seal", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        print(json.dumps(dry_run_plan(), indent=2))
        return 0
    if args.run_missing:
        print(json.dumps(run_missing(), indent=2), flush=True)
        return 0
    if args.seal:
        print(json.dumps(seal_corpus(), indent=2), flush=True)
        return 0
    raise RuntimeError("specify --dry-run, --run-missing, or --seal")


if __name__ == "__main__":
    sys.exit(main())
