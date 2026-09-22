"""Fail-closed path resolution for a sealed ClawsBench corpus copy."""

from __future__ import annotations

import json
import stat
from pathlib import Path


ATTEMPT_JOBS = ("standard60_v1", "standard60_repair_v1")
_SOURCE_JOBS = {
    "original": "standard60_v1",
    "original_with_verifier_replay": "standard60_v1",
    "repair_rerun": "standard60_repair_v1",
}


def attempt_directories(run_root: Path) -> list[Path]:
    """Inventory the two sealed attempt phases with globally unique IDs."""
    root = Path(run_root).resolve()
    attempts: list[Path] = []
    for job in ATTEMPT_JOBS:
        job_root = root / job
        if job_root.is_dir():
            attempts.extend(
                path for path in sorted(job_root.iterdir()) if path.is_dir()
            )
    ids = [path.name for path in attempts]
    if len(ids) != len(set(ids)):
        raise RuntimeError("attempt directory names are not globally unique")
    return attempts


def resolve_canonical_rollout(
    run_root: Path,
    task: str,
    entry: object,
    attempt_roots: set[Path],
) -> Path:
    """Resolve one canonical rollout without trusting its old absolute root."""
    if not isinstance(entry, dict):
        raise RuntimeError(f"canonical seal entry is invalid for {task}")
    if entry.get("task") != task:
        raise RuntimeError(f"canonical seal task mismatch for {task}")

    root = Path(run_root).resolve()
    raw_rollout = Path(str(entry.get("rollout") or "")).expanduser()
    try:
        relative = raw_rollout.relative_to(root)
    except ValueError:
        phase_positions = [
            index
            for index, part in enumerate(raw_rollout.parts)
            if part in ATTEMPT_JOBS
        ]
        if len(phase_positions) > 1:
            raise RuntimeError(f"ambiguous canonical rollout for {task}")
        if (
            len(phase_positions) != 1
            or phase_positions[0] != len(raw_rollout.parts) - 2
        ):
            raise RuntimeError(
                f"cannot prove canonical rollout relocation for {task}"
            )
        start = phase_positions[0]
        relative = Path(*raw_rollout.parts[start:])

    if len(relative.parts) != 2 or relative.parts[0] not in ATTEMPT_JOBS:
        raise RuntimeError(
            f"cannot prove canonical rollout relocation for {task}"
        )
    phase, attempt_id = relative.parts
    if phase != _SOURCE_JOBS.get(entry.get("source")):
        raise RuntimeError(f"canonical rollout phase mismatch for {task}")
    prefix, separator, suffix = attempt_id.partition("__")
    if prefix != task or not separator or not suffix:
        raise RuntimeError(f"canonical attempt identity mismatch for {task}")

    candidate_path = root
    for part in relative.parts:
        candidate_path /= part
        if candidate_path.is_symlink():
            raise RuntimeError(f"canonical rollout symlink for {task}")
    candidate = candidate_path
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise RuntimeError(
            f"canonical rollout escapes corpus root for {task}"
        ) from error
    if not candidate.is_dir():
        raise RuntimeError(f"canonical rollout is missing for {task}")
    if candidate not in attempt_roots:
        raise RuntimeError(
            f"canonical rollout is not present in attempt inventory for {task}"
        )
    result_path = candidate / "result.json"
    try:
        result_mode = result_path.lstat().st_mode
    except OSError as error:
        raise RuntimeError(
            f"canonical attempt identity mismatch for {task}"
        ) from error
    if not stat.S_ISREG(result_mode):
        raise RuntimeError(f"canonical attempt identity mismatch for {task}")
    try:
        result = json.loads(result_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"canonical attempt identity mismatch for {task}"
        ) from error
    if (
        not isinstance(result, dict)
        or result.get("task_name") != task
        or result.get("rollout_name") != attempt_id
    ):
        raise RuntimeError(f"canonical attempt identity mismatch for {task}")
    return candidate
