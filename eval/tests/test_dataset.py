import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from eval import dataset
from eval.dataset import load_cohort


_OFFICIAL_PAPER_MAIN_V1_ORIGINAL_ANCHOR = dataset._PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256


@pytest.fixture(autouse=True)
def _restore_paper_main_v1_anchor(monkeypatch):
    monkeypatch.setattr(
        dataset, "_PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256", _OFFICIAL_PAPER_MAIN_V1_ORIGINAL_ANCHOR
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def _write_hashed_json(path: Path, value: object, *, sidecar: bool = False) -> str:
    _write_json(path, value)
    digest = _sha256(path.read_bytes())
    if sidecar:
        path.with_name(f"{path.name}.sha256").write_text(f"{digest}  {path.name}\n")
    return digest


def _write_leaf(path: Path, files: dict[str, bytes] | bytes) -> dict[str, object]:
    if isinstance(files, bytes):
        files = {"evidence.json": files}
    path.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for relative, content in files.items():
        evidence_path = path / relative
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(content)
        hashes[relative] = _sha256(content)
    sums = b"".join(
        f"{hashes[relative]}  {relative}\n".encode()
        for relative in sorted(hashes, key=lambda value: value.encode())
    )
    (path / "SHA256SUMS").write_bytes(sums)
    (path / "ROOT_SHA256").write_text(f"{_sha256(sums)}  SHA256SUMS\n")
    return {
        "evidence_bytes": sum(len(content) for content in files.values()),
        "file_count": len(files),
        "root_sha256": _sha256(sums),
        "hashes": hashes,
    }


def _reseal_leaf(path: Path) -> dict[str, object]:
    files = {
        file_path.relative_to(path).as_posix(): file_path.read_bytes()
        for file_path in path.rglob("*")
        if file_path.is_file() and file_path.name not in {"ROOT_SHA256", "SHA256SUMS"}
    }
    hashes = {relative: _sha256(content) for relative, content in files.items()}
    sums = b"".join(
        f"{hashes[relative]}  {relative}\n".encode()
        for relative in sorted(hashes, key=lambda value: value.encode())
    )
    (path / "SHA256SUMS").write_bytes(sums)
    (path / "ROOT_SHA256").write_text(f"{_sha256(sums)}  SHA256SUMS\n")
    return {
        "evidence_bytes": sum(len(content) for content in files.values()),
        "file_count": len(files),
        "root_sha256": _sha256(sums),
        "hashes": hashes,
    }


def _write_agentdojo_leaf(
    path: Path,
    *,
    suite: str,
    user_task_id: str = "user_task_0",
    attack_type: str | None = "important_instructions",
    injection_task_id: str | None = "injection_task_0",
    protocol_error: bool = False,
    raw_response_id: str = "response-1",
    run_id: str = "run-glm47-banking",
    model_id: str = "glm47-model",
    response_model: str = "glm47-response",
    provider_tag: str = "glm47-provider",
    observed_provider: str = "glm47-observed-provider",
    endpoint_revision: str = "glm47-revision",
    benchmark_version: str = "v-test",
    agentdojo_version: str = "0.1-test",
    attack: str = "important_instructions",
    temperature: float = 0.0,
    rollout: int = 0,
    workers: int = 1,
    prototype_capture_commit: str = "abcdef0123456789abcdef0123456789abcdef01",
) -> dict[str, object]:
    attack_path = attack_type or "none"
    injection_path = injection_task_id or "none"
    trace_relative = (
        f"agentdojo/openai-compatible/{suite}/{user_task_id}/{attack_path}/{injection_path}.json"
    )
    identity = (suite, user_task_id, attack_type or "none", injection_task_id or "none")
    cell = {
        "suite_name": identity[0],
        "user_task_id": identity[1],
        "attack_type": identity[2],
        "injection_task_id": identity[3],
    }
    trace: dict[str, object] = {
        "suite_name": suite,
        "user_task_id": user_task_id,
        "attack_type": attack_type,
        "injection_task_id": injection_task_id,
        "error": None,
    }
    if not protocol_error:
        trace.update({"utility": True, "security": True})
    capture_lock = {
        "run_id": run_id,
        "model_id": model_id,
        "suites": [suite],
        "response_model": response_model,
        "provider": provider_tag,
        "endpoint_revision": endpoint_revision,
        "benchmark_version": benchmark_version,
        "agentdojo_version": agentdojo_version,
        "attack": attack,
        "temperature": temperature,
        "workers": workers,
        "expected_trace_cells": [cell],
    }
    manifest = {
        **capture_lock,
        "rollout": rollout,
        "eval_git": prototype_capture_commit[:7],
        "expected_trace_cells": [cell],
        "raw_response_summary": {"response_count": 1},
    }
    files = {
        "evidence.json": b'{"verdict":"pass"}\n',
        "capture-lock.json": _json_bytes(capture_lock),
        "manifest.json": _json_bytes(manifest),
        "results.json": b'{"banking":{}}\n',
        "raw_responses.jsonl": _json_bytes(
            {
                "response_id": raw_response_id,
                "billing_status": "reported",
                "attempt_status": "response",
                "billed_cost_usd": "1.25",
                "trace_cell": cell,
                "requested_model": model_id,
                "requested_provider_tag": provider_tag,
                "response_model": response_model,
                "provider": observed_provider,
            }
        ),
        trace_relative: _json_bytes(trace),
    }
    leaf = _write_leaf(path, files)
    leaf.update(
        {
            "trace_relative": trace_relative,
            "raw_response_id": raw_response_id,
            "identity": identity,
            "run_id": run_id,
            "model_id": model_id,
            "response_model": response_model,
            "provider_tag": provider_tag,
            "observed_provider": observed_provider,
            "endpoint_revision": endpoint_revision,
            "benchmark_version": benchmark_version,
            "agentdojo_version": agentdojo_version,
            "attack": attack,
            "temperature": temperature,
            "rollout": rollout,
            "workers": workers,
            "prototype_capture_commit": prototype_capture_commit,
        }
    )
    return leaf


def _write_tau_leaf(path: Path, *, domain: str = "airline") -> dict[str, object]:
    capture_config = {
        "agent": "glm47-model",
        "domain": domain,
        "response_model": "glm47-model",
        "provider": "glm47-provider",
        "endpoint_revision": "glm47-revision",
        "prototype_commit": "abcdef0123456789abcdef0123456789abcdef01",
        "tau2_commit": "1234567890abcdef1234567890abcdef12345678",
        "routes": {
            "openrouter/glm47-model": {
                "response_model": "glm47-model",
                "provider": "glm47-provider",
                "endpoint_revision": "glm47-revision",
            }
        },
        "task_ids": ["0"],
        "num_trials": 1,
    }
    generation_id = "generation-1"
    manifest = {
        "status": "PASS",
        "capture_config": capture_config,
        "prototype_commit": capture_config["prototype_commit"],
        "tau2_commit": capture_config["tau2_commit"],
        "validation": {"ok": True},
        "tau_results": {
            "ok": True,
            "missing_task_trials": [],
            "unexpected_task_trials": [],
            "duplicate_task_trials": [],
            "errors": [],
        },
        "llm_calls": {"generation_count": 1, "cost_usd": "0.25", "errors": []},
        "generation_ids": [generation_id],
    }
    files = {
        "evidence.json": b'{"simulation":"pass"}\n',
        "capture-lock.json": _json_bytes(capture_config),
        "manifest.json": _json_bytes(manifest),
        "results.json": _json_bytes(
            {
                "tasks": [{"id": "0"}],
                "simulations": [{"task_id": "0", "trial": 0}],
            }
        ),
        "artifacts/task_0/sim_0/llm_debug/generation.json": _json_bytes(
            {
                "response": {
                    "raw_response": {
                        "id": generation_id,
                        "usage": {"cost": "0.25"},
                    }
                }
            }
        ),
    }
    leaf = _write_leaf(path, files)
    leaf.update({"generation_id": generation_id, "capture_config": capture_config})
    return leaf


def _write_outer_and_lock(lock_path: Path, outer_path: Path, outer: dict[str, object]) -> None:
    _write_json(outer_path, outer)
    outer_sidecar = outer_path.with_name(f"{outer_path.name}.sha256")
    if outer_sidecar.exists():
        outer_sidecar.write_text(f"{_sha256(outer_path.read_bytes())}  {outer_path.name}\n")
    lock = json.loads(lock_path.read_text())
    lock["current_outer_manifest"]["sha256"] = _sha256(outer_path.read_bytes())
    lock["accepted_capture_summary"] = outer["accepted_capture_summary"]
    _write_json(lock_path, lock)


def _write_original_outer_and_lock(
    lock_path: Path, original_outer_path: Path, outer: dict[str, object]
) -> None:
    digest = _write_hashed_json(original_outer_path, outer, sidecar=True)
    lock = json.loads(lock_path.read_text())
    lock["original_outer_manifest"]["sha256"] = digest
    _write_json(lock_path, lock)
    dataset._PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256 = digest


def _legacy_logical(logical: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(logical)
    for shard_kind in ("accepted_shards", "rejected_shards"):
        for index, shard in enumerate(result.get(shard_kind, [])):
            legacy_root = f"legacy/{shard_kind}/{index}"
            shard["physical_path"] = legacy_root
            shard["root_sha256_path"] = f"{legacy_root}/ROOT_SHA256"
            shard["sha256sums_path"] = f"{legacy_root}/SHA256SUMS"
    errors = result.get("model_protocol_errors")
    if isinstance(errors, list):
        for index, error in enumerate(errors):
            error["partial_trace_path"] = f"legacy/protocol/{index}.json"
            error["raw_response_path"] = f"legacy/protocol/{index}.jsonl"
    return result


def _sync_original(lock_path: Path, paths: dict[str, Any]) -> None:
    """Give the synthetic original capture the same facts under legacy paths."""
    cohort_dir = lock_path.parent
    current_outer = json.loads(paths["outer"].read_text())
    original_dir: Path = paths["original_dir"]
    original_records = []
    for record in current_outer["logical_manifests"]:
        logical_path = cohort_dir / record["path"]
        original_name = Path(record["path"]).name
        original_logical_path = original_dir / original_name
        logical_hash = _write_hashed_json(
            original_logical_path,
            _legacy_logical(json.loads(logical_path.read_text())),
            sidecar=True,
        )
        original_record = copy.deepcopy(record)
        original_record["path"] = original_name
        original_record["sha256"] = logical_hash
        original_record["sha256_sidecar_path"] = f"{original_name}.sha256"
        original_records.append(original_record)
    original_outer = copy.deepcopy(current_outer)
    original_outer["capture_contract"]["path"] = "capture_contract.json"
    for catalog in original_outer["endpoint_catalogs"]:
        catalog["path"] = f"endpoint_catalog/{Path(catalog['path']).name}"
    original_outer["logical_manifests"] = original_records
    original_outer_path: Path = paths["original_outer"]
    original_hash = _write_hashed_json(original_outer_path, original_outer, sidecar=True)
    lock = json.loads(lock_path.read_text())
    lock["original_outer_manifest"]["sha256"] = original_hash
    _write_json(lock_path, lock)
    dataset._PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256 = original_hash


def _reseal_current_logical(
    lock_path: Path,
    paths: dict[str, Any],
    logical_path: Path,
    record_index: int,
    logical: dict[str, Any],
    *,
    sync_original: bool = True,
) -> dict[str, Any]:
    logical_hash = _write_hashed_json(logical_path, logical, sidecar=True)
    outer = json.loads(paths["outer"].read_text())
    outer["logical_manifests"][record_index]["sha256"] = logical_hash
    _write_outer_and_lock(lock_path, paths["outer"], outer)
    if sync_original:
        _sync_original(lock_path, paths)
    return outer


def _add_tau_shard(lock_path: Path, paths: dict[str, Any]) -> None:
    cohort_dir = lock_path.parent
    tau_path = cohort_dir / "corpus" / "accepted" / "tau" / "glm47" / "airline"
    tau = _write_tau_leaf(tau_path)
    tau_logical_path = cohort_dir / "seal" / "current" / "tau_glm47_logical_manifest.json"
    tau_logical = {
        "benchmark": "tau",
        "model_id": "glm47",
        "accepted_shards": [
            {
                "physical_path": "corpus/accepted/tau/glm47/airline",
                "root_sha256": tau["root_sha256"],
                "root_sha256_path": "corpus/accepted/tau/glm47/airline/ROOT_SHA256",
                "sha256sums_path": "corpus/accepted/tau/glm47/airline/SHA256SUMS",
                "domain": "airline",
                "simulation_count": 1,
                "file_count": tau["file_count"],
                "evidence_bytes": tau["evidence_bytes"],
                "cost_usd": 0.25,
                "manifest_sha256": tau["hashes"]["manifest.json"],
                "results_sha256": tau["hashes"]["results.json"],
            }
        ],
        "rejected_shards": [],
    }
    outer = json.loads(paths["outer"].read_text())
    tau_logical["capture_contract_sha256"] = outer["capture_contract"]["sha256"]
    tau_logical["model"] = {
        "catalog_sha256": outer["endpoint_catalogs"][0]["sha256"],
        "model": tau["capture_config"]["agent"],
        "response_model": tau["capture_config"]["response_model"],
        "provider_tag": tau["capture_config"]["provider"],
        "endpoint_revision": tau["capture_config"]["endpoint_revision"],
    }
    tau_logical["prototype_capture_commit"] = tau["capture_config"]["prototype_commit"]
    tau_logical["tau2_capture_commit"] = tau["capture_config"]["tau2_commit"]
    tau_logical_hash = _write_hashed_json(tau_logical_path, tau_logical, sidecar=True)
    outer["logical_manifests"].append(
        {
            "benchmark": "tau",
            "model_id": "glm47",
            "path": "seal/current/tau_glm47_logical_manifest.json",
            "sha256": tau_logical_hash,
            "sha256_sidecar_path": "seal/current/tau_glm47_logical_manifest.json.sha256",
            "accepted_count": 1,
            "accepted_count_unit": "simulations",
            "billed_cost_usd": 0.25,
            "rejected_root_count": 0,
        }
    )
    summary = outer["accepted_capture_summary"]
    summary["accepted_physical_root_count"] = 2
    summary["accepted_billed_cost_usd"] = 1.5
    summary["tau"] = {
        "accepted_simulation_count": 1,
        "billed_cost_usd": 0.25,
        "models": {"glm47": {"accepted_simulation_count": 1, "billed_cost_usd": 0.25}},
    }
    _write_outer_and_lock(lock_path, paths["outer"], outer)
    _sync_original(lock_path, paths)


def _add_agentdojo_model(lock_path: Path, paths: dict[str, Any]) -> None:
    cohort_dir = lock_path.parent
    model_id = "glm48"
    suite = "banking"
    accepted_path = cohort_dir / "corpus" / "accepted" / "agentdojo" / model_id / suite
    accepted = _write_agentdojo_leaf(
        accepted_path,
        suite=suite,
        user_task_id="user_task_1",
        raw_response_id="response-2",
        run_id="run-glm48-banking",
        model_id="glm48-model",
        response_model="glm48-response",
        provider_tag="glm48-provider",
        observed_provider="glm48-observed-provider",
        endpoint_revision="glm48-revision",
    )
    current_dir = cohort_dir / "seal" / "current"
    original_dir: Path = paths["original_dir"]
    catalog_path = current_dir / "endpoint_catalog" / "glm48.json"
    catalog_hash = _write_hashed_json(catalog_path, {"model": model_id})
    _write_hashed_json(original_dir / "endpoint_catalog" / "glm48.json", {"model": model_id})
    outer = json.loads(paths["outer"].read_text())
    logical_path = current_dir / "agentdojo_glm48_logical_manifest.json"
    membership_line = "\t".join(accepted["identity"]) + "\n"
    logical = {
        "benchmark": "agentdojo",
        "model_id": model_id,
        "capture_contract_sha256": outer["capture_contract"]["sha256"],
        "model": {
            "catalog_sha256": catalog_hash,
            "model": accepted["model_id"],
            "response_model": accepted["response_model"],
            "provider_tag": accepted["provider_tag"],
            "observed_provider": accepted["observed_provider"],
            "endpoint_revision": accepted["endpoint_revision"],
        },
        "run_config": {
            "benchmark_version": accepted["benchmark_version"],
            "agentdojo_package_version": accepted["agentdojo_version"],
            "attack": accepted["attack"],
            "temperature": accepted["temperature"],
            "rollout": accepted["rollout"],
            "workers": accepted["workers"],
        },
        "prototype_capture_commit": accepted["prototype_capture_commit"],
        "accepted_shards": [
            {
                "physical_path": f"corpus/accepted/agentdojo/{model_id}/{suite}",
                "root_sha256": accepted["root_sha256"],
                "root_sha256_path": f"corpus/accepted/agentdojo/{model_id}/{suite}/ROOT_SHA256",
                "sha256sums_path": f"corpus/accepted/agentdojo/{model_id}/{suite}/SHA256SUMS",
                "suite": suite,
                "run_id": accepted["run_id"],
                "expected_trace_count": 1,
                "graded_trace_count": 1,
                "model_protocol_error_count": 0,
                "raw_response_count": 1,
                "trace_file_count": 1,
                "file_count": accepted["file_count"],
                "evidence_bytes": accepted["evidence_bytes"],
                "cost_usd": 1.25,
                "manifest_sha256": accepted["hashes"]["manifest.json"],
                "results_sha256": accepted["hashes"]["results.json"],
                "raw_responses_sha256": accepted["hashes"]["raw_responses.jsonl"],
            }
        ],
        "rejected_shards": [],
        "capture_summary": {
            "accepted_shard_count": 1,
            "expected_trace_count": 1,
            "graded_trace_count": 1,
            "model_protocol_error_count": 0,
            "physical_trace_file_count": 1,
            "raw_response_count": 1,
            "total_billed_cost_usd": 1.25,
        },
        "logical_membership": {
            "count": 1,
            "sha256": _sha256(membership_line.encode()),
            "suite_counts": {suite: 1},
        },
    }
    logical_hash = _write_hashed_json(logical_path, logical, sidecar=True)
    outer["endpoint_catalogs"].append(
        {
            "model_id": model_id,
            "path": "seal/current/endpoint_catalog/glm48.json",
            "sha256": catalog_hash,
        }
    )
    outer["logical_manifests"].append(
        {
            "benchmark": "agentdojo",
            "model_id": model_id,
            "path": "seal/current/agentdojo_glm48_logical_manifest.json",
            "sha256": logical_hash,
            "sha256_sidecar_path": "seal/current/agentdojo_glm48_logical_manifest.json.sha256",
            "accepted_count": 1,
            "accepted_count_unit": "trace_cells",
            "billed_cost_usd": 1.25,
            "graded_count": 1,
            "model_protocol_error_count": 0,
            "rejected_root_count": 0,
        }
    )
    summary = outer["accepted_capture_summary"]
    summary["accepted_physical_root_count"] = 2
    summary["accepted_billed_cost_usd"] = 2.5
    agentdojo = summary["agentdojo"]
    agentdojo["accepted_trace_cell_count"] = 2
    agentdojo["billed_cost_usd"] = 2.5
    agentdojo["graded_trace_count"] = 2
    agentdojo["model_protocol_error_count"] = 0
    agentdojo["raw_response_count"] = 2
    agentdojo["models"][model_id] = {
        "accepted_trace_cell_count": 1,
        "billed_cost_usd": 1.25,
        "graded_trace_count": 1,
        "model_protocol_error_count": 0,
        "raw_response_count": 1,
    }
    _write_outer_and_lock(lock_path, paths["outer"], outer)
    _sync_original(lock_path, paths)


def _delete_summary_field(summary: dict[str, object], dotted_path: str) -> None:
    current = summary
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        current = current[part]
    del current[parts[-1]]


def _write_synthetic_cohort(
    tmp_path: Path, *, protocol_error: bool = False
) -> tuple[Path, dict[str, Any]]:
    cohort_dir = tmp_path / "paper_main_v1"
    accepted_path = cohort_dir / "corpus" / "accepted" / "agentdojo" / "glm47" / "banking"
    rejected_path = cohort_dir / "corpus" / "rejected" / "agentdojo" / "glm47" / "banking"
    accepted = _write_agentdojo_leaf(
        accepted_path, suite="banking", protocol_error=protocol_error
    )
    rejected = _write_leaf(
        rejected_path,
        {
            "evidence.json": b'{"verdict":"rejected"}\n',
            "manifest.json": b'{"status":"FAILED"}\n',
            "results.json": b'{"banking":{}}\n',
            "raw_responses.jsonl": b"",
        },
    )
    current_dir = cohort_dir / "seal" / "current"
    original_dir = cohort_dir / "seal" / "original-split-capture"
    contract_path = current_dir / "capture_contract.json"
    catalog_path = current_dir / "endpoint_catalog" / "glm.json"
    contract_hash = _write_hashed_json(contract_path, {"contract": "synthetic"})
    catalog_hash = _write_hashed_json(catalog_path, {"model": "glm47"})
    logical_path = current_dir / "agentdojo_glm47_logical_manifest.json"
    outer_path = current_dir / "capture_manifest.json"
    lock_path = cohort_dir / "cohort.lock.json"

    membership_line = "\t".join(accepted["identity"]) + "\n"
    graded_count = 0 if protocol_error else 1
    protocol_count = 1 if protocol_error else 0
    logical = {
            "benchmark": "agentdojo",
            "model_id": "glm47",
            "capture_contract_sha256": contract_hash,
            "model": {
                "catalog_sha256": catalog_hash,
                "model": accepted["model_id"],
                "response_model": accepted["response_model"],
                "provider_tag": accepted["provider_tag"],
                "observed_provider": accepted["observed_provider"],
                "endpoint_revision": accepted["endpoint_revision"],
            },
            "run_config": {
                "benchmark_version": accepted["benchmark_version"],
                "agentdojo_package_version": accepted["agentdojo_version"],
                "attack": accepted["attack"],
                "temperature": accepted["temperature"],
                "rollout": accepted["rollout"],
                "workers": accepted["workers"],
            },
            "prototype_capture_commit": accepted["prototype_capture_commit"],
            "accepted_shards": [
                {
                    "physical_path": "corpus/accepted/agentdojo/glm47/banking",
                    "root_sha256": accepted["root_sha256"],
                    "root_sha256_path": "corpus/accepted/agentdojo/glm47/banking/ROOT_SHA256",
                    "sha256sums_path": "corpus/accepted/agentdojo/glm47/banking/SHA256SUMS",
                    "suite": "banking",
                    "run_id": accepted["run_id"],
                    "expected_trace_count": 1,
                    "graded_trace_count": graded_count,
                    "model_protocol_error_count": protocol_count,
                    "raw_response_count": 1,
                    "trace_file_count": 1,
                    "file_count": accepted["file_count"],
                    "evidence_bytes": accepted["evidence_bytes"],
                    "cost_usd": 1.25,
                    "manifest_sha256": accepted["hashes"]["manifest.json"],
                    "results_sha256": accepted["hashes"]["results.json"],
                    "raw_responses_sha256": accepted["hashes"]["raw_responses.jsonl"],
                }
            ],
            "rejected_shards": [
                {
                    "physical_path": "corpus/rejected/agentdojo/glm47/banking",
                    "root_sha256": rejected["root_sha256"],
                    "root_sha256_path": "corpus/rejected/agentdojo/glm47/banking/ROOT_SHA256",
                    "sha256sums_path": "corpus/rejected/agentdojo/glm47/banking/SHA256SUMS",
                    "suite": "banking",
                    "file_count": rejected["file_count"],
                    "evidence_bytes": rejected["evidence_bytes"],
                    "manifest_sha256": rejected["hashes"]["manifest.json"],
                    "results_sha256": rejected["hashes"]["results.json"],
                    "raw_responses_sha256": rejected["hashes"]["raw_responses.jsonl"],
                }
            ],
            "capture_summary": {
                "accepted_shard_count": 1,
                "expected_trace_count": 1,
                "graded_trace_count": graded_count,
                "model_protocol_error_count": protocol_count,
                "physical_trace_file_count": 1,
                "raw_response_count": 1,
                "total_billed_cost_usd": 1.25,
            },
            "logical_membership": {
                "count": 1,
                "sha256": _sha256(membership_line.encode()),
                "suite_counts": {"banking": 1},
            },
    }
    if protocol_error:
        logical["model_protocol_errors"] = [
            {
                "cell": {
                    "suite_name": accepted["identity"][0],
                    "user_task_id": accepted["identity"][1],
                    "attack_type": accepted["identity"][2],
                    "injection_task_id": accepted["identity"][3],
                },
                "error_type": "model_protocol_error",
                "partial_trace_path": (
                    "corpus/accepted/agentdojo/glm47/banking/"
                    + accepted["trace_relative"]
                ),
                "raw_response_path": "corpus/accepted/agentdojo/glm47/banking/raw_responses.jsonl",
                "raw_response_id": accepted["raw_response_id"],
            }
        ]
    logical_sha256 = _write_hashed_json(logical_path, logical, sidecar=True)
    outer = {
            "schema_version": 1,
            "phase": "capture_only",
            "hash_algorithm": "sha256",
            "logical_manifests": [
                {
                    "benchmark": "agentdojo",
                    "model_id": "glm47",
                    "path": "seal/current/agentdojo_glm47_logical_manifest.json",
                    "sha256": logical_sha256,
                    "sha256_sidecar_path": "seal/current/agentdojo_glm47_logical_manifest.json.sha256",
                    "accepted_count": 1,
                    "accepted_count_unit": "trace_cells",
                    "billed_cost_usd": 1.25,
                    "graded_count": graded_count,
                    "model_protocol_error_count": protocol_count,
                    "rejected_root_count": 1,
                }
            ],
            "capture_contract": {
                "path": "seal/current/capture_contract.json",
                "sha256": contract_hash,
            },
            "endpoint_catalogs": [
                {
                    "model_id": "glm47",
                    "path": "seal/current/endpoint_catalog/glm.json",
                    "sha256": catalog_hash,
                }
            ],
            "rejected_provenance": [
                {
                    "benchmark": "agentdojo",
                    "model_id": "glm47",
                    "root_sha256": rejected["root_sha256"],
                }
            ],
            "accepted_capture_summary": {
                "accepted_physical_root_count": 1,
                "rejected_physical_root_count": 1,
                "accepted_billed_cost_usd": 1.25,
                "agentdojo": {
                    "accepted_trace_cell_count": 1,
                    "billed_cost_usd": 1.25,
                    "graded_trace_count": graded_count,
                    "model_protocol_error_count": protocol_count,
                    "raw_response_count": 1,
                    "models": {
                        "glm47": {
                            "accepted_trace_cell_count": 1,
                            "billed_cost_usd": 1.25,
                            "graded_trace_count": graded_count,
                            "model_protocol_error_count": protocol_count,
                            "raw_response_count": 1,
                        }
                    },
                },
                "tau": {"accepted_simulation_count": 0, "billed_cost_usd": 0.0, "models": {}},
            },
    }
    outer_hash = _write_hashed_json(outer_path, outer, sidecar=True)
    _write_hashed_json(original_dir / "capture_contract.json", {"contract": "synthetic"})
    _write_hashed_json(original_dir / "endpoint_catalog" / "glm.json", {"model": "glm47"})
    original_outer_path = original_dir / "capture_manifest.json"
    _write_json(
        lock_path,
        {
                "cohort_id": "paper_main_v1",
                "schema_version": 1,
                "status": "active_local_source",
                "current_outer_manifest": {
                    "path": "seal/current/capture_manifest.json",
                    "sha256": outer_hash,
                    "sha256_sidecar_path": "seal/current/capture_manifest.json.sha256",
                },
                "original_outer_manifest": {
                    "path": "seal/original-split-capture/capture_manifest.json",
                    "sha256": "0" * 64,
                    "sha256_sidecar_path": "seal/original-split-capture/capture_manifest.json.sha256",
                },
                "accepted_capture_summary": outer["accepted_capture_summary"],
        },
    )
    paths: dict[str, Any] = {
        "accepted": accepted_path,
        "rejected": rejected_path,
        "logical": logical_path,
        "outer": outer_path,
        "original_outer": original_outer_path,
        "original_dir": original_dir,
        "contract": contract_path,
    }
    _sync_original(lock_path, paths)
    return lock_path, paths


def test_load_cohort_returns_only_hash_verified_accepted_shards(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)

    cohort = load_cohort(lock_path)

    assert cohort.cohort_id == "paper_main_v1"
    assert len(cohort.accepted_shards) == 1
    shard = cohort.accepted_shards[0]
    assert shard.benchmark == "agentdojo"
    assert shard.model_id == "glm47"
    assert shard.kind == "banking"
    assert shard.path == paths["accepted"].resolve()
    assert shard.accepted_count == 1


def test_load_cohort_rejects_outer_manifest_mutation(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    paths["outer"].write_text(paths["outer"].read_text() + " ")

    with pytest.raises(ValueError, match="outer manifest hash"):
        load_cohort(lock_path)


def test_load_cohort_rejects_leaf_file_mutation(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    (paths["accepted"] / "evidence.json").write_text('{"verdict":"mutated"}\n')

    with pytest.raises(ValueError, match="hash mismatch"):
        load_cohort(lock_path)


def test_load_cohort_rejects_unlisted_leaf_file(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    (paths["accepted"] / "unexpected.json").write_text("unexpected\n")

    with pytest.raises(ValueError, match="unlisted regular file"):
        load_cohort(lock_path)


def test_load_cohort_rejects_symlink(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    link = paths["accepted"] / "linked-evidence.json"
    try:
        link.symlink_to(paths["accepted"] / "evidence.json")
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(ValueError, match="symlink"):
        load_cohort(lock_path)


def test_load_cohort_rejects_duplicate_physical_path(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    logical = json.loads(paths["logical"].read_text())
    logical["accepted_shards"].append(dict(logical["accepted_shards"][0]))
    logical["capture_summary"]["accepted_shard_count"] = 2
    logical["capture_summary"]["expected_trace_count"] = 2
    logical["capture_summary"]["graded_trace_count"] = 2
    logical["capture_summary"]["physical_trace_file_count"] = 2
    logical["capture_summary"]["raw_response_count"] = 2
    logical["capture_summary"]["total_billed_cost_usd"] = 2.5
    _write_hashed_json(paths["logical"], logical, sidecar=True)
    outer = json.loads(paths["outer"].read_text())
    outer["logical_manifests"][0]["sha256"] = _sha256(paths["logical"].read_bytes())
    outer["logical_manifests"][0]["accepted_count"] = 2
    outer["logical_manifests"][0]["billed_cost_usd"] = 2.5
    outer["accepted_capture_summary"]["accepted_physical_root_count"] = 2
    outer["accepted_capture_summary"]["accepted_billed_cost_usd"] = 2.5
    outer["accepted_capture_summary"]["agentdojo"]["accepted_trace_cell_count"] = 2
    outer["accepted_capture_summary"]["agentdojo"]["billed_cost_usd"] = 2.5
    outer["accepted_capture_summary"]["agentdojo"]["graded_trace_count"] = 2
    outer["accepted_capture_summary"]["agentdojo"]["raw_response_count"] = 2
    model_summary = outer["accepted_capture_summary"]["agentdojo"]["models"]["glm47"]
    model_summary["accepted_trace_cell_count"] = 2
    model_summary["billed_cost_usd"] = 2.5
    model_summary["graded_trace_count"] = 2
    model_summary["raw_response_count"] = 2
    _write_outer_and_lock(lock_path, paths["outer"], outer)
    _sync_original(lock_path, paths)

    with pytest.raises(ValueError, match="duplicate physical path"):
        load_cohort(lock_path)


def test_load_cohort_rejects_leaf_traversal_error(tmp_path, monkeypatch):
    lock_path, _ = _write_synthetic_cohort(tmp_path)

    def walk_with_error(root, *, followlinks, onerror=None):
        if onerror is not None:
            onerror(PermissionError(13, "permission denied", str(root)))
        return iter(())

    monkeypatch.setattr(dataset.os, "walk", walk_with_error)

    with pytest.raises(ValueError, match="leaf traversal error"):
        load_cohort(lock_path)


def test_load_cohort_rejects_nested_agentdojo_model_summary_mismatch(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    outer = json.loads(paths["outer"].read_text())
    outer["accepted_capture_summary"]["agentdojo"]["models"]["glm47"]["raw_response_count"] = 3
    _write_outer_and_lock(lock_path, paths["outer"], outer)
    _sync_original(lock_path, paths)

    with pytest.raises(ValueError, match="AgentDojo model raw response count mismatch"):
        load_cohort(lock_path)


def test_load_cohort_rejects_nested_tau_model_summary_mismatch(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    _add_tau_shard(lock_path, paths)
    outer = json.loads(paths["outer"].read_text())
    outer["accepted_capture_summary"]["tau"]["models"]["glm47"]["accepted_simulation_count"] = 2
    _write_outer_and_lock(lock_path, paths["outer"], outer)
    _sync_original(lock_path, paths)

    with pytest.raises(ValueError, match="Tau model accepted count mismatch"):
        load_cohort(lock_path)


def test_load_cohort_tolerates_unknown_lock_metadata(tmp_path):
    lock_path, _ = _write_synthetic_cohort(tmp_path)
    lock = json.loads(lock_path.read_text())
    lock["future_metadata"] = {"retention_policy": "unchanged", "revision": 2}
    _write_json(lock_path, lock)

    assert load_cohort(lock_path).cohort_id == "paper_main_v1"


def test_load_cohort_parses_authenticated_manifest_bytes_without_second_read(tmp_path, monkeypatch):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    original_read_text = Path.read_text
    sealed_paths = {paths["outer"].resolve(), paths["logical"].resolve()}

    def reject_second_manifest_read(self, *args, **kwargs):
        if self.resolve() in sealed_paths:
            raise AssertionError("manifest was read again after hashing")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_second_manifest_read)

    assert load_cohort(lock_path).cohort_id == "paper_main_v1"


def test_load_cohort_parses_authenticated_sha256sums_bytes_without_second_read(tmp_path, monkeypatch):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    original_read_bytes = Path.read_bytes
    sums_path = (paths["accepted"] / "SHA256SUMS").resolve()
    reads = 0

    def mutate_after_first_sha256sums_read(self, *args, **kwargs):
        nonlocal reads
        if self.resolve() == sums_path:
            reads += 1
            if reads > 1:
                return b""
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", mutate_after_first_sha256sums_read)

    assert load_cohort(lock_path).cohort_id == "paper_main_v1"


@pytest.mark.parametrize(
    "field",
    [
        "accepted_trace_cell_count",
        "billed_cost_usd",
        "graded_trace_count",
        "model_protocol_error_count",
        "raw_response_count",
        "models",
        "models.glm47.accepted_trace_cell_count",
        "models.glm47.billed_cost_usd",
        "models.glm47.graded_trace_count",
        "models.glm47.model_protocol_error_count",
        "models.glm47.raw_response_count",
    ],
)
def test_load_cohort_rejects_missing_required_agentdojo_summary_field(tmp_path, field):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    outer = json.loads(paths["outer"].read_text())
    _delete_summary_field(outer["accepted_capture_summary"]["agentdojo"], field)
    _write_outer_and_lock(lock_path, paths["outer"], outer)

    with pytest.raises(ValueError, match="missing required summary field"):
        load_cohort(lock_path)


@pytest.mark.parametrize(
    "field",
    [
        "accepted_simulation_count",
        "billed_cost_usd",
        "models",
        "models.glm47.accepted_simulation_count",
        "models.glm47.billed_cost_usd",
    ],
)
def test_load_cohort_rejects_missing_required_tau_summary_field(tmp_path, field):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    _add_tau_shard(lock_path, paths)
    outer = json.loads(paths["outer"].read_text())
    _delete_summary_field(outer["accepted_capture_summary"]["tau"], field)
    _write_outer_and_lock(lock_path, paths["outer"], outer)

    with pytest.raises(ValueError, match="missing required summary field"):
        load_cohort(lock_path)


def test_load_cohort_rejects_required_summary_field_missing_only_from_lock(tmp_path):
    lock_path, _ = _write_synthetic_cohort(tmp_path)
    lock = json.loads(lock_path.read_text())
    del lock["accepted_capture_summary"]["agentdojo"]["models"]
    _write_json(lock_path, lock)

    with pytest.raises(ValueError, match="lock accepted summary agentdojo: missing required summary field models"):
        load_cohort(lock_path)


def test_load_cohort_rejects_missing_outer_logical_billed_cost(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    outer = json.loads(paths["outer"].read_text())
    del outer["logical_manifests"][0]["billed_cost_usd"]
    _write_outer_and_lock(lock_path, paths["outer"], outer)

    with pytest.raises(ValueError, match="outer logical billed cost must be numeric"):
        load_cohort(lock_path)


def test_load_cohort_rejects_duplicate_root_hash_across_accepted_and_rejected(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    logical = json.loads(paths["logical"].read_text())
    logical["rejected_shards"][0]["root_sha256"] = logical["accepted_shards"][0]["root_sha256"]
    _reseal_current_logical(lock_path, paths, paths["logical"], 0, logical)

    with pytest.raises(ValueError, match="duplicate root SHA-256"):
        load_cohort(lock_path)


def test_load_cohort_rejects_missing_rejected_root(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    paths["rejected"].rename(paths["rejected"].with_name("missing-rejected-root"))

    with pytest.raises(ValueError, match="physical path"):
        load_cohort(lock_path)


def test_load_cohort_derives_agentdojo_trace_totals_from_leaf_files(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    logical = json.loads(paths["logical"].read_text())
    shard = logical["accepted_shards"][0]
    shard["expected_trace_count"] = 2
    shard["graded_trace_count"] = 2
    logical["capture_summary"]["expected_trace_count"] = 2
    logical["capture_summary"]["graded_trace_count"] = 2
    logical_hash = _write_hashed_json(paths["logical"], logical, sidecar=True)
    outer = json.loads(paths["outer"].read_text())
    record = outer["logical_manifests"][0]
    record["sha256"] = logical_hash
    record["accepted_count"] = 2
    record["graded_count"] = 2
    summary = outer["accepted_capture_summary"]
    summary["agentdojo"]["accepted_trace_cell_count"] = 2
    summary["agentdojo"]["graded_trace_count"] = 2
    model_summary = summary["agentdojo"]["models"]["glm47"]
    model_summary["accepted_trace_cell_count"] = 2
    model_summary["graded_trace_count"] = 2
    _write_outer_and_lock(lock_path, paths["outer"], outer)
    _sync_original(lock_path, paths)

    with pytest.raises(ValueError, match="AgentDojo trace count mismatch"):
        load_cohort(lock_path)


def test_load_cohort_rejects_empty_protocol_evidence_list(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path, protocol_error=True)
    logical = json.loads(paths["logical"].read_text())
    logical["model_protocol_errors"] = []
    _reseal_current_logical(lock_path, paths, paths["logical"], 0, logical)

    with pytest.raises(ValueError, match="model protocol error identities"):
        load_cohort(lock_path)


def test_load_cohort_rejects_missing_current_outer_asset(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    paths["contract"].unlink()

    with pytest.raises(ValueError, match="capture contract"):
        load_cohort(lock_path)


def test_load_cohort_rejects_original_outer_manifest_mutation(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    paths["original_outer"].write_text(paths["original_outer"].read_text() + " ")

    with pytest.raises(ValueError, match="original outer manifest hash"):
        load_cohort(lock_path)


def test_load_cohort_requires_rejected_provenance_fact_parity_with_original(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    outer = json.loads(paths["outer"].read_text())
    outer["rejected_provenance"][0]["reason"] = "tampered current reason"
    _write_outer_and_lock(lock_path, paths["outer"], outer)

    with pytest.raises(ValueError, match="current rejected provenance facts changed from original"):
        load_cohort(lock_path)


def test_load_cohort_requires_original_rejected_provenance_coverage(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    original_outer = json.loads(paths["original_outer"].read_text())
    original_outer["rejected_provenance"] = []
    _write_original_outer_and_lock(lock_path, paths["original_outer"], original_outer)

    with pytest.raises(ValueError, match="original outer rejected provenance does not match rejected roots"):
        load_cohort(lock_path)


@pytest.mark.parametrize("outer_kind", ["current", "original"])
def test_load_cohort_requires_logical_manifest_sidecars_in_both_outer_chains(tmp_path, outer_kind):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    if outer_kind == "current":
        outer = json.loads(paths["outer"].read_text())
        del outer["logical_manifests"][0]["sha256_sidecar_path"]
        _write_outer_and_lock(lock_path, paths["outer"], outer)
    else:
        outer = json.loads(paths["original_outer"].read_text())
        del outer["logical_manifests"][0]["sha256_sidecar_path"]
        _write_original_outer_and_lock(lock_path, paths["original_outer"], outer)

    with pytest.raises(ValueError, match="logical manifest SHA-256 sidecar path"):
        load_cohort(lock_path)


def test_load_paper_main_v1_requires_original_outer_hash_anchor(tmp_path):
    lock_path, _ = _write_synthetic_cohort(tmp_path)
    lock = json.loads(lock_path.read_text())
    lock["original_outer_manifest"]["sha256"] = "0" * 64
    _write_json(lock_path, lock)

    with pytest.raises(ValueError, match="paper_main_v1 original outer manifest hash anchor"):
        load_cohort(lock_path)


def test_load_cohort_rejects_lock_cohort_id_change_before_anchor(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    anchored_hash = dataset._PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256
    original_outer = json.loads(paths["original_outer"].read_text())
    original_outer["phase"] = "replaced_capture"
    _write_original_outer_and_lock(lock_path, paths["original_outer"], original_outer)
    dataset._PAPER_MAIN_V1_ORIGINAL_OUTER_SHA256 = anchored_hash
    lock = json.loads(lock_path.read_text())
    lock["cohort_id"] = "unanchored_profile"
    _write_json(lock_path, lock)

    with pytest.raises(ValueError, match="unsupported cohort ID"):
        load_cohort(lock_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("status", "inactive")],
)
def test_load_cohort_rejects_lock_profile_schema_drift(tmp_path, field, value):
    lock_path, _ = _write_synthetic_cohort(tmp_path)
    lock = json.loads(lock_path.read_text())
    lock[field] = value
    _write_json(lock_path, lock)

    with pytest.raises(ValueError, match=f"lock {field}"):
        load_cohort(lock_path)


@pytest.mark.parametrize("outer_kind", ["current", "original"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("phase", "post_capture"), ("hash_algorithm", "sha512")],
)
def test_load_cohort_rejects_outer_profile_schema_drift(tmp_path, outer_kind, field, value):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    if outer_kind == "current":
        outer = json.loads(paths["outer"].read_text())
        outer[field] = value
        _write_outer_and_lock(lock_path, paths["outer"], outer)
    else:
        outer = json.loads(paths["original_outer"].read_text())
        outer[field] = value
        _write_original_outer_and_lock(lock_path, paths["original_outer"], outer)

    with pytest.raises(ValueError, match=f"{outer_kind} outer {field}"):
        load_cohort(lock_path)


def test_load_cohort_requires_equal_agentdojo_model_trace_memberships(tmp_path):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    _add_agentdojo_model(lock_path, paths)

    with pytest.raises(ValueError, match="AgentDojo model trace memberships differ"):
        load_cohort(lock_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "wrong-run"),
        ("model_id", "wrong-model"),
        ("provider", "wrong-provider"),
        ("endpoint_revision", "wrong-revision"),
    ],
)
def test_load_cohort_binds_agentdojo_leaf_identity_to_logical_manifest(tmp_path, field, value):
    lock_path, paths = _write_synthetic_cohort(tmp_path)
    for filename in ("capture-lock.json", "manifest.json"):
        path = paths["accepted"] / filename
        document = json.loads(path.read_text())
        document[field] = value
        _write_json(path, document)
    resealed = _reseal_leaf(paths["accepted"])
    logical = json.loads(paths["logical"].read_text())
    shard = logical["accepted_shards"][0]
    shard["root_sha256"] = resealed["root_sha256"]
    shard["file_count"] = resealed["file_count"]
    shard["evidence_bytes"] = resealed["evidence_bytes"]
    shard["manifest_sha256"] = resealed["hashes"]["manifest.json"]
    _reseal_current_logical(lock_path, paths, paths["logical"], 0, logical)

    with pytest.raises(ValueError, match="AgentDojo manifest identity mismatch"):
        load_cohort(lock_path)


def test_load_paper_main_v1_real_cohort():
    lock_path = Path(__file__).resolve().parents[1] / "paper_main_v1" / "cohort.lock.json"

    cohort = load_cohort(lock_path)

    assert cohort.cohort_id == "paper_main_v1"
    assert len(cohort.accepted_shards) == 14
