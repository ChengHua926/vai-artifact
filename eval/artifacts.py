"""Shared deterministic primitives for the paper-main evaluation."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CASE_ID_NAMESPACE = b"paper-main-case-v1\0"


def _validate_json_object_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _validate_json_object_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_json_object_keys(child)


def canonical_json_bytes(value: Any) -> bytes:
    """Encode one JSON value with stable keys, separators, and UTF-8 bytes."""

    _validate_json_object_keys(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    """Encode rows canonically, sorting their complete encoded bytes."""

    encoded = sorted(canonical_json_bytes(row) for row in rows)
    return b"".join(row + b"\n" for row in encoded)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash a regular file without following a final symlink."""

    file_path = Path(path)
    if file_path.is_symlink() or not file_path.is_file():
        raise ValueError(f"source must be a regular file: {file_path}")
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_file_record(path: Path, *, root: Path) -> dict[str, str]:
    """Return a source hash with a portable path relative to ``root``."""

    source_root = Path(root)
    source_path = Path(path)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(f"source root must be a regular directory: {source_root}")
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError(f"source must be a regular file: {source_path}")
    resolved_root = source_root.resolve(strict=True)
    resolved_source = source_path.resolve(strict=True)
    try:
        relative = resolved_source.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"source is outside source root: {source_path}") from error
    return {"path": relative.as_posix(), "sha256": sha256_file(resolved_source)}


def _require_nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def canonical_case_key(
    *, benchmark: str, model_id: str, identity: Mapping[str, Any]
) -> str:
    """Create an unambiguous, readable key for one benchmark/model case."""

    benchmark = _require_nonempty_text(benchmark, "benchmark")
    model_id = _require_nonempty_text(model_id, "model_id")
    if not isinstance(identity, Mapping) or not identity:
        raise ValueError("identity must be a non-empty mapping")
    return canonical_json_bytes(
        {
            "benchmark": benchmark,
            "identity": dict(identity),
            "model_id": model_id,
        }
    ).decode("utf-8")


def stable_case_id(cohort_outer_sha256: str, case_key: str) -> str:
    """Bind a case key to the exact current outer cohort manifest."""

    if not isinstance(cohort_outer_sha256, str) or not _SHA256_RE.fullmatch(
        cohort_outer_sha256
    ):
        raise ValueError("cohort_outer_sha256 must be a lowercase SHA-256")
    case_key = _require_nonempty_text(case_key, "case_key")
    digest = sha256_bytes(
        _CASE_ID_NAMESPACE
        + cohort_outer_sha256.encode("ascii")
        + b"\0"
        + case_key.encode("utf-8")
    )
    return f"case_{digest}"


def validate_unique_case_rows(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Materialize case rows and reject missing or duplicate keys and IDs."""

    materialized = tuple(rows)
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for index, row in enumerate(materialized):
        if not isinstance(row, Mapping):
            raise ValueError(f"case row {index} must be a mapping")
        case_id = _require_nonempty_text(row.get("case_id"), f"case row {index} case_id")
        case_key = _require_nonempty_text(
            row.get("case_key"), f"case row {index} case_key"
        )
        if case_id in seen_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        if case_key in seen_keys:
            raise ValueError(f"duplicate case_key: {case_key}")
        seen_ids.add(case_id)
        seen_keys.add(case_key)
    return materialized


def _safe_artifact_path(raw_path: str, *, suffix: str) -> Path:
    text = _require_nonempty_text(raw_path, "artifact path")
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise ValueError(f"artifact path contains control characters: {text!r}")
    path = Path(text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != text
        or path.name == "SHA256SUMS"
    ):
        raise ValueError(f"artifact path must be a safe relative path: {text}")
    if path.suffix != suffix:
        raise ValueError(f"artifact path must end in {suffix}: {text}")
    return path


def _reject_symlinked_output_ancestors(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    for ancestor in reversed(absolute.parents):
        if ancestor.is_symlink():
            raise ValueError(f"symlinked output ancestor is not allowed: {ancestor}")


def write_artifact_bundle(
    output_dir: Path,
    *,
    row_files: Mapping[str, Iterable[Mapping[str, Any]]],
    summary: Mapping[str, Any],
    summary_file: str = "summary.json",
) -> dict[str, str]:
    """Write deterministic ledgers, an unchanged caller summary, and hashes."""

    if not isinstance(row_files, Mapping):
        raise ValueError("row_files must be a mapping")
    if not isinstance(summary, Mapping):
        raise ValueError("summary must be a mapping")

    payloads: dict[str, bytes] = {}
    for name, rows in row_files.items():
        relative = _safe_artifact_path(name, suffix=".jsonl")
        normalized_name = relative.as_posix()
        if normalized_name in payloads:
            raise ValueError(f"duplicate artifact path: {normalized_name}")
        payloads[normalized_name] = canonical_jsonl_bytes(rows)

    summary_path = _safe_artifact_path(summary_file, suffix=".json")
    normalized_summary = summary_path.as_posix()
    if normalized_summary in payloads:
        raise ValueError(f"duplicate artifact path: {normalized_summary}")
    payloads[normalized_summary] = canonical_json_bytes(summary)

    destination = Path(output_dir)
    _reject_symlinked_output_ancestors(destination)
    if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
        raise ValueError(f"output directory must be a regular directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    allowed = set(payloads) | {"SHA256SUMS"}
    for existing in destination.rglob("*"):
        if existing.is_symlink():
            raise ValueError(f"symlink output artifact is not allowed: {existing}")
        if existing.is_file():
            relative = existing.relative_to(destination).as_posix()
            if relative not in allowed:
                raise ValueError(f"unexpected output artifact: {relative}")

    hashes = {
        name: sha256_bytes(payloads[name])
        for name in sorted(payloads, key=lambda value: value.encode("utf-8"))
    }
    sums = b"".join(
        f"{digest}  {name}\n".encode("utf-8") for name, digest in hashes.items()
    )

    for name in hashes:
        target = destination / name
        current = destination
        for part in Path(name).parts[:-1]:
            current /= part
            if current.exists() and (current.is_symlink() or not current.is_dir()):
                raise ValueError(f"artifact parent must be a regular directory: {current}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise ValueError(f"symlink output artifact is not allowed: {target}")
        target.write_bytes(payloads[name])
    (destination / "SHA256SUMS").write_bytes(sums)
    return hashes
