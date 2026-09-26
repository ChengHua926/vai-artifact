"""Fail-closed loader for sealed evaluation cohorts."""

from __future__ import annotations

import hashlib
import gzip
import json
import os
import re
import shutil
import tarfile
import tempfile
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from eval.artifacts import sha256_file


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_LEAF_SIDECARS = frozenset({"ROOT_SHA256", "SHA256SUMS"})
# Re-pinned for the anonymized review artifact: the sealed captures were rewritten to remove
# local paths, and the seal manifests were recomputed over the rewritten bytes.
_PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256 = (
    "257ad5b0eb5aac1b6d311625c77ce017bb0421a0311e1da8478d66ad9ea60d76"
)
PAPER_MAIN_V1_ROOT = Path(__file__).resolve().parent / "data" / "runs"
PAPER_MAIN_V1_LOCK = PAPER_MAIN_V1_ROOT / "cohort.lock.json"
PAPER_MAIN_V1_METADATA = PAPER_MAIN_V1_ROOT / "dataset.json"


@dataclass(frozen=True)
class AcceptedShard:
    benchmark: str
    model_id: str
    kind: str
    path: Path
    root_sha256: str
    accepted_count: int


@dataclass(frozen=True)
class Cohort:
    cohort_id: str
    lock_path: Path
    outer_manifest_path: Path
    accepted_shards: tuple[AcceptedShard, ...]


@dataclass(frozen=True)
class EvaluationCohort:
    """A loader-verified cohort plus the hash that namespaces its case IDs."""

    cohort: Cohort
    outer_sha256: str


@dataclass(frozen=True)
class ArchiveMetadata:
    filename: str
    sha256: str
    bytes: int
    url: str | None


@dataclass(frozen=True)
class DatasetMetadata:
    schema_version: int
    cohort_id: str
    corpus_directory: str
    archive: ArchiveMetadata | None


def _fail(message: str) -> None:
    raise ValueError(message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail(f"invalid SHA-256 for {label}")
    return value


def _as_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be a list")
    return value


def _as_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{label} must be a non-negative integer")
    return value


def _as_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _fail(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"{label} must be numeric") from error
    if not result.is_finite():
        _fail(f"{label} must be finite")
    return result


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a non-empty string")
    return value


def _resolve_relative(base: Path, raw_path: Any, label: str) -> Path:
    text = _require_text(raw_path, label)
    relative = Path(text)
    if relative.is_absolute() or ".." in relative.parts:
        _fail(f"{label} must be a safe relative path")

    candidate = base / relative
    current = base
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            _fail(f"symlink not allowed: {current}")
    try:
        resolved = candidate.resolve(strict=True)
        base_resolved = base.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"missing {label}: {candidate}") from error
    if not resolved.is_relative_to(base_resolved):
        _fail(f"{label} escapes cohort directory")
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        _fail(f"{label} must be a regular file")
    try:
        return _parse_json_bytes(path.read_bytes(), label)
    except OSError as error:
        raise ValueError(f"cannot read {label}: {path}") from error


def _parse_json_bytes(content: bytes, label: str) -> dict[str, Any]:
    try:
        return _as_object(json.loads(content), label)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {label}") from error


def _read_sidecar_hash(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        _fail(f"{label} must be a regular file")
    try:
        tokens = path.read_text().split()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read {label}: {path}") from error
    if not tokens:
        _fail(f"missing SHA-256 token in {label}")
    return _as_sha256(tokens[0], label)


def _verify_hashed_json(
    base: Path,
    reference: dict[str, Any],
    label: str,
) -> tuple[Path, dict[str, Any]]:
    path = _resolve_relative(base, reference.get("path"), f"{label} path")
    expected = _as_sha256(reference.get("sha256"), f"{label} hash")
    if path.is_symlink() or not path.is_file():
        _fail(f"{label} must be a regular file")
    content = path.read_bytes()
    actual = _sha256(content)
    if actual != expected:
        _fail(f"{label} hash mismatch")
    sidecar_path = reference.get("sha256_sidecar_path")
    if sidecar_path is not None:
        sidecar = _resolve_relative(base, sidecar_path, f"{label} SHA-256 sidecar path")
        if _read_sidecar_hash(sidecar, f"{label} SHA-256 sidecar") != expected:
            _fail(f"{label} SHA-256 sidecar mismatch")
    return path, _parse_json_bytes(content, label)


def _require_hash_sidecar_reference(reference: dict[str, Any], label: str) -> None:
    _require_text(reference.get("sha256_sidecar_path"), f"{label} SHA-256 sidecar path")


def _parse_sha256sums(content: bytes) -> dict[str, str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("SHA256SUMS must be UTF-8") from error
    if not text or not text.endswith("\n"):
        _fail("SHA256SUMS must be non-empty and newline terminated")

    entries: dict[str, str] = {}
    paths: list[str] = []
    for line in text.splitlines():
        if line.count("  ") != 1:
            _fail("invalid SHA256SUMS entry")
        digest, relative_path = line.split("  ", 1)
        _as_sha256(digest, "SHA256SUMS entry")
        relative = Path(relative_path)
        if (
            not relative_path
            or relative.is_absolute()
            or ".." in relative.parts
            or relative_path in _LEAF_SIDECARS
            or relative.as_posix() != relative_path
        ):
            _fail("invalid SHA256SUMS path")
        if relative_path in entries:
            _fail("duplicate SHA256SUMS path")
        entries[relative_path] = digest
        paths.append(relative_path)
    if paths != sorted(paths, key=lambda value: value.encode("utf-8")):
        _fail("SHA256SUMS paths must be sorted")
    return entries


def _validate_leaf(shard: dict[str, Any], cohort_dir: Path) -> tuple[Path, str, dict[str, str]]:
    root = _resolve_relative(cohort_dir, shard.get("physical_path"), "physical path")
    if not root.is_dir() or root.is_symlink():
        _fail("physical path must be a regular directory")
    sidecar_path = _resolve_relative(cohort_dir, shard.get("sha256sums_path"), "SHA256SUMS path")
    root_sidecar_path = _resolve_relative(cohort_dir, shard.get("root_sha256_path"), "ROOT_SHA256 path")
    if sidecar_path != root / "SHA256SUMS" or root_sidecar_path != root / "ROOT_SHA256":
        _fail("leaf sidecars must be inside the physical root")

    expected_root = _as_sha256(shard.get("root_sha256"), "leaf root hash")
    sidecar_root = _read_sidecar_hash(root_sidecar_path, "ROOT_SHA256")
    sums_bytes = sidecar_path.read_bytes()
    if _sha256(sums_bytes) != expected_root or sidecar_root != expected_root:
        _fail("leaf root hash mismatch")
    expected_files = _parse_sha256sums(sums_bytes)

    actual_files: dict[str, Path] = {}
    def traversal_error(error: OSError) -> None:
        raise ValueError(f"leaf traversal error: {error}") from error

    try:
        for directory, directories, files in os.walk(root, followlinks=False, onerror=traversal_error):
            directory_path = Path(directory)
            for name in directories:
                if (directory_path / name).is_symlink():
                    _fail(f"symlink not allowed: {directory_path / name}")
            for name in files:
                file_path = directory_path / name
                if file_path.is_symlink():
                    _fail(f"symlink not allowed: {file_path}")
                if not file_path.is_file():
                    _fail(f"non-regular leaf file: {file_path}")
                relative = file_path.relative_to(root).as_posix()
                if relative not in _LEAF_SIDECARS:
                    actual_files[relative] = file_path
    except OSError as error:
        traversal_error(error)
    unexpected = sorted(set(actual_files) - set(expected_files))
    if unexpected:
        _fail(f"unlisted regular file: {unexpected[0]}")
    missing = sorted(set(expected_files) - set(actual_files))
    if missing:
        _fail(f"missing listed file: {missing[0]}")

    byte_count = 0
    for relative, expected_hash in expected_files.items():
        file_path = actual_files[relative]
        data = file_path.read_bytes()
        if _sha256(data) != expected_hash:
            _fail(f"leaf file hash mismatch: {relative}")
        byte_count += len(data)
    if _as_nonnegative_int(shard.get("file_count"), "leaf file count") != len(expected_files):
        _fail("leaf file count mismatch")
    if _as_nonnegative_int(shard.get("evidence_bytes"), "leaf evidence bytes") != byte_count:
        _fail("leaf evidence bytes mismatch")
    return root, expected_root, expected_files


def _shard_count(shard: dict[str, Any], benchmark: str) -> int:
    if "accepted_count" in shard:
        return _as_nonnegative_int(shard["accepted_count"], "accepted shard count")
    if benchmark == "agentdojo":
        field = "expected_trace_count"
    else:
        field = "simulation_count" if "simulation_count" in shard else "expected_simulation_count"
    return _as_nonnegative_int(shard.get(field), f"accepted shard {field}")


def _sum_shard_int(shards: list[dict[str, Any]], field: str, label: str) -> int:
    return sum(_as_nonnegative_int(shard.get(field), f"{label} {field}") for shard in shards)


def _sum_shard_decimal(shards: list[dict[str, Any]], field: str, label: str) -> Decimal:
    return sum((_as_decimal(shard.get(field), f"{label} {field}") for shard in shards), Decimal())


def _count_true_shards(shards: list[dict[str, Any]], field: str, label: str) -> int:
    total = 0
    for shard in shards:
        value = shard.get(field)
        if not isinstance(value, bool):
            _fail(f"{label} {field} must be a boolean")
        total += value
    return total


def _validate_retained_count(summary: dict[str, Any], field: str, actual: int, label: str) -> None:
    if field in summary and _as_nonnegative_int(summary[field], label) != actual:
        _fail(f"{label} mismatch")


def _validate_retained_decimal(
    summary: dict[str, Any], field: str, actual: Decimal, label: str
) -> None:
    if field in summary and _as_decimal(summary[field], label) != actual:
        _fail(f"{label} mismatch")


def _required_count(summary: dict[str, Any], field: str, label: str) -> int:
    if field not in summary:
        _fail(f"{label}: missing required summary field {field}")
    return _as_nonnegative_int(summary[field], f"{label} {field}")


def _required_decimal(summary: dict[str, Any], field: str, label: str) -> Decimal:
    if field not in summary:
        _fail(f"{label}: missing required summary field {field}")
    return _as_decimal(summary[field], f"{label} {field}")


def _required_object(summary: dict[str, Any], field: str, label: str) -> dict[str, Any]:
    if field not in summary:
        _fail(f"{label}: missing required summary field {field}")
    return _as_object(summary[field], f"{label} {field}")


def _validate_accepted_summary_schema(summary: dict[str, Any], label: str) -> None:
    _required_count(summary, "accepted_physical_root_count", label)
    _required_count(summary, "rejected_physical_root_count", label)
    _required_decimal(summary, "accepted_billed_cost_usd", label)

    agentdojo = _required_object(summary, "agentdojo", label)
    for field in (
        "accepted_trace_cell_count",
        "graded_trace_count",
        "model_protocol_error_count",
        "raw_response_count",
    ):
        _required_count(agentdojo, field, f"{label} agentdojo")
    _required_decimal(agentdojo, "billed_cost_usd", f"{label} agentdojo")
    agentdojo_models = _required_object(agentdojo, "models", f"{label} agentdojo")
    for model_id, model_summary_value in agentdojo_models.items():
        model_summary = _as_object(
            model_summary_value, f"{label} agentdojo model summary {model_id}"
        )
        for field in (
            "accepted_trace_cell_count",
            "graded_trace_count",
            "model_protocol_error_count",
            "raw_response_count",
        ):
            _required_count(model_summary, field, f"{label} agentdojo model {model_id}")
        _required_decimal(model_summary, "billed_cost_usd", f"{label} agentdojo model {model_id}")

    tau = _required_object(summary, "tau", label)
    _required_count(tau, "accepted_simulation_count", f"{label} tau")
    _required_decimal(tau, "billed_cost_usd", f"{label} tau")
    tau_models = _required_object(tau, "models", f"{label} tau")
    for model_id, model_summary_value in tau_models.items():
        model_summary = _as_object(model_summary_value, f"{label} tau model summary {model_id}")
        _required_count(model_summary, "accepted_simulation_count", f"{label} tau model {model_id}")
        _required_decimal(model_summary, "billed_cost_usd", f"{label} tau model {model_id}")


def _validate_paper_main_outer_profile(outer: dict[str, Any], label: str) -> None:
    if _as_nonnegative_int(outer.get("schema_version"), f"{label} schema_version") != 1:
        _fail(f"{label} schema_version mismatch")
    if outer.get("phase") != "capture_only":
        _fail(f"{label} phase mismatch")
    if outer.get("hash_algorithm") != "sha256":
        _fail(f"{label} hash_algorithm mismatch")


def _validate_required_count(
    summary: dict[str, Any], field: str, actual: int, label: str
) -> None:
    if _required_count(summary, field, label) != actual:
        _fail(f"{label} mismatch")


def _validate_required_decimal(
    summary: dict[str, Any], field: str, actual: Decimal, label: str
) -> None:
    if _required_decimal(summary, field, label) != actual:
        _fail(f"{label} mismatch")


def _validate_logical_summary(
    logical: dict[str, Any], benchmark: str, accepted: list[dict[str, Any]]
) -> None:
    summary = logical.get("capture_summary")
    if summary is None:
        return
    summary = _as_object(summary, "logical capture summary")
    _validate_retained_count(summary, "accepted_shard_count", len(accepted), "logical accepted shard count")
    count_field = "expected_trace_count" if benchmark == "agentdojo" else "accepted_simulation_count"
    _validate_retained_count(
        summary,
        count_field,
        sum(_shard_count(shard, benchmark) for shard in accepted),
        "logical accepted count",
    )
    _validate_retained_decimal(
        summary,
        "total_billed_cost_usd",
        _sum_shard_decimal(accepted, "cost_usd", "accepted shard"),
        "logical billed cost",
    )
    if benchmark != "agentdojo":
        return

    for field, label in (
        ("graded_trace_count", "logical graded trace count"),
        ("model_protocol_error_count", "logical model protocol error count"),
        ("physical_trace_file_count", "logical physical trace file count"),
        ("raw_response_count", "logical raw response count"),
    ):
        if field not in summary:
            continue
        shard_field = "trace_file_count" if field == "physical_trace_file_count" else field
        _validate_retained_count(summary, field, _sum_shard_int(accepted, shard_field, "accepted shard"), label)
    if "raw_valid_shard_count" in summary:
        _validate_retained_count(
            summary,
            "raw_valid_shard_count",
            _count_true_shards(accepted, "raw_validation_ok", "accepted shard"),
            "logical raw-valid shard count",
        )
    if "strict_valid_shard_count" in summary:
        _validate_retained_count(
            summary,
            "strict_valid_shard_count",
            _count_true_shards(accepted, "strict_validation_ok", "accepted shard"),
            "logical strict-valid shard count",
        )
    if "summed_shard_wall_seconds" in summary:
        _validate_retained_decimal(
            summary,
            "summed_shard_wall_seconds",
            _sum_shard_decimal(accepted, "wall_seconds", "accepted shard"),
            "logical summed shard wall seconds",
        )
    if "rejected_infrastructure_shard_count" in summary:
        rejected = [
            _as_object(value, "rejected shard")
            for value in _as_list(logical.get("rejected_shards"), "rejected shards")
        ]
        rejected_count = sum(shard.get("status") == "rejected_infrastructure_error" for shard in rejected)
        _validate_retained_count(
            summary,
            "rejected_infrastructure_shard_count",
            rejected_count,
            "logical rejected infrastructure shard count",
        )


def _validate_model_summaries(
    benchmark: str,
    summary: dict[str, Any],
    shards_by_model: dict[str, list[dict[str, Any]]],
) -> None:
    models = _required_object(summary, "models", f"{benchmark} accepted summary")
    if set(models) != set(shards_by_model):
        _fail(f"{benchmark} model summary membership mismatch")
    display = "AgentDojo" if benchmark == "agentdojo" else "Tau"
    for model_id, model_summary_value in models.items():
        model_summary = _as_object(model_summary_value, f"{benchmark} model summary {model_id}")
        shards = shards_by_model[model_id]
        if benchmark == "agentdojo":
            _validate_required_count(
                model_summary,
                "accepted_trace_cell_count",
                sum(_shard_count(shard, benchmark) for shard in shards),
                f"{display} model accepted count",
            )
            for field, label in (
                ("graded_trace_count", f"{display} model graded trace count"),
                ("model_protocol_error_count", f"{display} model protocol error count"),
                ("raw_response_count", f"{display} model raw response count"),
            ):
                _validate_required_count(
                    model_summary,
                    field,
                    _sum_shard_int(shards, field, "accepted shard"),
                    label,
                )
        else:
            _validate_required_count(
                model_summary,
                "accepted_simulation_count",
                sum(_shard_count(shard, benchmark) for shard in shards),
                f"{display} model accepted count",
            )
        _validate_required_decimal(
            model_summary,
            "billed_cost_usd",
            _sum_shard_decimal(shards, "cost_usd", "accepted shard"),
            f"{display} model billed cost",
        )


def _verify_outer_metadata(
    outer: dict[str, Any], base: Path, label: str
) -> tuple[dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]], str, dict[str, str]]:
    """Authenticate outer assets and its logical-manifest hash chain."""
    contract_reference = _as_object(outer.get("capture_contract"), f"{label} capture contract")
    _verify_hashed_json(base, contract_reference, f"{label} capture contract")
    contract_hash = _as_sha256(contract_reference.get("sha256"), f"{label} capture contract hash")

    catalogs: dict[str, str] = {}
    for catalog_value in _as_list(outer.get("endpoint_catalogs"), f"{label} endpoint catalogs"):
        catalog = _as_object(catalog_value, f"{label} endpoint catalog")
        model_id = _require_text(catalog.get("model_id"), f"{label} endpoint catalog model ID")
        if model_id in catalogs:
            _fail(f"duplicate {label} endpoint catalog model ID: {model_id}")
        _verify_hashed_json(base, catalog, f"{label} endpoint catalog")
        catalogs[model_id] = _as_sha256(
            catalog.get("sha256"), f"{label} endpoint catalog hash"
        )

    logical_records: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for record_value in _as_list(outer.get("logical_manifests"), f"{label} logical manifests"):
        record = _as_object(record_value, f"{label} logical manifest record")
        _require_hash_sidecar_reference(record, f"{label} logical manifest")
        benchmark = _require_text(record.get("benchmark"), f"{label} logical manifest benchmark")
        model_id = _require_text(record.get("model_id"), f"{label} logical manifest model ID")
        key = (benchmark, model_id)
        if key in logical_records:
            _fail(f"duplicate {label} logical manifest identity: {benchmark}/{model_id}")
        _, logical = _verify_hashed_json(base, record, f"{label} logical manifest")
        if logical.get("benchmark") != benchmark or logical.get("model_id") != model_id:
            _fail(f"{label} logical manifest identity mismatch")
        if _as_sha256(
            logical.get("capture_contract_sha256"), f"{label} logical capture contract hash"
        ) != contract_hash:
            _fail(f"{label} logical capture contract hash mismatch")
        model = _as_object(logical.get("model"), f"{label} logical model")
        if model_id not in catalogs:
            _fail(f"missing {label} endpoint catalog for model {model_id}")
        if _as_sha256(
            model.get("catalog_sha256"), f"{label} logical model catalog hash"
        ) != catalogs[model_id]:
            _fail(f"{label} logical model catalog hash mismatch")
        logical_records[key] = (record, logical)
    return logical_records, contract_hash, catalogs


def _normalized_original_logical(logical: dict[str, Any]) -> dict[str, Any]:
    """Erase only physical relocation fields before comparing the split capture."""
    normalized = json.loads(json.dumps(logical))
    for shard_kind in ("accepted_shards", "rejected_shards"):
        for shard_value in normalized.get(shard_kind, []):
            if not isinstance(shard_value, dict):
                continue
            for field in ("physical_path", "root_sha256_path", "sha256sums_path"):
                shard_value.pop(field, None)
    errors = normalized.get("model_protocol_errors")
    if isinstance(errors, list):
        for error in errors:
            if not isinstance(error, dict):
                continue
            error.pop("partial_trace_path", None)
            error.pop("raw_response_path", None)
    return normalized


def _logical_rejected_roots(
    records: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]], label: str
) -> dict[str, tuple[str, str]]:
    roots: dict[str, tuple[str, str]] = {}
    for (benchmark, model_id), (_, logical) in records.items():
        for value in _as_list(logical.get("rejected_shards"), f"{label} rejected shards"):
            shard = _as_object(value, f"{label} rejected shard")
            root_hash = _as_sha256(shard.get("root_sha256"), f"{label} rejected shard root hash")
            if root_hash in roots:
                _fail(f"duplicate {label} rejected shard root hash")
            roots[root_hash] = (benchmark, model_id)
    return roots


def _logical_accepted_roots(
    records: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]], label: str
) -> dict[str, tuple[str, str]]:
    roots: dict[str, tuple[str, str]] = {}
    for (benchmark, model_id), (_, logical) in records.items():
        for value in _as_list(logical.get("accepted_shards"), f"{label} accepted shards"):
            shard = _as_object(value, f"{label} accepted shard")
            root_hash = _as_sha256(shard.get("root_sha256"), f"{label} accepted shard root hash")
            roots[root_hash] = (benchmark, model_id)
    return roots


def _provenance_by_root(outer: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    for value in _as_list(outer.get("rejected_provenance"), f"{label} rejected provenance"):
        entry = _as_object(value, f"{label} rejected provenance entry")
        root_hash = _as_sha256(entry.get("root_sha256"), f"{label} rejected provenance root hash")
        if root_hash in provenance:
            _fail(f"duplicate {label} rejected provenance root hash")
        provenance[root_hash] = entry
    return provenance


def _normalized_provenance(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(entry))
    normalized.pop("source_logical_manifest", None)
    return normalized


def _assert_shard_prefix(
    shard: dict[str, Any], benchmark: str, model_id: str, collection: str
) -> None:
    raw_path = Path(_require_text(shard.get("physical_path"), "physical path"))
    expected = ("corpus", collection, benchmark, model_id)
    if raw_path.parts[: len(expected)] != expected:
        _fail(f"{collection} shard physical path has an invalid prefix")


def _authenticated_leaf_bytes(
    root: Path, expected_files: dict[str, str], relative: str, label: str
) -> bytes:
    if relative not in expected_files:
        _fail(f"{label} is not committed by SHA256SUMS")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        _fail(f"{label} must be a regular file")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read {label}: {path}") from error
    if _sha256(content) != expected_files[relative]:
        _fail(f"{label} hash mismatch")
    return content


def _authenticated_leaf_json(
    root: Path, expected_files: dict[str, str], relative: str, label: str
) -> dict[str, Any]:
    return _parse_json_bytes(_authenticated_leaf_bytes(root, expected_files, relative, label), label)


def _verify_named_leaf_hash(
    root: Path,
    expected_files: dict[str, str],
    shard: dict[str, Any],
    field: str,
    relative: str,
    label: str,
    *,
    required: bool,
) -> bytes | None:
    value = shard.get(field)
    if value is None:
        if required:
            _fail(f"missing {label} hash")
        return None
    expected = _as_sha256(value, f"{label} hash")
    content = _authenticated_leaf_bytes(root, expected_files, relative, label)
    if _sha256(content) != expected:
        _fail(f"{label} hash mismatch")
    return content


def _cell_identity(value: Any, label: str) -> tuple[str, str, str, str]:
    cell = _as_object(value, label)
    suite = _require_text(cell.get("suite_name"), f"{label} suite name")
    user_task = _require_text(cell.get("user_task_id"), f"{label} user task ID")
    attack = cell.get("attack_type")
    injection = cell.get("injection_task_id")
    if attack is None:
        attack = "none"
    if injection is None:
        injection = "none"
    return (
        suite,
        user_task,
        _require_text(attack, f"{label} attack type"),
        _require_text(injection, f"{label} injection task ID"),
    )


def _task_id(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        _fail(f"{label} must be a string or integer")
    return _require_text(str(value), label)


def _identity_set(value: Any, label: str) -> set[tuple[str, str, str, str]]:
    result: set[tuple[str, str, str, str]] = set()
    for index, cell in enumerate(_as_list(value, label)):
        identity = _cell_identity(cell, f"{label}[{index}]")
        if identity in result:
            _fail(f"duplicate {label} identity")
        result.add(identity)
    return result


def _verify_agentdojo_capture_identity(
    logical: dict[str, Any],
    shard: dict[str, Any],
    suite: str,
    manifest: dict[str, Any],
    capture_lock: dict[str, Any],
) -> dict[str, str]:
    """Bind the accepted leaf's run metadata to the sealed logical route."""
    model = _as_object(logical.get("model"), "AgentDojo logical model")
    run_config = _as_object(logical.get("run_config"), "AgentDojo logical run config")
    expected_route = {
        "model_id": _require_text(model.get("model"), "AgentDojo logical model name"),
        "response_model": _require_text(
            model.get("response_model"), "AgentDojo logical response model"
        ),
        "provider": _require_text(
            model.get("provider_tag"), "AgentDojo logical provider tag"
        ),
        "endpoint_revision": _require_text(
            model.get("endpoint_revision"), "AgentDojo logical endpoint revision"
        ),
    }
    expected_shared: dict[str, Any] = {
        "run_id": _require_text(shard.get("run_id"), "AgentDojo shard run ID"),
        "model_id": expected_route["model_id"],
        "suites": [suite],
        "response_model": expected_route["response_model"],
        "provider": expected_route["provider"],
        "endpoint_revision": expected_route["endpoint_revision"],
        "benchmark_version": _require_text(
            run_config.get("benchmark_version"), "AgentDojo logical benchmark version"
        ),
        "agentdojo_version": _require_text(
            run_config.get("agentdojo_package_version"), "AgentDojo logical package version"
        ),
        "attack": _require_text(run_config.get("attack"), "AgentDojo logical attack"),
        "temperature": _as_decimal(
            run_config.get("temperature"), "AgentDojo logical temperature"
        ),
        "workers": _as_nonnegative_int(
            run_config.get("workers"), "AgentDojo logical workers"
        ),
    }
    for field, expected in expected_shared.items():
        manifest_value = manifest.get(field)
        capture_value = capture_lock.get(field)
        if field == "temperature":
            matches_manifest = _as_decimal(manifest_value, "AgentDojo manifest temperature") == expected
            matches_lock = _as_decimal(capture_value, "AgentDojo capture lock temperature") == expected
        elif field == "workers":
            matches_manifest = _as_nonnegative_int(
                manifest_value, "AgentDojo manifest workers"
            ) == expected
            matches_lock = _as_nonnegative_int(
                capture_value, "AgentDojo capture lock workers"
            ) == expected
        else:
            matches_manifest = manifest_value == expected
            matches_lock = capture_value == expected
        if not matches_manifest or not matches_lock:
            _fail("AgentDojo manifest identity mismatch")
        if manifest_value != capture_value:
            _fail("AgentDojo capture lock identity mismatch")
    if _as_nonnegative_int(manifest.get("rollout"), "AgentDojo manifest rollout") != _as_nonnegative_int(
        run_config.get("rollout"), "AgentDojo logical rollout"
    ):
        _fail("AgentDojo manifest identity mismatch")
    prototype_commit = _require_text(
        logical.get("prototype_capture_commit"), "AgentDojo logical prototype capture commit"
    )
    eval_git = _require_text(manifest.get("eval_git"), "AgentDojo manifest eval git")
    if not prototype_commit.startswith(eval_git):
        _fail("AgentDojo manifest identity mismatch")
    return {
        "requested_model": expected_route["model_id"],
        "requested_provider_tag": expected_route["provider"],
        "response_model": expected_route["response_model"],
        "provider": _require_text(
            model.get("observed_provider"), "AgentDojo logical observed provider"
        ),
    }


def _parse_raw_responses(
    content: bytes,
    label: str,
    trace_identities: set[tuple[str, str, str, str]],
    global_response_ids: set[str],
    expected_route: dict[str, str],
) -> tuple[dict[str, tuple[str, str, str, str]], Decimal]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} must be UTF-8") from error
    responses: dict[str, tuple[str, str, str, str]] = {}
    cost = Decimal()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            _fail(f"blank {label} line")
        try:
            row = _as_object(json.loads(line), f"{label} line {line_number}")
        except json.JSONDecodeError as error:
            raise ValueError(f"cannot parse {label} line {line_number}") from error
        response_id = _require_text(row.get("response_id"), f"{label} response ID")
        if response_id in responses or response_id in global_response_ids:
            _fail(f"duplicate {label} response ID")
        if row.get("billing_status") != "reported":
            _fail(f"{label} billing status must be reported")
        if row.get("attempt_status") != "response":
            _fail(f"{label} attempt status must be response")
        for field, expected in expected_route.items():
            if row.get(field) != expected:
                _fail(f"{label} route field mismatch: {field}")
        billed = _as_decimal(row.get("billed_cost_usd"), f"{label} billed cost")
        if billed < 0:
            _fail(f"{label} billed cost must be non-negative")
        identity = _cell_identity(row.get("trace_cell"), f"{label} trace cell")
        if identity not in trace_identities:
            _fail(f"{label} trace cell is not in the trace membership")
        responses[response_id] = identity
        global_response_ids.add(response_id)
        cost += billed
    return responses, cost


def _read_agentdojo_shard(
    root: Path,
    expected_files: dict[str, str],
    logical: dict[str, Any],
    shard: dict[str, Any],
    global_response_ids: set[str],
) -> dict[str, Any]:
    suite = _require_text(shard.get("suite"), "accepted shard suite")
    manifest_bytes = _verify_named_leaf_hash(
        root, expected_files, shard, "manifest_sha256", "manifest.json", "AgentDojo manifest", required=True
    )
    results_bytes = _verify_named_leaf_hash(
        root, expected_files, shard, "results_sha256", "results.json", "AgentDojo results", required=True
    )
    raw_bytes = _verify_named_leaf_hash(
        root,
        expected_files,
        shard,
        "raw_responses_sha256",
        "raw_responses.jsonl",
        "AgentDojo raw responses",
        required=True,
    )
    assert manifest_bytes is not None and results_bytes is not None and raw_bytes is not None
    manifest = _parse_json_bytes(manifest_bytes, "AgentDojo manifest")
    _parse_json_bytes(results_bytes, "AgentDojo results")
    capture_lock = _authenticated_leaf_json(
        root, expected_files, "capture-lock.json", "AgentDojo capture lock"
    )
    expected_route = _verify_agentdojo_capture_identity(
        logical, shard, suite, manifest, capture_lock
    )

    trace_paths: dict[tuple[str, str, str, str], Path] = {}
    protocol_identities: set[tuple[str, str, str, str]] = set()
    trace_relatives = [
        relative
        for relative in expected_files
        if relative.startswith("agentdojo/") and relative.endswith(".json")
    ]
    if not trace_relatives:
        _fail("AgentDojo shard has no trace JSON files")
    for relative in sorted(trace_relatives, key=lambda item: item.encode("utf-8")):
        trace = _authenticated_leaf_json(root, expected_files, relative, "AgentDojo trace")
        identity = _cell_identity(trace, "AgentDojo trace cell")
        if identity[0] != suite:
            _fail("AgentDojo trace suite mismatch")
        expected_relative = (
            f"agentdojo/openai-compatible/{identity[0]}/{identity[1]}/"
            f"{identity[2]}/{identity[3]}.json"
        )
        if relative != expected_relative:
            _fail("AgentDojo trace path does not match trace identity")
        if identity in trace_paths:
            _fail("duplicate AgentDojo trace identity")
        trace_paths[identity] = root / relative
        if not (type(trace.get("utility")) is bool and type(trace.get("security")) is bool):
            protocol_identities.add(identity)

    trace_identities = set(trace_paths)
    capture_identities = _identity_set(
        capture_lock.get("expected_trace_cells"), "AgentDojo capture lock expected trace cells"
    )
    manifest_identities = _identity_set(
        manifest.get("expected_trace_cells"), "AgentDojo manifest expected trace cells"
    )
    if trace_identities != capture_identities or trace_identities != manifest_identities:
        _fail("AgentDojo trace membership mismatch")

    responses, raw_cost = _parse_raw_responses(
        raw_bytes,
        "AgentDojo raw responses",
        trace_identities,
        global_response_ids,
        expected_route,
    )
    graded_count = len(trace_identities) - len(protocol_identities)
    if _as_nonnegative_int(shard.get("expected_trace_count"), "AgentDojo shard trace count") != len(trace_identities):
        _fail("AgentDojo trace count mismatch")
    if _as_nonnegative_int(shard.get("graded_trace_count"), "AgentDojo shard graded trace count") != graded_count:
        _fail("AgentDojo graded trace count mismatch")
    if _as_nonnegative_int(
        shard.get("model_protocol_error_count"), "AgentDojo shard protocol error count"
    ) != len(protocol_identities):
        _fail("AgentDojo protocol error count mismatch")
    if _as_nonnegative_int(shard.get("trace_file_count"), "AgentDojo shard trace file count") != len(trace_identities):
        _fail("AgentDojo trace file count mismatch")
    if _as_nonnegative_int(shard.get("raw_response_count"), "AgentDojo shard raw response count") != len(responses):
        _fail("AgentDojo raw response count mismatch")
    if _as_decimal(shard.get("cost_usd"), "AgentDojo shard cost") != raw_cost:
        _fail("AgentDojo raw response cost mismatch")
    return {
        "trace_identities": trace_identities,
        "trace_paths": trace_paths,
        "protocol_identities": protocol_identities,
        "responses": responses,
        "raw_path": root / "raw_responses.jsonl",
        "trace_count": len(trace_identities),
        "graded_count": graded_count,
        "protocol_count": len(protocol_identities),
        "raw_count": len(responses),
        "cost": raw_cost,
    }


def _validate_agentdojo_logical(
    logical: dict[str, Any], metrics: list[dict[str, Any]], cohort_dir: Path
) -> dict[str, Any]:
    all_identities: set[tuple[str, str, str, str]] = set()
    actual_protocol: dict[tuple[str, str, str, str], tuple[Path, Path, dict[str, tuple[str, str, str, str]]]] = {}
    for metric in metrics:
        overlap = all_identities.intersection(metric["trace_identities"])
        if overlap:
            _fail("duplicate AgentDojo trace identity across accepted shards")
        all_identities.update(metric["trace_identities"])
        for identity in metric["protocol_identities"]:
            actual_protocol[identity] = (
                metric["trace_paths"][identity], metric["raw_path"], metric["responses"]
            )

    membership = _as_object(logical.get("logical_membership"), "AgentDojo logical membership")
    lines = ["\t".join(identity) + "\n" for identity in all_identities]
    membership_hash = _sha256("".join(sorted(lines, key=lambda item: item.encode("utf-8"))).encode())
    if _as_nonnegative_int(membership.get("count"), "AgentDojo membership count") != len(all_identities):
        _fail("AgentDojo logical membership count mismatch")
    if _as_sha256(membership.get("sha256"), "AgentDojo membership hash") != membership_hash:
        _fail("AgentDojo logical membership hash mismatch")
    suite_counts: dict[str, int] = {}
    for identity in all_identities:
        suite_counts[identity[0]] = suite_counts.get(identity[0], 0) + 1
    listed_suite_counts = _as_object(membership.get("suite_counts"), "AgentDojo membership suite counts")
    if {
        suite: _as_nonnegative_int(count, "AgentDojo membership suite count")
        for suite, count in listed_suite_counts.items()
    } != suite_counts:
        _fail("AgentDojo logical membership suite counts mismatch")

    declared_value = logical.get("model_protocol_errors")
    if declared_value is None:
        declared: list[Any] = []
    else:
        declared = _as_list(declared_value, "AgentDojo model protocol errors")
    declared_identities: set[tuple[str, str, str, str]] = set()
    for index, value in enumerate(declared):
        error = _as_object(value, "AgentDojo model protocol error")
        identity = _cell_identity(error.get("cell"), "AgentDojo model protocol error cell")
        if identity in declared_identities:
            _fail("duplicate AgentDojo model protocol error identity")
        declared_identities.add(identity)
        if error.get("error_type") != "model_protocol_error":
            _fail("invalid AgentDojo model protocol error type")
        if identity not in actual_protocol:
            _fail("AgentDojo model protocol error identity is not a protocol trace")
        trace_path, raw_path, responses = actual_protocol[identity]
        if _resolve_relative(
            cohort_dir, error.get("partial_trace_path"), "AgentDojo protocol partial trace path"
        ) != trace_path:
            _fail("AgentDojo protocol partial trace path mismatch")
        if _resolve_relative(
            cohort_dir, error.get("raw_response_path"), "AgentDojo protocol raw response path"
        ) != raw_path:
            _fail("AgentDojo protocol raw response path mismatch")
        response_id = _require_text(error.get("raw_response_id"), "AgentDojo protocol raw response ID")
        if responses.get(response_id) != identity:
            _fail("AgentDojo protocol raw response ID mismatch")
    if declared_identities != set(actual_protocol):
        _fail("AgentDojo model protocol error identities mismatch")

    summary = _as_object(logical.get("capture_summary"), "AgentDojo logical capture summary")
    derived = {
        "accepted_shard_count": len(metrics),
        "expected_trace_count": len(all_identities),
        "graded_trace_count": sum(metric["graded_count"] for metric in metrics),
        "model_protocol_error_count": len(actual_protocol),
        "physical_trace_file_count": len(all_identities),
        "raw_response_count": sum(metric["raw_count"] for metric in metrics),
    }
    for field, actual in derived.items():
        _validate_required_count(summary, field, actual, f"AgentDojo logical {field}")
    cost = sum((metric["cost"] for metric in metrics), Decimal())
    _validate_required_decimal(summary, "total_billed_cost_usd", cost, "AgentDojo logical billed cost")
    derived["cost"] = cost
    return derived


def _read_tau_shard(
    root: Path, expected_files: dict[str, str], logical: dict[str, Any], shard: dict[str, Any]
) -> dict[str, Any]:
    domain = _require_text(shard.get("domain"), "accepted shard domain")
    manifest_bytes = _verify_named_leaf_hash(
        root, expected_files, shard, "manifest_sha256", "manifest.json", "Tau manifest", required=True
    )
    results_bytes = _verify_named_leaf_hash(
        root, expected_files, shard, "results_sha256", "results.json", "Tau results", required=True
    )
    assert manifest_bytes is not None and results_bytes is not None
    manifest = _parse_json_bytes(manifest_bytes, "Tau manifest")
    results = _parse_json_bytes(results_bytes, "Tau results")
    capture_lock = _authenticated_leaf_json(root, expected_files, "capture-lock.json", "Tau capture lock")
    capture_config = _as_object(manifest.get("capture_config"), "Tau manifest capture config")
    if capture_lock != capture_config:
        _fail("Tau capture lock does not match manifest capture config")
    model = _as_object(logical.get("model"), "Tau logical model")
    logical_model = _require_text(model.get("model"), "Tau logical model name")
    expected_route = {
        "agent": logical_model,
        "response_model": logical_model,
        "provider": _require_text(model.get("provider_tag"), "Tau logical provider tag"),
        "endpoint_revision": _require_text(
            model.get("endpoint_revision"), "Tau logical endpoint revision"
        ),
    }
    for field, expected in expected_route.items():
        if capture_config.get(field) != expected:
            _fail("Tau capture identity mismatch")
    for field, logical_field in (
        ("prototype_commit", "prototype_capture_commit"),
        ("tau2_commit", "tau2_capture_commit"),
    ):
        expected = _require_text(logical.get(logical_field), f"Tau logical {logical_field}")
        if capture_config.get(field) != expected or manifest.get(field) != expected:
            _fail("Tau capture commit mismatch")
    routes = _as_object(capture_config.get("routes"), "Tau capture routes")
    pinned_route = _as_object(
        routes.get(f"openrouter/{expected_route['agent']}"), "Tau pinned agent route"
    )
    for field in ("response_model", "provider", "endpoint_revision"):
        if pinned_route.get(field) != expected_route[field]:
            _fail("Tau pinned agent route mismatch")
    if capture_config.get("domain") != domain:
        _fail("Tau capture config domain mismatch")
    if manifest.get("status") != "PASS":
        _fail("Tau manifest status must be PASS")
    validation = manifest.get("validation")
    if validation is not None and _as_object(validation, "Tau manifest validation").get("ok") is not True:
        _fail("Tau manifest validation failed")
    tau_results = _as_object(manifest.get("tau_results"), "Tau manifest tau results")
    if tau_results.get("ok") is not True:
        _fail("Tau manifest tau results failed")
    for field in ("missing_task_trials", "unexpected_task_trials", "duplicate_task_trials", "errors"):
        if field in tau_results and _as_list(tau_results[field], f"Tau manifest {field}"):
            _fail(f"Tau manifest {field} must be empty")

    task_ids = [_require_text(task_id, "Tau capture task ID") for task_id in _as_list(capture_config.get("task_ids"), "Tau capture task IDs")]
    if len(task_ids) != len(set(task_ids)):
        _fail("duplicate Tau capture task ID")
    trial_count = _as_nonnegative_int(capture_config.get("num_trials"), "Tau capture trial count")
    expected = {(domain, task_id, trial) for task_id in task_ids for trial in range(trial_count)}
    simulations: set[tuple[str, str, int]] = set()
    for index, row_value in enumerate(_as_list(results.get("simulations"), "Tau simulations")):
        row = _as_object(row_value, f"Tau simulation {index}")
        identity = (
            domain,
            _task_id(row.get("task_id"), "Tau simulation task ID"),
            _as_nonnegative_int(row.get("trial"), "Tau simulation trial"),
        )
        if identity in simulations:
            _fail("duplicate Tau simulation identity")
        simulations.add(identity)
    if simulations != expected:
        _fail("Tau simulation membership mismatch")
    task_rows = _as_list(results.get("tasks"), "Tau tasks")
    listed_tasks = {
        _task_id(_as_object(task, "Tau task").get("id"), "Tau task ID")
        for task in task_rows
    }
    if listed_tasks != set(task_ids) or len(task_rows) != len(listed_tasks):
        _fail("Tau task membership mismatch")
    if _as_nonnegative_int(shard.get("simulation_count"), "Tau shard simulation count") != len(simulations):
        _fail("Tau simulation count mismatch")

    generation_ids = [_require_text(value, "Tau generation ID") for value in _as_list(manifest.get("generation_ids"), "Tau generation IDs")]
    if len(generation_ids) != len(set(generation_ids)):
        _fail("duplicate Tau manifest generation ID")
    debug_paths = [
        relative
        for relative in expected_files
        if relative.startswith("artifacts/") and "/llm_debug/" in relative and relative.endswith(".json")
    ]
    debug_ids: set[str] = set()
    debug_cost = Decimal()
    for relative in debug_paths:
        debug = _authenticated_leaf_json(root, expected_files, relative, "Tau LLM debug record")
        response = _as_object(debug.get("response"), "Tau LLM debug response")
        raw_response = _as_object(response.get("raw_response"), "Tau LLM raw response")
        generation_id = _require_text(raw_response.get("id"), "Tau LLM generation ID")
        if generation_id in debug_ids:
            _fail("duplicate Tau LLM generation ID")
        debug_ids.add(generation_id)
        cost = _as_decimal(
            _as_object(raw_response.get("usage"), "Tau LLM usage").get("cost"), "Tau LLM cost"
        )
        if cost < 0:
            _fail("Tau LLM cost must be non-negative")
        debug_cost += cost
    if debug_ids != set(generation_ids):
        _fail("Tau LLM generation ID membership mismatch")
    llm_calls = _as_object(manifest.get("llm_calls"), "Tau manifest LLM calls")
    if _as_nonnegative_int(llm_calls.get("generation_count"), "Tau LLM generation count") != len(debug_ids):
        _fail("Tau LLM generation count mismatch")
    if _as_list(llm_calls.get("errors"), "Tau LLM errors"):
        _fail("Tau LLM errors must be empty")
    if _as_decimal(llm_calls.get("cost_usd"), "Tau LLM cost") != debug_cost:
        _fail("Tau LLM cost mismatch")
    if _as_decimal(shard.get("cost_usd"), "Tau shard cost") != debug_cost:
        _fail("Tau shard cost mismatch")
    return {"identities": simulations, "count": len(simulations), "cost": debug_cost}


def load_cohort(lock_path: Path, verify_leaf_files: bool = True) -> Cohort:
    """Fail closed over the sealed current capture and its original provenance."""
    if verify_leaf_files is not True:
        _fail("leaf verification cannot be disabled")
    lock_path = Path(lock_path)
    if lock_path.is_symlink() or not lock_path.is_file():
        _fail("cohort lock must be a regular file")
    cohort_dir = lock_path.parent.resolve(strict=True)
    lock = _read_json(lock_path, "cohort lock")
    cohort_id = _require_text(lock.get("cohort_id"), "cohort ID")
    if cohort_id != "paper_main_v1":
        _fail("unsupported cohort ID")
    if _as_nonnegative_int(lock.get("schema_version"), "lock schema_version") != 1:
        _fail("lock schema_version mismatch")
    if lock.get("status") != "active_local_source":
        _fail("lock status mismatch")
    lock_summary = _as_object(lock.get("accepted_capture_summary"), "lock accepted summary")
    _validate_accepted_summary_schema(lock_summary, "lock accepted summary")

    outer_reference = _as_object(lock.get("current_outer_manifest"), "current outer manifest")
    _require_hash_sidecar_reference(outer_reference, "current outer manifest")
    outer_path, outer = _verify_hashed_json(cohort_dir, outer_reference, "outer manifest")
    _validate_paper_main_outer_profile(outer, "current outer")
    outer_summary = _as_object(outer.get("accepted_capture_summary"), "outer accepted summary")
    _validate_accepted_summary_schema(outer_summary, "outer accepted summary")
    if lock_summary != outer_summary:
        _fail("lock accepted summary does not match outer manifest")

    original_reference = _as_object(lock.get("original_outer_manifest"), "original outer manifest")
    _require_hash_sidecar_reference(original_reference, "original outer manifest")
    if original_reference.get("sha256") != _PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256:
        _fail("paper_main_v1 original outer manifest hash anchor mismatch")
    original_outer_path, original_outer = _verify_hashed_json(
        cohort_dir, original_reference, "original outer manifest"
    )
    _validate_paper_main_outer_profile(original_outer, "original outer")
    original_summary = _as_object(
        original_outer.get("accepted_capture_summary"), "original outer accepted summary"
    )
    _validate_accepted_summary_schema(original_summary, "original outer accepted summary")
    if original_summary != outer_summary:
        _fail("original outer accepted summary does not match current outer manifest")

    current_records, current_contract_hash, current_catalogs = _verify_outer_metadata(
        outer, cohort_dir, "outer"
    )
    original_records, original_contract_hash, original_catalogs = _verify_outer_metadata(
        original_outer, original_outer_path.parent, "original outer"
    )
    if current_contract_hash != original_contract_hash or current_catalogs != original_catalogs:
        _fail("current outer asset chain does not match original outer")
    if set(current_records) != set(original_records):
        _fail("current logical manifest identities do not match original outer")
    for key, (_, logical) in current_records.items():
        if _normalized_original_logical(logical) != _normalized_original_logical(original_records[key][1]):
            _fail(f"current logical manifest facts changed from original: {key[0]}/{key[1]}")

    current_accepted_metadata = _logical_accepted_roots(current_records, "outer")
    current_rejected_metadata = _logical_rejected_roots(current_records, "outer")
    original_accepted_metadata = _logical_accepted_roots(original_records, "original outer")
    original_rejected_metadata = _logical_rejected_roots(original_records, "original outer")
    if set(current_accepted_metadata).intersection(current_rejected_metadata):
        _fail("duplicate root SHA-256 across accepted and rejected shards")
    if set(original_accepted_metadata).intersection(original_rejected_metadata):
        _fail("duplicate root SHA-256 across accepted and rejected shards")
    current_provenance = _provenance_by_root(outer, "outer")
    original_provenance = _provenance_by_root(original_outer, "original outer")
    for label, provenance, rejected_metadata in (
        ("outer", current_provenance, current_rejected_metadata),
        ("original outer", original_provenance, original_rejected_metadata),
    ):
        if set(provenance) != set(rejected_metadata):
            _fail(f"{label} rejected provenance does not match rejected roots")
        for root_hash, entry in provenance.items():
            benchmark, model_id = rejected_metadata[root_hash]
            if entry.get("benchmark") != benchmark or entry.get("model_id") != model_id:
                _fail(f"{label} rejected provenance identity mismatch")
    if set(current_provenance) != set(original_provenance) or any(
        _normalized_provenance(current_provenance[root_hash])
        != _normalized_provenance(original_provenance[root_hash])
        for root_hash in current_provenance
    ):
        _fail("current rejected provenance facts changed from original")

    accepted_shards: list[AcceptedShard] = []
    seen_paths: set[Path] = set()
    root_paths: dict[str, Path] = {}
    global_response_ids: set[str] = set()
    agent_metrics: dict[str, list[dict[str, Any]]] = {}
    tau_metrics: dict[str, list[dict[str, Any]]] = {}
    rejected_roots: dict[str, tuple[str, str]] = {}

    def register_leaf(path: Path, root_hash: str) -> None:
        if path in seen_paths:
            _fail(f"duplicate physical path: {path}")
        seen_paths.add(path)
        prior_path = root_paths.get(root_hash)
        if prior_path is not None and prior_path != path:
            _fail(f"duplicate root SHA-256 across physical paths: {root_hash}")
        root_paths[root_hash] = path

    for (benchmark, model_id), (record, logical) in current_records.items():
        if benchmark not in {"agentdojo", "tau"}:
            _fail(f"unsupported benchmark: {benchmark}")
        accepted = [
            _as_object(value, "accepted shard")
            for value in _as_list(logical.get("accepted_shards"), "accepted shards")
        ]
        rejected = [
            _as_object(value, "rejected shard")
            for value in _as_list(logical.get("rejected_shards"), "rejected shards")
        ]
        _validate_logical_summary(logical, benchmark, accepted)
        if _as_nonnegative_int(record.get("rejected_root_count"), "outer logical rejected root count") != len(rejected):
            _fail("outer logical rejected root count mismatch")

        per_record_agent: list[dict[str, Any]] = []
        per_record_tau: list[dict[str, Any]] = []
        for shard in accepted:
            _assert_shard_prefix(shard, benchmark, model_id, "accepted")
            kind_field = "suite" if benchmark == "agentdojo" else "domain"
            kind = _require_text(shard.get(kind_field), f"accepted shard {kind_field}")
            physical_path = _resolve_relative(cohort_dir, shard.get("physical_path"), "physical path")
            root_hash = _as_sha256(shard.get("root_sha256"), "accepted shard root hash")
            register_leaf(physical_path, root_hash)
            verified_path, verified_root, expected_files = _validate_leaf(shard, cohort_dir)
            if verified_path != physical_path or verified_root != root_hash:
                _fail("accepted shard leaf identity mismatch")
            if benchmark == "agentdojo":
                metric = _read_agentdojo_shard(
                    verified_path, expected_files, logical, shard, global_response_ids
                )
                per_record_agent.append(metric)
                accepted_count = metric["trace_count"]
            else:
                metric = _read_tau_shard(verified_path, expected_files, logical, shard)
                per_record_tau.append(metric)
                accepted_count = metric["count"]
            accepted_shards.append(
                AcceptedShard(
                    benchmark=benchmark,
                    model_id=model_id,
                    kind=kind,
                    path=physical_path,
                    root_sha256=root_hash,
                    accepted_count=accepted_count,
                )
            )

        for shard in rejected:
            _assert_shard_prefix(shard, benchmark, model_id, "rejected")
            physical_path = _resolve_relative(cohort_dir, shard.get("physical_path"), "physical path")
            root_hash = _as_sha256(shard.get("root_sha256"), "rejected shard root hash")
            register_leaf(physical_path, root_hash)
            verified_path, verified_root, expected_files = _validate_leaf(shard, cohort_dir)
            if verified_path != physical_path or verified_root != root_hash:
                _fail("rejected shard leaf identity mismatch")
            for field, relative, label in (
                ("manifest_sha256", "manifest.json", "rejected manifest"),
                ("results_sha256", "results.json", "rejected results"),
                ("raw_responses_sha256", "raw_responses.jsonl", "rejected raw responses"),
            ):
                _verify_named_leaf_hash(
                    verified_path, expected_files, shard, field, relative, label, required=False
                )
            rejected_roots[root_hash] = (benchmark, model_id)

        if benchmark == "agentdojo":
            derived = _validate_agentdojo_logical(logical, per_record_agent, cohort_dir)
            if _as_nonnegative_int(record.get("accepted_count"), "outer logical accepted count") != derived["expected_trace_count"]:
                _fail("outer logical accepted count mismatch")
            if _as_nonnegative_int(record.get("graded_count"), "outer logical graded count") != derived["graded_trace_count"]:
                _fail("outer logical graded count mismatch")
            if _as_nonnegative_int(record.get("model_protocol_error_count"), "outer logical protocol error count") != derived["model_protocol_error_count"]:
                _fail("outer logical protocol error count mismatch")
            if _as_decimal(record.get("billed_cost_usd"), "outer logical billed cost") != derived["cost"]:
                _fail("outer logical billed cost mismatch")
            agent_metrics[model_id] = per_record_agent
        else:
            count = sum(metric["count"] for metric in per_record_tau)
            cost = sum((metric["cost"] for metric in per_record_tau), Decimal())
            membership: set[tuple[str, str, int]] = set()
            for metric in per_record_tau:
                if membership.intersection(metric["identities"]):
                    _fail("duplicate Tau simulation identity across accepted shards")
                membership.update(metric["identities"])
            if _as_nonnegative_int(record.get("accepted_count"), "outer logical accepted count") != count:
                _fail("outer logical accepted count mismatch")
            if _as_decimal(record.get("billed_cost_usd"), "outer logical billed cost") != cost:
                _fail("outer logical billed cost mismatch")
            tau_metrics[model_id] = per_record_tau

    if set(rejected_roots) != set(current_rejected_metadata):
        _fail("outer rejected provenance does not match rejected roots")
    for root_hash, entry in current_provenance.items():
        benchmark, model_id = rejected_roots[root_hash]
        if entry.get("benchmark") != benchmark or entry.get("model_id") != model_id:
            _fail("outer rejected provenance identity mismatch")
    if set(current_provenance) != set(rejected_roots):
        _fail("outer rejected provenance does not match rejected roots")

    _validate_required_count(
        outer_summary,
        "accepted_physical_root_count",
        len(accepted_shards),
        "accepted physical root count",
    )
    _validate_required_count(
        outer_summary,
        "rejected_physical_root_count",
        len(rejected_roots),
        "rejected physical root count",
    )
    agent_count = sum(metric["trace_count"] for values in agent_metrics.values() for metric in values)
    agent_graded = sum(metric["graded_count"] for values in agent_metrics.values() for metric in values)
    agent_protocol = sum(metric["protocol_count"] for values in agent_metrics.values() for metric in values)
    agent_raw = sum(metric["raw_count"] for values in agent_metrics.values() for metric in values)
    agent_cost = sum((metric["cost"] for values in agent_metrics.values() for metric in values), Decimal())
    tau_count = sum(metric["count"] for values in tau_metrics.values() for metric in values)
    tau_cost = sum((metric["cost"] for values in tau_metrics.values() for metric in values), Decimal())
    _validate_required_decimal(
        outer_summary,
        "accepted_billed_cost_usd",
        agent_cost + tau_cost,
        "accepted billed cost",
    )

    agent_summary = _required_object(outer_summary, "agentdojo", "outer accepted summary")
    _validate_required_count(agent_summary, "accepted_trace_cell_count", agent_count, "AgentDojo accepted count")
    _validate_required_count(agent_summary, "graded_trace_count", agent_graded, "AgentDojo graded trace count")
    _validate_required_count(agent_summary, "model_protocol_error_count", agent_protocol, "AgentDojo model protocol error count")
    _validate_required_count(agent_summary, "raw_response_count", agent_raw, "AgentDojo raw response count")
    _validate_required_decimal(agent_summary, "billed_cost_usd", agent_cost, "AgentDojo billed cost")
    agent_models = _required_object(agent_summary, "models", "AgentDojo accepted summary")
    if set(agent_models) != set(agent_metrics):
        _fail("AgentDojo model summary membership mismatch")
    for model_id, metrics in agent_metrics.items():
        summary = _as_object(agent_models[model_id], f"AgentDojo model summary {model_id}")
        _validate_required_count(summary, "accepted_trace_cell_count", sum(metric["trace_count"] for metric in metrics), "AgentDojo model accepted count")
        _validate_required_count(summary, "graded_trace_count", sum(metric["graded_count"] for metric in metrics), "AgentDojo model graded trace count")
        _validate_required_count(summary, "model_protocol_error_count", sum(metric["protocol_count"] for metric in metrics), "AgentDojo model protocol error count")
        _validate_required_count(summary, "raw_response_count", sum(metric["raw_count"] for metric in metrics), "AgentDojo model raw response count")
        _validate_required_decimal(summary, "billed_cost_usd", sum((metric["cost"] for metric in metrics), Decimal()), "AgentDojo model billed cost")
    agent_memberships = [
        set().union(*(metric["trace_identities"] for metric in metrics))
        for metrics in agent_metrics.values()
    ]
    if agent_memberships and any(
        membership != agent_memberships[0] for membership in agent_memberships[1:]
    ):
        _fail("AgentDojo model trace memberships differ")

    tau_summary = _required_object(outer_summary, "tau", "outer accepted summary")
    _validate_required_count(tau_summary, "accepted_simulation_count", tau_count, "Tau accepted count")
    _validate_required_decimal(tau_summary, "billed_cost_usd", tau_cost, "Tau billed cost")
    tau_models = _required_object(tau_summary, "models", "Tau accepted summary")
    if set(tau_models) != set(tau_metrics):
        _fail("Tau model summary membership mismatch")
    for model_id, metrics in tau_metrics.items():
        summary = _as_object(tau_models[model_id], f"Tau model summary {model_id}")
        _validate_required_count(summary, "accepted_simulation_count", sum(metric["count"] for metric in metrics), "Tau model accepted count")
        _validate_required_decimal(summary, "billed_cost_usd", sum((metric["cost"] for metric in metrics), Decimal()), "Tau model billed cost")
    tau_memberships = [
        set().union(*(metric["identities"] for metric in metrics)) for metrics in tau_metrics.values()
    ]
    if tau_memberships and any(membership != tau_memberships[0] for membership in tau_memberships[1:]):
        _fail("Tau model simulation memberships differ")

    return Cohort(
        cohort_id=cohort_id,
        lock_path=lock_path.resolve(strict=True),
        outer_manifest_path=outer_path,
        accepted_shards=tuple(accepted_shards),
    )


def load_paper_main_v1(lock_path: Path = PAPER_MAIN_V1_LOCK) -> EvaluationCohort:
    """Load the one supported cohort through its fail-closed sealed loader."""

    cohort = load_cohort(Path(lock_path))
    if cohort.cohort_id != "paper_main_v1":
        raise ValueError(f"unsupported evaluation cohort: {cohort.cohort_id}")
    return EvaluationCohort(
        cohort=cohort,
        outer_sha256=sha256_file(cohort.outer_manifest_path),
    )


def _dataset_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields mismatch")


def _archive_filename(value: Any) -> str:
    filename = _require_text(value, "archive filename")
    path = Path(filename)
    if (
        path.name != filename
        or path.is_absolute()
        or any(unicodedata.category(character) == "Cc" for character in filename)
    ):
        raise ValueError("archive filename must be a safe filename")
    return filename


def load_dataset_metadata(path: Path = PAPER_MAIN_V1_METADATA) -> DatasetMetadata:
    """Load the strict, versioned paper-main archive descriptor."""

    metadata_path = Path(path)
    value = _read_json(metadata_path, "dataset metadata")
    _exact_keys(
        value,
        {"schema_version", "cohort_id", "corpus_directory", "archive"},
        "dataset metadata",
    )
    if value.get("schema_version") != 1:
        raise ValueError("dataset metadata schema_version must be 1")
    if value.get("cohort_id") != "paper_main_v1":
        raise ValueError("dataset metadata cohort_id must be paper_main_v1")
    if value.get("corpus_directory") != "corpus":
        raise ValueError("dataset metadata corpus_directory must be corpus")

    archive_value = value.get("archive")
    archive: ArchiveMetadata | None
    if archive_value is None:
        archive = None
    else:
        archive_object = _dataset_object(archive_value, "dataset archive")
        _exact_keys(
            archive_object,
            {"filename", "sha256", "bytes", "url"},
            "dataset archive",
        )
        byte_size = archive_object.get("bytes")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size <= 0:
            raise ValueError("archive bytes must be a positive integer")
        url = archive_object.get("url")
        if url is not None:
            if not isinstance(url, str):
                raise ValueError("archive URL must use HTTPS")
            parsed_url = urllib.parse.urlsplit(url)
            if parsed_url.scheme != "https" or not parsed_url.netloc:
                raise ValueError("archive URL must use HTTPS")
        archive = ArchiveMetadata(
            filename=_archive_filename(archive_object.get("filename")),
            sha256=_as_sha256(archive_object.get("sha256"), "archive"),
            bytes=byte_size,
            url=url,
        )
    return DatasetMetadata(
        schema_version=1,
        cohort_id="paper_main_v1",
        corpus_directory="corpus",
        archive=archive,
    )


def build_paper_main_archive(corpus: Path, destination: Path) -> ArchiveMetadata:
    """Build the sealed corpus as a deterministic ``corpus/`` tar.gz tree."""

    source = Path(corpus)
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"corpus source must be a regular directory: {source}")
    archive_path = Path(destination)
    _archive_filename(archive_path.name)
    if archive_path.resolve(strict=False).is_relative_to(source.resolve(strict=True)):
        raise ValueError("archive destination must be outside corpus source")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.is_symlink() or (
        archive_path.exists() and not archive_path.is_file()
    ):
        raise ValueError(f"archive destination must be a regular file: {archive_path}")

    source_entries = list(source.rglob("*"))
    for path in source_entries:
        if path.is_symlink():
            raise ValueError(f"symlink not allowed in corpus archive: {path}")
        if not path.is_dir() and not path.is_file():
            raise ValueError(f"non-regular corpus archive entry: {path}")
    entries = [(PurePosixPath("corpus"), source)]
    entries.extend(
        (PurePosixPath("corpus") / path.relative_to(source).as_posix(), path)
        for path in source_entries
    )
    entries.sort(key=lambda item: item[0].as_posix().encode("utf-8"))

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw_archive:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_archive,
                mtime=0,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
                ) as archive:
                    for archive_name, source_path in entries:
                        info = tarfile.TarInfo(archive_name.as_posix())
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        if source_path.is_dir():
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            _validate_archive_member(info)
                            archive.addfile(info)
                        else:
                            info.type = tarfile.REGTYPE
                            info.mode = 0o644
                            info.size = source_path.stat().st_size
                            _validate_archive_member(info)
                            with source_path.open("rb") as content:
                                archive.addfile(info, content)
        os.replace(temporary_path, archive_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return ArchiveMetadata(
        filename=archive_path.name,
        sha256=sha256_file(archive_path),
        bytes=archive_path.stat().st_size,
        url=None,
    )


def _verify_committed_checksums(root: Path) -> None:
    manifest = root / "SHA256SUMS"
    if manifest.is_symlink():
        raise ValueError("committed SHA256SUMS must be a regular file")
    if not manifest.exists():
        return
    if not manifest.is_file():
        raise ValueError("committed SHA256SUMS must be a regular file")
    entries = _parse_sha256sums(manifest.read_bytes())
    for relative, expected in entries.items():
        evidence = _resolve_relative(root, relative, "committed evidence")
        if not evidence.is_file() or evidence.is_symlink():
            raise ValueError(f"committed evidence must be a regular file: {relative}")
        if sha256_file(evidence) != expected:
            raise ValueError(f"committed evidence checksum mismatch: {relative}")


def _require_dataset_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"dataset root must be a regular directory: {root}")


def verify_paper_main_v1(*, root: Path = PAPER_MAIN_V1_ROOT) -> EvaluationCohort:
    """Verify the sealed corpus and optional committed evidence without mutation."""

    dataset_root = Path(root)
    _require_dataset_root(dataset_root)
    load_dataset_metadata(dataset_root / "dataset.json")
    verified = load_paper_main_v1(dataset_root / "cohort.lock.json")
    _verify_committed_checksums(dataset_root)
    return verified


def _validate_archive_member(member: tarfile.TarInfo) -> PurePosixPath:
    name = member.name
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != name
        or "\\" in name
        or any(unicodedata.category(character) == "Cc" for character in name)
    ):
        raise ValueError(f"archive member must be a safe relative path: {name!r}")
    if not path.parts or path.parts[0] != "corpus":
        raise ValueError("archive must contain exactly one top-level corpus tree")
    if not (member.isdir() or member.isreg()):
        raise ValueError("archive may contain only regular files and directories")
    if len(path.parts) == 1 and not member.isdir():
        raise ValueError("archive top-level corpus member must be a directory")
    return path


def _extract_archive(archive_path: Path, destination: Path) -> Path:
    try:
        archive = tarfile.open(archive_path, mode="r:*")
    except (OSError, tarfile.TarError) as error:
        raise ValueError("cannot read dataset archive") from error
    with archive:
        members = archive.getmembers()
        if not members:
            raise ValueError("dataset archive is empty")
        validated: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        seen: set[str] = set()
        for member in members:
            relative = _validate_archive_member(member)
            normalized = relative.as_posix()
            if normalized in seen:
                raise ValueError(f"duplicate archive member: {normalized}")
            seen.add(normalized)
            validated.append((member, relative))

        for member, relative in sorted(
            validated, key=lambda item: (len(item[1].parts), item[1].as_posix())
        ):
            target = destination.joinpath(*relative.parts)
            try:
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    if not target.is_dir():
                        raise ValueError(f"conflicting archive member: {relative}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read archive member: {relative}")
                with source, target.open("xb") as handle:
                    shutil.copyfileobj(source, handle)
            except OSError as error:
                raise ValueError(f"conflicting archive member: {relative}") from error
    corpus = destination / "corpus"
    if corpus.is_symlink() or not corpus.is_dir():
        raise ValueError("archive lacks a regular top-level corpus directory")
    return corpus


def _require_restore_target(corpus: Path) -> bool:
    if corpus.is_symlink():
        raise ValueError("corpus target must be absent or empty")
    if not corpus.exists():
        return False
    if not corpus.is_dir() or any(corpus.iterdir()):
        raise ValueError("corpus target must be absent or empty")
    return True


def _download_archive(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url) as response, destination.open("xb") as handle:
        shutil.copyfileobj(response, handle)


def fetch_paper_main_v1(
    archive_path: Path | None = None,
    *,
    archive_sha256: str | None = None,
    archive_bytes: int | None = None,
    root: Path = PAPER_MAIN_V1_ROOT,
) -> EvaluationCohort:
    """Restore the sealed corpus from verified local or HTTPS archive bytes."""

    dataset_root = Path(root)
    _require_dataset_root(dataset_root)
    metadata = load_dataset_metadata(dataset_root / "dataset.json")
    corpus = dataset_root / metadata.corpus_directory
    target_existed = _require_restore_target(corpus)

    local_override = archive_path is not None
    if local_override:
        if metadata.archive is None:
            if archive_sha256 is None or archive_bytes is None:
                raise ValueError(
                    "unconfigured local restore requires --sha256 and --bytes"
                )
            expected_sha256 = _as_sha256(archive_sha256, "local archive")
            if (
                isinstance(archive_bytes, bool)
                or not isinstance(archive_bytes, int)
                or archive_bytes <= 0
            ):
                raise ValueError("local archive bytes must be a positive integer")
            expected_bytes = archive_bytes
        else:
            if archive_sha256 is not None or archive_bytes is not None:
                raise ValueError(
                    "local hash and size overrides require unconfigured archive metadata"
                )
            expected_sha256 = metadata.archive.sha256
            expected_bytes = metadata.archive.bytes
        source_archive = Path(archive_path)
        download_url = None
    else:
        if metadata.archive is None:
            raise ValueError("paper_main_v1 archive is not configured")
        if archive_sha256 is not None or archive_bytes is not None:
            raise ValueError("hash and size overrides require --archive")
        if metadata.archive.url is None:
            raise ValueError("paper_main_v1 archive has no HTTPS URL")
        expected_sha256 = metadata.archive.sha256
        expected_bytes = metadata.archive.bytes
        source_archive = None
        download_url = metadata.archive.url

    with tempfile.TemporaryDirectory(
        prefix=".paper-main-v1-restore-", dir=dataset_root.parent
    ) as temporary:
        staging = Path(temporary)
        if source_archive is None:
            configured_archive = metadata.archive
            if configured_archive is None or download_url is None:
                raise AssertionError("configured archive invariant")
            source_archive = staging / configured_archive.filename
            _download_archive(download_url, source_archive)
        if source_archive.is_symlink() or not source_archive.is_file():
            raise ValueError(f"dataset archive must be a regular file: {source_archive}")
        if source_archive.stat().st_size != expected_bytes:
            raise ValueError("dataset archive byte size mismatch")
        if sha256_file(source_archive) != expected_sha256:
            raise ValueError("dataset archive SHA-256 mismatch")

        staged_corpus = _extract_archive(source_archive, staging / "extracted")
        if target_existed:
            corpus.rmdir()
        os.replace(staged_corpus, corpus)
        try:
            return verify_paper_main_v1(root=dataset_root)
        except Exception:
            if corpus.exists():
                shutil.rmtree(corpus)
            if target_existed:
                corpus.mkdir()
            raise
