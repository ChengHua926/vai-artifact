"""Pure, fail-closed integrity checks for AgentDojo capture artifacts."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


CELL_FIELDS = ("suite_name", "user_task_id", "attack_type", "injection_task_id")


def provider_preferences(tag: str) -> dict[str, dict[str, object]]:
    """Return the OpenRouter request body that admits only one endpoint tag."""
    if not isinstance(tag, str) or not tag.strip():
        raise ValueError("provider tag must be a nonempty string")
    return {"provider": {"order": [tag], "allow_fallbacks": False}}


def expected_trace_cells(suite_definitions: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Expand frozen suite/user/injection inputs into required attack and benign cells."""
    cells: list[dict[str, str]] = []
    for definition in suite_definitions:
        suite_name = definition["suite_name"]
        user_task_ids = definition["user_task_ids"]
        injection_task_ids = definition["injection_task_ids"]
        for user_task_id in user_task_ids:
            for injection_task_id in injection_task_ids:
                cells.append(
                    {
                        "suite_name": suite_name,
                        "user_task_id": user_task_id,
                        "attack_type": "important_instructions",
                        "injection_task_id": injection_task_id,
                    }
                )
            cells.append(
                {
                    "suite_name": suite_name,
                    "user_task_id": user_task_id,
                    "attack_type": "none",
                    "injection_task_id": "none",
                }
            )
    return cells


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() and parsed >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def billed_cost_decimal(value: Any) -> Decimal:
    """Parse one OpenRouter billed cost in the single domain used at runtime and review."""
    parsed = _as_decimal(value)
    if parsed is None:
        raise ValueError("invalid billed cost")
    return parsed


_ERROR_RESPONSE_METADATA_KEYS = {
    "id", "model", "provider", "created", "object", "service_tier",
    "system_fingerprint", "moderation", "usage", "finish_reasons", "error",
    "route_conflicts",
}


def _insured_zero_completion(record: dict[str, Any]) -> bool:
    if record.get("attempt_status") != "error" or record.get("output_status") != "empty":
        return False
    usage = record.get("usage")
    if not isinstance(usage, dict):
        return False
    counters = [usage[key] for key in ("completion_tokens", "output_tokens") if key in usage]
    if not counters or any(not isinstance(value, int) or isinstance(value, bool) or value != 0 for value in counters):
        return False
    return record.get("finish_reason") in (None, "", "error")


def raw_billed_cost_total(path: Path) -> Decimal:
    """Return a validated cumulative billed cost for a resumable raw response log."""
    total = Decimal("0")
    if not path.exists():
        return total
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid raw response JSON at line {line_number}") from error
        if not isinstance(record, dict):
            raise ValueError(f"raw response at line {line_number} is not an object")
        billing_status = record.get("billing_status", "reported")
        if billing_status == "not_reported":
            if record.get("billed_cost_usd") is not None:
                raise ValueError(f"not-billed attempt has a cost at line {line_number}")
            if not _insured_zero_completion(record):
                raise ValueError(f"not-billed attempt lacks zero-completion evidence at line {line_number}")
            continue
        if billing_status != "reported":
            raise ValueError(f"billing status is incomplete at line {line_number}")
        total += billed_cost_decimal(record.get("billed_cost_usd"))
    return total


def ensure_capture_lock(run_dir: Path, config: dict[str, Any]) -> str:
    """Create or verify the immutable configuration required to resume a capture directory."""
    lock_path = run_dir / "capture-lock.json"
    existing = [path for path in run_dir.iterdir() if path.name != "capture-lock.json"] if run_dir.exists() else []
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


def preflight_capture_artifacts(run_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Validate a matched-lock directory before a resumed capture can issue another request."""
    raw_path = run_dir / "raw_responses.jsonl"
    logdir = run_dir / "agentdojo"
    trace_files = list(logdir.rglob("*.json")) if logdir.exists() else []
    if not raw_path.exists() and not trace_files:
        return {"ok": True, "state": "fresh", "raw_responses": None, "trace_corpus": None}
    if not raw_path.exists() or not trace_files:
        raw_validation = None
        if raw_path.exists():
            raw_validation = validate_raw_response_summary(
                summarize_raw_responses(raw_path),
                model_id=config["model_id"], provider=config["provider"],
                response_model=config["response_model"], endpoint_revision=config["endpoint_revision"],
                max_cost_usd=config["max_cost_usd"],
                expected_cells=config["expected_trace_cells"], require_complete=False,
            )
        return {
            "ok": False,
            "state": "incomplete",
            "raw_responses": raw_validation,
            "trace_corpus": None,
            "errors": ["resume artifacts are incomplete; use a fresh run ID"],
        }
    raw_summary = summarize_raw_responses(raw_path)
    raw_validation = validate_raw_response_summary(
        raw_summary,
        model_id=config["model_id"],
        provider=config["provider"],
        response_model=config["response_model"],
        endpoint_revision=config["endpoint_revision"],
        max_cost_usd=config["max_cost_usd"],
        expected_cells=config["expected_trace_cells"],
        require_complete=False,
    )
    if raw_validation["cost_complete"] and Decimal(str(raw_validation["total_billed_cost_usd"])) >= Decimal(str(config["max_cost_usd"])):
        raw_validation = {
            **raw_validation,
            "ok": False,
            "errors": [*raw_validation["errors"], "resume cost is at or above configured ceiling"],
        }
    trace_validation = validate_trace_corpus(
        logdir,
        config["expected_trace_cells"],
        expected_provenance={
            "benchmark_version": config["benchmark_version"],
            "agentdojo_package_version": config["agentdojo_version"],
        },
        require_complete=False,
    )
    raw_cells = {
        tuple(cell[field] for field in CELL_FIELDS)
        for cell in raw_validation["successful_trace_cells"]
        if isinstance(cell, dict) and all(isinstance(cell.get(field), str) for field in CELL_FIELDS)
    }
    trace_cells = {
        tuple(cell[field] for field in CELL_FIELDS)
        for cell in trace_validation["observed_cells"]
    }
    cell_sets_match = raw_cells == trace_cells
    return {
        "ok": raw_validation["ok"] and trace_validation["ok"] and cell_sets_match,
        "state": "complete" if cell_sets_match and len(raw_cells) == len(config["expected_trace_cells"]) else "partial",
        "raw_responses": raw_validation,
        "trace_corpus": trace_validation,
        "errors": [] if cell_sets_match else ["raw and trace cells do not match"],
    }


def summarize_raw_responses(path: Path) -> dict[str, Any]:
    """Summarize recorded OpenRouter responses and reject ambiguous provenance."""
    errors: list[str] = []
    records: list[dict[str, Any]] = []
    if not path.exists():
        return {
            "ok": False,
            "attempt_count": 0,
            "response_count": 0,
            "successful_response_count": 0,
            "error_attempt_count": 0,
            "unreported_error_attempt_count": 0,
            "providers": [],
            "response_models": [],
            "requested_models": [],
            "trace_cells": [],
            "successful_trace_cells": [],
            "total_tokens": 0,
            "total_billed_cost_usd": None,
            "cost_complete": False,
            "records": [],
            "errors": ["raw response log is missing"],
        }

    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"invalid raw response JSON at line {line_number}")
            continue
        if not isinstance(record, dict):
            errors.append(f"raw response at line {line_number} is not an object")
            continue
        records.append(record)

    response_records = [record for record in records if record.get("attempt_status", "response") == "response"]
    error_records = [record for record in records if record.get("attempt_status") == "error"]
    providers = sorted({record.get("provider") for record in response_records if record.get("provider")})
    requested_provider_tags = sorted(
        {record.get("requested_provider_tag") for record in records if record.get("requested_provider_tag")}
    )
    response_models = sorted(
        {record.get("response_model") for record in response_records if record.get("response_model")}
    )
    requested_models = sorted({record.get("requested_model") for record in records if record.get("requested_model")})
    trace_cells = [record.get("trace_cell") for record in records]
    successful_trace_cells = [record.get("trace_cell") for record in response_records]
    if not records:
        errors.append("raw response log is empty")
    if any(record.get("attempt_status", "response") not in {"response", "error"} for record in records):
        errors.append("invalid attempt status")
    if any(not record.get("provider") for record in response_records):
        errors.append("raw response missing provider")
    if any(not record.get("response_model") for record in response_records):
        errors.append("raw response missing response model")
    if any(not record.get("requested_model") for record in records):
        errors.append("raw response missing requested model")
    if len(providers) > 1:
        errors.append("mixed providers in raw responses")
    if len(response_models) > 1:
        errors.append("mixed response models in raw responses")
    if len(requested_models) > 1:
        errors.append("mixed requested models in raw responses")

    total_tokens = 0
    costs: list[Decimal] = []
    invalid_cost = False
    for record in records:
        usage = record.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            total_tokens += usage["total_tokens"]
        billing_status = record.get("billing_status", "reported")
        cost = _as_decimal(record.get("billed_cost_usd"))
        if (
            billing_status == "not_reported"
            and record.get("billed_cost_usd") is None
            and _insured_zero_completion(record)
        ):
            continue
        if billing_status != "reported" or cost is None:
            invalid_cost = True
        else:
            costs.append(cost)
    if invalid_cost:
        errors.append("invalid billed cost")
    cost_complete = not invalid_cost and bool(records)
    return {
        "ok": not errors,
        "attempt_count": len(records),
        "response_count": len(response_records),
        "successful_response_count": len(response_records),
        "error_attempt_count": len(error_records),
        "unreported_error_attempt_count": sum(
            record.get("billing_status") == "not_reported" for record in error_records
        ),
        "providers": providers,
        "requested_provider_tags": requested_provider_tags,
        "response_models": response_models,
        "requested_models": requested_models,
        "trace_cells": trace_cells,
        "successful_trace_cells": successful_trace_cells,
        "total_tokens": total_tokens,
        "total_billed_cost_usd": float(sum(costs)) if cost_complete else None,
        "cost_complete": cost_complete,
        "records": records,
        "errors": errors,
    }


def validate_raw_response_summary(
    summary: dict[str, Any],
    *,
    model_id: str,
    provider: str,
    response_model: str,
    endpoint_revision: str,
    max_cost_usd: float,
    expected_cells: Iterable[dict[str, str]] | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Reject raw-response provenance or cost evidence that cannot prove this run's identity."""
    errors = list(summary["errors"])
    if summary["requested_models"] != [model_id]:
        errors.append("requested model does not match capture configuration")
    if summary["requested_provider_tags"] != [provider]:
        errors.append("requested provider tag does not match capture configuration")
    if summary["response_models"] != [response_model]:
        errors.append("response model does not match capture configuration")
    if not isinstance(endpoint_revision, str) or not endpoint_revision.strip():
        errors.append("endpoint revision is missing from capture configuration")
    if not summary["cost_complete"]:
        errors.append("raw response billed cost is incomplete")
    elif summary["total_billed_cost_usd"] > max_cost_usd:
        errors.append("raw response billed cost exceeds configured ceiling")
    if expected_cells is not None:
        expected = {tuple(cell[field] for field in CELL_FIELDS) for cell in expected_cells}
        observed: set[tuple[str, str, str, str]] = set()
        seen_attempt_ids: set[int] = set()
        seen_response_ids: set[str] = set()
        prior_attempt_id = 0
        for line_number, record in enumerate(summary["records"], start=1):
            attempt_status = record.get("attempt_status", "response")
            billing_status = record.get("billing_status", "reported")
            attempt_id = record.get("attempt_id")
            if not isinstance(attempt_id, int) or isinstance(attempt_id, bool) or attempt_id <= 0:
                errors.append(f"invalid raw response row {line_number}: attempt_id")
            else:
                if attempt_id in seen_attempt_ids:
                    errors.append("duplicate raw response attempt_id")
                if attempt_id <= prior_attempt_id:
                    errors.append("raw response attempt_id is not monotone")
                seen_attempt_ids.add(attempt_id)
                prior_attempt_id = attempt_id
            if record.get("requested_model") != model_id:
                errors.append(f"invalid raw response row {line_number}: requested_model")
            if record.get("requested_provider_tag") != provider:
                errors.append(f"invalid raw response row {line_number}: requested_provider_tag")
            if billing_status == "not_reported":
                if record.get("billed_cost_usd") is not None:
                    errors.append(f"invalid raw response row {line_number}: billed_cost_usd")
                if not _insured_zero_completion(record):
                    errors.append(f"invalid raw response row {line_number}: billing_status")
            elif billing_status == "reported":
                try:
                    billed_cost_decimal(record.get("billed_cost_usd"))
                except ValueError:
                    errors.append(f"invalid raw response row {line_number}: billed_cost_usd")
            elif billing_status == "unknown" and record.get("billed_cost_usd") is None:
                pass
            else:
                errors.append(f"invalid raw response row {line_number}: billing_status")
            if attempt_status == "response":
                if billing_status != "reported":
                    errors.append(f"invalid raw response row {line_number}: billing_status")
                response_id = record.get("response_id")
                if not isinstance(response_id, str) or not response_id.strip():
                    errors.append(f"invalid raw response row {line_number}: response_id")
                elif response_id in seen_response_ids:
                    errors.append("duplicate raw response response_id")
                else:
                    seen_response_ids.add(response_id)
                if record.get("response_model") != response_model:
                    errors.append(f"invalid raw response row {line_number}: response_model")
                if not isinstance(record.get("provider"), str) or not record["provider"].strip():
                    errors.append(f"invalid raw response row {line_number}: provider")
                if "finish_reason" not in record or (
                    record["finish_reason"] is not None
                    and (not isinstance(record["finish_reason"], str) or not record["finish_reason"].strip())
                ):
                    errors.append(f"invalid raw response row {line_number}: finish_reason")
            elif attempt_status == "error":
                error = record.get("error")
                if not isinstance(error, dict) or not isinstance(error.get("type"), str) or not error["type"].strip():
                    errors.append(f"invalid raw response row {line_number}: error")
                elif "body" not in error:
                    errors.append(f"invalid raw response row {line_number}: error")
                status_code = error.get("status_code") if isinstance(error, dict) else None
                if status_code is not None and (
                    not isinstance(status_code, int) or isinstance(status_code, bool)
                ):
                    errors.append(f"invalid raw response row {line_number}: error")
                error_response_model = record.get("response_model")
                if error_response_model is not None and error_response_model != response_model:
                    errors.append(f"invalid raw response row {line_number}: response_model")
                error_provider = record.get("provider")
                if error_provider is not None and (
                    not isinstance(error_provider, str)
                    or not error_provider.strip()
                    or error_provider not in summary["providers"]
                ):
                    errors.append(f"invalid raw response row {line_number}: provider")
                if record.get("raw_response") is not None:
                    errors.append(f"invalid raw response row {line_number}: raw_response")
                response_metadata = record.get("response_metadata")
                if response_metadata is not None and (
                    not isinstance(response_metadata, dict)
                    or not set(response_metadata).issubset(_ERROR_RESPONSE_METADATA_KEYS)
                ):
                    errors.append(f"invalid raw response row {line_number}: response_metadata")
                elif isinstance(response_metadata, dict):
                    if "route_conflicts" in response_metadata:
                        errors.append(f"invalid raw response row {line_number}: route identity conflict")
                    if response_metadata.get("model") != error_response_model:
                        errors.append(f"invalid raw response row {line_number}: response_model")
                    if response_metadata.get("provider") != error_provider:
                        errors.append(f"invalid raw response row {line_number}: provider")
            else:
                errors.append(f"invalid raw response row {line_number}: attempt_status")
            cell = record.get("trace_cell")
            if not isinstance(cell, dict) or any(not isinstance(cell.get(field), str) for field in CELL_FIELDS):
                errors.append(f"invalid raw response row {line_number}: trace_cell")
                continue
            cell_tuple = tuple(cell[field] for field in CELL_FIELDS)
            if cell_tuple not in expected:
                errors.append(f"invalid raw response row {line_number}: trace_cell")
                continue
            if attempt_status == "response":
                observed.add(cell_tuple)
        if not observed <= expected:
            errors.append("raw response has unexpected trace cell")
        if require_complete and observed != expected:
            errors.append("raw responses do not cover every expected trace cell")
    return {
        **{key: value for key, value in summary.items() if key != "records"},
        "ok": not errors,
        "endpoint_revision": endpoint_revision,
        "errors": errors,
    }


def _normalized_cell(trace: dict[str, Any]) -> tuple[str, str, str, str] | None:
    values = []
    for field in CELL_FIELDS:
        value = trace.get(field)
        if field in {"attack_type", "injection_task_id"} and value is None:
            value = "none"
        if not isinstance(value, str) or not value:
            return None
        values.append(value)
    return tuple(values)  # type: ignore[return-value]


def _cell_dict(cell: tuple[str, str, str, str]) -> dict[str, str]:
    return dict(zip(CELL_FIELDS, cell, strict=True))


def _trace_errors(trace: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if trace.get("error"):
        errors.append("trace error")
    messages = trace.get("messages")
    if not isinstance(messages, list) or not messages:
        errors.append("empty messages")
    else:
        for message in messages:
            if not isinstance(message, dict):
                errors.append("invalid message")
                break
            if message.get("role") == "tool" and "content" not in message and "error" not in message:
                errors.append("malformed tool result")
                break
    if not isinstance(trace.get("utility"), bool) or not isinstance(trace.get("security"), bool):
        errors.append("missing native verdict")
    return errors


def validate_trace_corpus(
    logdir: Path,
    expected_cells: Iterable[dict[str, str]],
    expected_provenance: dict[str, Any] | None = None,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Validate exact, unique, complete AgentDojo traces for the supplied frozen cells."""
    expected = [
        tuple(cell[field] for field in CELL_FIELDS)
        for cell in expected_cells
    ]
    expected_set = set(expected)
    errors: list[str] = []
    if len(expected) != len(expected_set):
        errors.append("expected cells are not unique")

    observed: dict[tuple[str, str, str, str], list[Path]] = {}
    invalid_traces: list[dict[str, Any]] = []
    files = sorted(logdir.rglob("*.json")) if logdir.exists() else []
    for path in files:
        try:
            trace = json.loads(path.read_text())
        except json.JSONDecodeError:
            invalid_traces.append({"path": str(path), "errors": ["invalid JSON"]})
            continue
        if not isinstance(trace, dict):
            invalid_traces.append({"path": str(path), "errors": ["trace is not an object"]})
            continue
        cell = _normalized_cell(trace)
        if cell is None:
            invalid_traces.append({"path": str(path), "errors": ["missing trace cell identity"]})
            continue
        trace_errors = _trace_errors(trace)
        if expected_provenance:
            for field, expected_value in expected_provenance.items():
                if trace.get(field) != expected_value:
                    trace_errors.append(f"trace provenance mismatch: {field}")
        if trace_errors:
            invalid_traces.append({"path": str(path), "cell": _cell_dict(cell), "errors": trace_errors})
            continue
        observed.setdefault(cell, []).append(path)

    duplicate_cells = [
        _cell_dict(cell)
        for cell, paths in sorted(observed.items())
        if len(paths) > 1
    ]
    observed_set = set(observed)
    missing_cells = [_cell_dict(cell) for cell in sorted(expected_set - observed_set)]
    unexpected_cells = [_cell_dict(cell) for cell in sorted(observed_set - expected_set)]
    if invalid_traces:
        errors.append("invalid traces present")
    if duplicate_cells:
        errors.append("duplicate trace cells present")
    if require_complete and missing_cells:
        errors.append("expected trace cells are missing")
    if unexpected_cells:
        errors.append("unexpected trace cells present")
    return {
        "ok": not errors,
        "trace_files": len(files),
        "expected_count": len(expected),
        "valid_count": sum(len(paths) for paths in observed.values()),
        "invalid_traces": invalid_traces,
        "duplicate_cells": duplicate_cells,
        "missing_cells": missing_cells,
        "unexpected_cells": unexpected_cells,
        "observed_cells": [_cell_dict(cell) for cell in sorted(observed_set)],
        "errors": errors,
    }
