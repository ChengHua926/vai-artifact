#!/usr/bin/env python3
"""Build small, local-only ClawsBench viewer manifests.

The sealed corpus remains the source of truth. This exporter does not copy raw
artifacts: it inventories them, normalizes the small records needed for the
primary UI, and leaves artifact bodies to the bounded localhost API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from eval.clawsbench.sealed_corpus import (
    attempt_directories,
    resolve_canonical_rollout,
)


HERE = Path(__file__).resolve().parent
CLAWS_DIR = HERE.parent
EVAL_ROOT = CLAWS_DIR.parent
DEFAULT_RUN_ROOT = CLAWS_DIR / "corpus"
DEFAULT_OUT = CLAWS_DIR / "data"
DEFAULT_PUBLISHED_SAMPLE_OUT = (
    EVAL_ROOT / "viewer/app/server-data/clawsbench"
)
PHASES = ("initial", "terminal", "post_verifier")
PUBLISHED_SAMPLE_TASKS = (
    "auth-app-install-scope-eval",
    "email-workflow-cleanup-and-report",
    "multi-doc-slack-spec-drift",
    "slack-channel-reorg",
    "stripe-refund-correct-customer",
)


def configured_secrets(*env_paths: Path) -> list[str]:
    """Collect configured credentials without ever logging their values."""
    values: list[str] = []
    candidates = list(os.environ.items())
    for env_path in env_paths:
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            candidates.append((name, value.strip().strip("\"'")))
    for name, value in candidates:
        if (
            any(
                token in name.upper()
                for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            )
            and len(value) >= 16
            and not value.startswith(("$", "<", "{"))
        ):
            values.append(value)
    return sorted(set(values))


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse JSONL without silently dropping malformed evidence."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        path.read_text(errors="replace").splitlines(), start=1
    ):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            records.append(
                {
                    "_malformed": True,
                    "line": line_number,
                    "raw": raw,
                    "error": "invalid JSON",
                }
            )
            continue
        records.append(
            value
            if isinstance(value, dict)
            else {"_value": value, "line": line_number}
        )
    return records


def provider_call_summaries(path: Path) -> list[dict[str, Any]]:
    """Keep one compact row per provider exchange; raw bodies remain artifacts."""
    if not path.is_file():
        return []
    summaries: list[dict[str, Any]] = []
    with path.open(errors="replace") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                summaries.append(
                    {
                        "line": line_number,
                        "_malformed": True,
                        "error": "invalid JSON",
                    }
                )
                continue
            request = value.get("request") or {}
            response = value.get("response") or {}
            request_body = request.get("body") or {}
            response_body = response.get("body") or {}
            choices = response_body.get("choices") or []
            first_choice = choices[0] if choices else {}
            summaries.append(
                {
                    "line": line_number,
                    "request_timestamp": request.get("timestamp"),
                    "response_timestamp": response.get("timestamp"),
                    "model": request_body.get("model"),
                    "status_code": response.get("status_code"),
                    "duration_ms": value.get("duration_ms"),
                    "finish_reason": first_choice.get("finish_reason"),
                    "usage": response_body.get("usage"),
                }
            )
    return summaries


def first_provider_system_message(path: Path) -> str:
    """Return the actual system-role content sent on the first provider call."""
    if not path.is_file():
        return ""
    with path.open(errors="replace") as handle:
        for raw in handle:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            request_body = ((value.get("request") or {}).get("body") or {})
            for message in request_body.get("messages") or []:
                if message.get("role") != "system":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "\n".join(
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
            return ""
    return ""


def phase_activity(
    initial: dict[str, Any] | None,
    terminal: dict[str, Any] | None,
    post_verifier: dict[str, Any] | None,
) -> dict[str, Any]:
    """Derive phase deltas only when later logs extend earlier logs exactly."""
    initial_entries = list((initial or {}).get("entries") or [])
    terminal_entries = list((terminal or {}).get("entries") or [])
    post_entries = list((post_verifier or {}).get("entries") or [])
    initial_ok = terminal_entries[: len(initial_entries)] == initial_entries
    terminal_ok = post_entries[: len(terminal_entries)] == terminal_entries
    if not initial_ok:
        return {
            "prefix_ok": False,
            "warning": "terminal action log is not an extension of initial log",
            "initial_count": len(initial_entries),
            "terminal_count": len(terminal_entries),
            "post_verifier_count": len(post_entries),
            "agent_entries": [],
            "verifier_entries": [],
        }
    if not terminal_ok:
        return {
            "prefix_ok": False,
            "warning": (
                "post-verifier action log is not an extension of terminal log"
            ),
            "initial_count": len(initial_entries),
            "terminal_count": len(terminal_entries),
            "post_verifier_count": len(post_entries),
            "agent_entries": [],
            "verifier_entries": [],
        }
    return {
        "prefix_ok": True,
        "warning": None,
        "initial_count": len(initial_entries),
        "terminal_count": len(terminal_entries),
        "post_verifier_count": len(post_entries),
        "agent_entries": terminal_entries[len(initial_entries) :],
        "verifier_entries": post_entries[len(terminal_entries) :],
    }


def _format(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".jsonl"):
        return "jsonl"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".md"):
        return "markdown"
    if name.endswith(".stdout"):
        return "stdout"
    if name.endswith(".stderr"):
        return "stderr"
    if name.endswith(".log"):
        return "log"
    return "text"


def _category(relative: Path) -> str:
    parts = relative.parts
    joined = relative.as_posix()
    if joined.startswith("trajectory/"):
        return "trajectory"
    if joined.startswith("artifacts/env0/"):
        return "environment"
    if joined.startswith("artifacts/openclaw/"):
        return "openclaw"
    if joined.startswith("artifacts/openrouter"):
        return "provider"
    if parts and parts[0] in {"verifier", "trainer"}:
        return "evaluation"
    if parts and parts[0] == "agent":
        return "agent"
    return "run"


def _phase_service(relative: Path) -> tuple[str | None, str | None]:
    parts = relative.parts
    if len(parts) >= 5 and parts[:2] == ("artifacts", "env0"):
        phase = parts[2] if parts[2] in PHASES else None
        service = parts[3] if parts[3].startswith("mock-") else None
        return phase, service
    return None, None


def _artifact_ref(
    path: Path,
    *,
    base: Path,
    scope: str,
    attempt_id: str | None,
    secret_bytes: list[bytes],
) -> dict[str, Any]:
    data = path.read_bytes()
    if any(secret in data for secret in secret_bytes):
        raise RuntimeError(
            f"configured secret found in corpus artifact: {path}"
        )
    relative = path.relative_to(base)
    phase, service = _phase_service(relative)
    return {
        "scope": scope,
        "attempt_id": attempt_id,
        "path": relative.as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": _format(path),
        "category": _category(relative),
        "phase": phase,
        "service": service,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    )
    temporary.replace(path)


def _attempt_manifest(
    path: Path,
    *,
    run_root: Path,
    canonical_path: Path | None,
    secret_bytes: list[bytes],
) -> dict[str, Any]:
    artifacts = [
        _artifact_ref(
            file,
            base=path,
            scope="attempt",
            attempt_id=path.name,
            secret_bytes=secret_bytes,
        )
        for file in sorted(path.rglob("*"))
        if file.is_file()
    ]
    result = _load_json(path / "result.json", {}) or {}
    validation = (
        _load_json(path / "repair-validation.json")
        or _load_json(path / "capture-validation.json")
        or {}
    )
    return {
        "id": path.name,
        "task": path.name.split("__", 1)[0],
        "root": path.relative_to(run_root).as_posix(),
        "canonical": (
            canonical_path is not None
            and path.resolve() == canonical_path.resolve()
        ),
        "result": {
            "reward": ((result.get("rewards") or {}).get("reward")),
            "error": result.get("error"),
            "verifier_error": result.get("verifier_error"),
            "finished_at": result.get("finished_at"),
        },
        "validation": validation,
        "artifacts": artifacts,
    }


def _prompt(rollout: Path) -> str:
    prompts = _load_json(rollout / "prompts.json", [])
    if isinstance(prompts, list) and prompts:
        return str(prompts[0])
    bundle = _load_json(
        rollout / "artifacts/openclaw/bundle/prompts.json", {}
    )
    if isinstance(bundle, dict):
        return str(bundle.get("latestSubmittedPrompt") or "")
    return ""


def _bootstrap_files(rollout: Path) -> dict[str, str]:
    root = rollout / "artifacts/openclaw/bootstrap"
    if not root.is_dir():
        return {}
    return {
        path.name: path.read_text(errors="replace")
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def _service_activity(
    rollout: Path, services: Iterable[str]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for service in services:
        logs = {
            phase: _load_json(
                rollout
                / "artifacts/env0"
                / phase
                / service
                / "action_log.json",
                {},
            )
            for phase in PHASES
        }
        output[service] = phase_activity(
            logs["initial"], logs["terminal"], logs["post_verifier"]
        )
    return output


def _canonical_record(
    rollout: Path,
    seal_entry: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    result = _load_json(rollout / "result.json", {}) or {}
    bundle_prompts = _load_json(
        rollout / "artifacts/openclaw/bundle/prompts.json", {}
    )
    initial_manifest = _load_json(
        rollout / "artifacts/env0/initial/capture-manifest.json", {}
    )
    services = sorted((initial_manifest.get("services") or {}).keys())
    system_prompt_path = (
        rollout / "artifacts/openclaw/bundle/system-prompt.txt"
    )
    provider_trace_path = rollout / "trajectory/llm_trajectory.jsonl"
    return {
        "attempt_id": rollout.name,
        "source": seal_entry.get("source"),
        "prompt": _prompt(rollout),
        "services": services,
        "reward": seal_entry.get("reward"),
        "cost_usd": seal_entry.get("cost_usd"),
        "cost_coverage": seal_entry.get("cost_coverage"),
        "model": result.get("model"),
        "skill_mode": result.get("skill_mode"),
        "agent_result": result.get("agent_result"),
        "result_status": {
            "error": result.get("error"),
            "error_category": result.get("error_category"),
            "verifier_error": result.get("verifier_error"),
            "partial_trajectory": result.get("partial_trajectory"),
        },
        "instructions": {
            "task_prompt": _prompt(rollout),
            "bundle_prompts": bundle_prompts,
            "actual_system_message": first_provider_system_message(
                provider_trace_path
            ),
            "system_prompt_excerpt": (
                system_prompt_path.read_text(errors="replace")
                if system_prompt_path.is_file()
                else ""
            ),
            "bootstrap": _bootstrap_files(rollout),
            "tools": _load_json(
                rollout / "artifacts/openclaw/bundle/tools.json", {}
            ),
        },
        "agent_timeline": load_jsonl(
            rollout / "trajectory/acp_trajectory.jsonl"
        ),
        "provider_calls": provider_call_summaries(
            provider_trace_path
        ),
        "provider_usage": _load_json(
            rollout / "artifacts/openrouter-generations.json", {}
        ),
        "service_activity": _service_activity(rollout, services),
        "evaluation": {
            "rewards": result.get("rewards"),
            "verifier_replay": seal_entry.get("verifier_replay"),
        },
        "provenance": {
            "seal": seal_entry,
            "config": _load_json(rollout / "config.json", {}),
            "timing": _load_json(rollout / "timing.json", {}),
            "capture_validation": _load_json(
                rollout / "capture-validation.json", {}
            ),
            "source": result.get("source"),
            "trajectory_summary": result.get("trajectory_summary"),
            "usage_tracking": result.get("usage_tracking"),
        },
        "artifacts": artifacts,
    }


def _reward_class(reward: Any) -> str:
    if isinstance(reward, (int, float)) and reward >= 1:
        return "full"
    if isinstance(reward, (int, float)) and reward <= 0:
        return "zero"
    return "partial"


def _replace_output(temporary: Path, output_root: Path) -> None:
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(temporary), str(output_root))


def _portable_value(value: Any, replacements: list[tuple[str, str]]) -> Any:
    if isinstance(value, dict):
        return {
            key: _portable_value(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_portable_value(item, replacements) for item in value]
    if isinstance(value, str):
        for source, replacement in replacements:
            value = value.replace(source, replacement)
    return value


def build_manifests(
    run_root: Path,
    output_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Build local manifests and return their audited counts."""
    run_root = run_root.resolve()
    seal_path = run_root / "sealed-corpus.json"
    seal = _load_json(seal_path)
    if not isinstance(seal, dict):
        raise RuntimeError(f"sealed corpus is missing or invalid: {seal_path}")
    tasks = seal.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise RuntimeError("sealed corpus has no canonical task map")
    if seal.get("canonical_count") != len(tasks):
        raise RuntimeError("sealed corpus canonical count is inconsistent")

    attempt_dirs = attempt_directories(run_root)
    attempt_roots = {path.resolve() for path in attempt_dirs}
    canonical_paths: dict[str, Path] = {}
    for task, entry in tasks.items():
        canonical_paths[task] = resolve_canonical_rollout(
            run_root,
            task,
            entry,
            attempt_roots,
        )

    secret_bytes = [
        value.encode()
        for value in sorted(set(secret_values))
        if isinstance(value, str) and len(value) >= 16
    ]
    attempts_by_task: dict[str, list[dict[str, Any]]] = {}
    for path in attempt_dirs:
        task = path.name.split("__", 1)[0]
        manifest = _attempt_manifest(
            path,
            run_root=run_root,
            canonical_path=canonical_paths.get(task),
            secret_bytes=secret_bytes,
        )
        attempts_by_task.setdefault(task, []).append(manifest)

    canonical_ids = {
        task: next(
            (
                attempt["id"]
                for attempt in attempts_by_task.get(task, [])
                if attempt["canonical"]
            ),
            None,
        )
        for task in tasks
    }
    missing_ids = [task for task, value in canonical_ids.items() if not value]
    if missing_ids:
        raise RuntimeError(
            f"canonical rollout is not present in attempt inventory: {missing_ids}"
        )

    def inside_attempt(path: Path) -> bool:
        return any(root in path.resolve().parents for root in attempt_roots)

    run_artifacts = [
        _artifact_ref(
            path,
            base=run_root,
            scope="run",
            attempt_id=None,
            secret_bytes=secret_bytes,
        )
        for path in sorted(run_root.rglob("*"))
        if path.is_file() and not inside_attempt(path)
    ]

    with tempfile.TemporaryDirectory(
        prefix="clawsbench-viewer-", dir=output_root.parent
    ) as temporary_name:
        temporary = Path(temporary_name) / "clawsbench"
        task_summaries: list[dict[str, Any]] = []
        for task in sorted(tasks):
            seal_entry = tasks[task]
            attempts = sorted(
                attempts_by_task.get(task, []), key=lambda item: item["id"]
            )
            canonical_attempt = next(
                item for item in attempts if item["canonical"]
            )
            canonical = _canonical_record(
                canonical_paths[task],
                seal_entry,
                canonical_attempt["artifacts"],
            )
            detail = {
                "schema_version": 1,
                "benchmark": "clawsbench",
                "task": task,
                "family": task.split("-", 1)[0],
                "canonical": canonical,
                "attempts": attempts,
            }
            _atomic_json(temporary / "tasks" / f"{task}.json", detail)
            phase_drift = bool(
                seal_entry.get("phase_drift_annotations") or []
            )
            post_terminal = any(
                (activity or {}).get("new_entry_count", 0) > 0
                for activity in (
                    seal_entry.get("post_terminal_activity") or {}
                ).values()
            )
            task_summaries.append(
                {
                    "task": task,
                    "family": task.split("-", 1)[0],
                    "prompt": canonical["prompt"],
                    "reward": canonical["reward"],
                    "reward_class": _reward_class(canonical["reward"]),
                    "model": canonical["model"],
                    "services": canonical["services"],
                    "source": canonical["source"],
                    "cost_usd": canonical["cost_usd"],
                    "canonical_attempt_id": canonical["attempt_id"],
                    "attempt_count": len(attempts),
                    "phase_drift": phase_drift,
                    "post_terminal_activity": post_terminal,
                    "verifier_replay": bool(
                        seal_entry.get("verifier_replay")
                    ),
                    "repair_rerun": (
                        seal_entry.get("source") == "repair_rerun"
                    ),
                }
            )

        artifact_count = len(run_artifacts) + sum(
            len(attempt["artifacts"])
            for attempts in attempts_by_task.values()
            for attempt in attempts
        )
        index = {
            "schema_version": 1,
            "benchmark": "clawsbench",
            "label": "ClawsBench Standard60",
            "local_only": True,
            "model": seal.get("model"),
            "canonical_count": len(tasks),
            "attempt_count": len(attempt_dirs),
            "artifact_count": artifact_count,
            "families": sorted(
                {summary["family"] for summary in task_summaries}
            ),
            "tasks": task_summaries,
            "corpus": {
                key: seal.get(key)
                for key in (
                    "canonical_cost_usd",
                    "canonical_cost_coverage",
                    "canonical_cost_is_lower_bound",
                    "selection_policy",
                    "sealed_at",
                    "phase_drift_tasks",
                    "post_terminal_mutation_tasks",
                    "recovered_verifier_tasks",
                    "repair_rerun_tasks",
                )
            },
        }
        run_manifest = {
            "schema_version": 1,
            "benchmark": "clawsbench",
            "seal": {key: value for key, value in seal.items() if key != "tasks"},
            "excluded_original_attempts": seal.get(
                "excluded_original_attempts", {}
            ),
            "artifacts": run_artifacts,
        }
        _atomic_json(temporary / "index.json", index)
        _atomic_json(temporary / "run.json", run_manifest)
        _replace_output(temporary, output_root)

    return {
        "canonical_count": len(tasks),
        "attempt_count": len(attempt_dirs),
        "artifact_count": artifact_count,
        "output": str(output_root),
    }


def build_published_sample(
    run_root: Path,
    output_root: Path,
    *,
    tasks: Iterable[str] = PUBLISHED_SAMPLE_TASKS,
    secret_values: Iterable[str] = (),
) -> dict[str, Any]:
    """Publish portable normalized examples without raw corpus artifacts."""
    run_root = run_root.resolve()
    output_root = output_root.resolve()
    selected = tuple(dict.fromkeys(tasks))
    if not selected:
        raise RuntimeError("published sample task list is empty")

    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="clawsbench-published-", dir=output_root.parent
    ) as temporary_name:
        workspace = Path(temporary_name)
        full_output = workspace / "full"
        full_report = build_manifests(
            run_root,
            full_output,
            secret_values=secret_values,
        )
        full_index = _load_json(full_output / "index.json", {}) or {}
        full_run = _load_json(full_output / "run.json", {}) or {}
        full_seal = _load_json(run_root / "sealed-corpus.json", {}) or {}
        summaries = {
            item["task"]: item for item in full_index.get("tasks") or []
        }
        unknown = [task for task in selected if task not in summaries]
        if unknown:
            raise RuntimeError(
                f"published sample tasks are missing from the seal: {unknown}"
            )

        replacements = sorted(
            {
                str(run_root): "<clawsbench-corpus>",
                str(EVAL_ROOT.parent): "<repository>",
                str(Path.home()): "<home>",
            }.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        published = workspace / "published"
        details: list[dict[str, Any]] = []
        attempt_count = 0
        for task in selected:
            detail = _portable_value(
                _load_json(full_output / "tasks" / f"{task}.json", {}),
                replacements,
            )
            if not isinstance(detail, dict) or not detail:
                raise RuntimeError(f"published task manifest is missing: {task}")
            detail["canonical"]["artifacts"] = []
            for attempt in detail.get("attempts") or []:
                attempt["root"] = f"published-sample/{attempt['id']}"
                attempt["artifacts"] = []
                (published / "corpus" / attempt["root"]).mkdir(
                    parents=True, exist_ok=True
                )
            attempt_count += len(detail.get("attempts") or [])
            details.append(detail)
            _atomic_json(
                published / "data/tasks" / f"{task}.json", detail
            )

        sample_index = _portable_value(full_index, replacements)
        sample_index.update(
            {
                "label": "ClawsBench published examples",
                "local_only": False,
                "sample": True,
                "sample_raw_artifacts": False,
                "full_canonical_count": full_report["canonical_count"],
                "canonical_count": len(selected),
                "attempt_count": attempt_count,
                "artifact_count": 0,
                "families": sorted(
                    {summaries[task]["family"] for task in selected}
                ),
                "tasks": [
                    _portable_value(summaries[task], replacements)
                    for task in selected
                ],
            }
        )
        sample_index.setdefault("corpus", {})["sample_note"] = (
            "Five normalized trajectories are published for review. "
            "The complete raw 60-task corpus remains local."
        )
        _atomic_json(published / "data/index.json", sample_index)

        sample_run = _portable_value(full_run, replacements)
        sample_run.update(
            {
                "sample": True,
                "sample_tasks": list(selected),
                "full_canonical_count": full_report["canonical_count"],
                "artifacts": [],
                "excluded_original_attempts": {
                    task: value
                    for task, value in (
                        sample_run.get("excluded_original_attempts") or {}
                    ).items()
                    if task in selected
                },
            }
        )
        _atomic_json(published / "data/run.json", sample_run)

        sample_seal = _portable_value(full_seal, replacements)
        sample_seal.update(
            {
                "sample": True,
                "full_canonical_count": full_report["canonical_count"],
                "canonical_count": len(selected),
                "task_count": len(selected),
                "tasks": {
                    task: {
                        **_portable_value(full_seal["tasks"][task], replacements),
                        "rollout": next(
                            attempt["root"]
                            for attempt in details[index]["attempts"]
                            if attempt["canonical"]
                        ),
                    }
                    for index, task in enumerate(selected)
                },
                "excluded_original_attempts": {
                    task: value
                    for task, value in (
                        sample_seal.get("excluded_original_attempts") or {}
                    ).items()
                    if task in selected
                },
            }
        )
        _atomic_json(published / "corpus/sealed-corpus.json", sample_seal)
        _replace_output(published, output_root)

    return {
        "canonical_count": len(selected),
        "attempt_count": attempt_count,
        "artifact_count": 0,
        "output": str(output_root),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build local-only ClawsBench viewer manifests."
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=Path(
            os.environ.get("CLAWSBENCH_CORPUS_ROOT", DEFAULT_RUN_ROOT)
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--publish-sample",
        action="store_true",
        help="write the fixed representative sample used by the deployed viewer",
    )
    parser.add_argument(
        "--sample-output",
        type=Path,
        default=DEFAULT_PUBLISHED_SAMPLE_OUT,
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        help=(
            "Environment file whose configured secrets must not occur in the "
            "corpus. Repeatable; defaults to eval/.env and project .env."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.run_root.is_dir():
        print(
            json.dumps(
                {
                    "available": False,
                    "reason": f"corpus not found: {args.run_root}",
                },
                indent=2,
            )
        )
        return 0
    secret_values = configured_secrets(
        *(args.env_file or [EVAL_ROOT / ".env", EVAL_ROOT.parent / ".env"])
    )
    if args.publish_sample:
        report = build_published_sample(
            args.run_root,
            args.sample_output,
            secret_values=secret_values,
        )
    else:
        report = build_manifests(
            args.run_root,
            args.output,
            secret_values=secret_values,
        )
    print(json.dumps({"available": True, **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
