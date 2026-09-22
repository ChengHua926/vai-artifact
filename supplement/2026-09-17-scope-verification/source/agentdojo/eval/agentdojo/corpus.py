"""Fail-closed AgentDojo cell loading from the sealed evaluation cohort."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.artifacts import (
    canonical_case_key,
    source_file_record,
    stable_case_id,
    validate_unique_case_rows,
)
from eval.dataset import EvaluationCohort, load_paper_main_v1


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {label}: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _case_kind(cell: dict[str, Any]) -> str:
    attack_type = cell.get("attack_type")
    user_task = cell.get("user_task_id")
    injection_task = cell.get("injection_task_id")
    if attack_type == "important_instructions" and injection_task != "none":
        return "attack"
    if attack_type != "none" or injection_task != "none":
        raise ValueError(f"unsupported AgentDojo cell shape: {cell}")
    if isinstance(user_task, str) and user_task.startswith("injection_task_"):
        return "injection_baseline"
    return "benign"


def _protocol_status(kind: str, trace: dict[str, Any]) -> str:
    utility = trace.get("utility")
    if not isinstance(utility, bool):
        return "protocol_error"
    if kind == "attack" and not isinstance(trace.get("security"), bool):
        return "protocol_error"
    return "graded"


def _cell_path(root: Path, cell: dict[str, Any]) -> Path:
    values = []
    for key in ("suite_name", "user_task_id", "attack_type", "injection_task_id"):
        value = cell.get(key)
        if not isinstance(value, str) or not value or "/" in value or value in {".", ".."}:
            raise ValueError(f"invalid AgentDojo cell {key}: {value!r}")
        values.append(value)
    suite, user_task, attack_type, injection_task = values
    return (
        root
        / "agentdojo"
        / "openai-compatible"
        / suite
        / user_task
        / attack_type
        / f"{injection_task}.json"
    )


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


def load_agentdojo_cases(
    evaluation_cohort: EvaluationCohort | None = None,
) -> list[dict[str, Any]]:
    """Load all and only AgentDojo cells named by accepted sealed shards."""

    loaded = load_paper_main_v1()
    if evaluation_cohort is not None and (
        not isinstance(evaluation_cohort, EvaluationCohort)
        or _cohort_identity(evaluation_cohort) != _cohort_identity(loaded)
    ):
        raise ValueError(
            "caller-supplied cohort does not match freshly verified paper_main_v1"
        )
    rows: list[dict[str, Any]] = []
    for shard in loaded.cohort.accepted_shards:
        if shard.benchmark != "agentdojo":
            continue
        capture = _read_object(shard.path / "capture-lock.json", "capture lock")
        cells = capture.get("expected_trace_cells")
        if not isinstance(cells, list) or len(cells) != shard.accepted_count:
            raise ValueError(f"accepted cell count mismatch for {shard.path}")
        seen: set[tuple[str, str, str, str]] = set()
        for raw_cell in cells:
            if not isinstance(raw_cell, dict):
                raise ValueError(f"AgentDojo cell must be an object: {raw_cell!r}")
            cell = dict(raw_cell)
            path = _cell_path(shard.path, cell)
            trace = _read_object(path, "AgentDojo trace")
            identity = (
                str(cell["suite_name"]),
                str(cell["user_task_id"]),
                str(cell["attack_type"]),
                str(cell["injection_task_id"]),
            )
            if identity in seen:
                raise ValueError(f"duplicate accepted AgentDojo cell in shard: {identity}")
            seen.add(identity)
            for key, expected in zip(
                ("suite_name", "user_task_id", "attack_type", "injection_task_id"),
                identity,
                strict=True,
            ):
                observed = trace.get(key)
                observed = (
                    "none"
                    if observed is None and key in {"attack_type", "injection_task_id"}
                    else observed
                )
                if observed != expected:
                    raise ValueError(
                        f"AgentDojo trace identity mismatch for {path}: {key}={observed!r}"
                    )
            if identity[0] != shard.kind:
                raise ValueError(f"AgentDojo suite/shard mismatch: {identity[0]} != {shard.kind}")
            kind = _case_kind(cell)
            key_identity = {
                "attack_type": identity[2],
                "injection_task_id": identity[3],
                "suite": identity[0],
                "user_task_id": identity[1],
            }
            case_key = canonical_case_key(
                benchmark="agentdojo",
                model_id=shard.model_id,
                identity=key_identity,
            )
            rows.append(
                {
                    "schema_version": 1,
                    "case_id": stable_case_id(loaded.outer_sha256, case_key),
                    "case_key": case_key,
                    "cohort_id": loaded.cohort.cohort_id,
                    "cohort_outer_sha256": loaded.outer_sha256,
                    "benchmark": "agentdojo",
                    "benchmark_version": trace.get("benchmark_version"),
                    "model_id": shard.model_id,
                    "suite": identity[0],
                    "user_task_id": identity[1],
                    "attack_type": identity[2],
                    "injection_task_id": identity[3],
                    "case_kind": kind,
                    "protocol_status": _protocol_status(kind, trace),
                    "stored_utility": trace.get("utility")
                    if isinstance(trace.get("utility"), bool)
                    else None,
                    "stored_security": trace.get("security")
                    if kind == "attack" and isinstance(trace.get("security"), bool)
                    else None,
                    "raw_security": trace.get("security"),
                    "raw_error": trace.get("error"),
                    "source": source_file_record(path, root=shard.path),
                    "source_path": path,
                    "trace": trace,
                }
            )
    validate_unique_case_rows(rows)
    if len(rows) != 2162:
        raise ValueError(f"expected 2162 AgentDojo cells, got {len(rows)}")
    return sorted(rows, key=lambda row: row["case_key"])
