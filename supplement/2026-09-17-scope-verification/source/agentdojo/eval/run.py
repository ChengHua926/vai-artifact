"""Write deterministic review artifacts for the sealed paper cohort."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from eval.artifacts import (
    canonical_json_bytes,
    canonical_jsonl_bytes,
    sha256_bytes,
    sha256_file,
    source_file_record,
    write_artifact_bundle,
)
from eval.dataset import (
    EvaluationCohort,
    PAPER_MAIN_V1_ROOT,
    fetch_paper_main_v1,
    load_paper_main_v1,
    verify_paper_main_v1,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PAPER_MAIN_V1_ROOT


def evaluate_agentdojo(evaluation_cohort: EvaluationCohort) -> dict[str, Any]:
    from eval.agentdojo import evaluate_agentdojo as implementation

    return implementation(evaluation_cohort)


def build_grader_contracts() -> list[dict[str, Any]]:
    from eval.agentdojo.contracts import build_grader_contracts as implementation

    return implementation()


def load_agentdojo_cases(evaluation_cohort: EvaluationCohort) -> list[dict[str, Any]]:
    from eval.agentdojo.corpus import load_agentdojo_cases as implementation

    return implementation(evaluation_cohort)


def summarize_agentdojo(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from eval.agentdojo.evaluate import summarize_case_rows

    return summarize_case_rows(rows)


def register_shipped_promises() -> tuple[Any, list[dict[str, Any]]]:
    from eval.agentdojo.runtime import register_shipped_promises as implementation

    return implementation()


def evaluate_tau(
    evaluation_cohort: EvaluationCohort, *, tau_root: Path | None = None
) -> dict[str, Any]:
    from eval.tau import evaluate_tau as implementation

    return implementation(evaluation_cohort, tau_root=tau_root)


def effect_contract_manifest() -> list[dict[str, Any]]:
    from eval.tau.contracts import effect_contract_manifest as implementation

    return implementation()


def load_tau_cases(evaluation_cohort: EvaluationCohort) -> list[dict[str, Any]]:
    from eval.tau.corpus import load_tau_cases as implementation

    return implementation(evaluation_cohort)


def summarize_tau(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from eval.tau.evaluate import summarize_case_rows

    return summarize_case_rows(rows)


def compiled_monitor_manifest() -> dict[str, Any]:
    from eval.tau.monitor import compiled_monitor_manifest as implementation

    return implementation()

_AGENTDOJO_ROW_FILES = {
    "cases.jsonl": "case_rows",
    "calls.jsonl": "call_rows",
    "verdicts.jsonl": "verdict_rows",
    "grader_contracts.jsonl": "grader_contracts",
    "promises.jsonl": "promise_manifest",
}
_TAU_ROW_FILES = {
    "cases.jsonl": "case_rows",
    "calls.jsonl": "call_rows",
    "effects.jsonl": "effect_rows",
    "fires.jsonl": "fire_rows",
    "effect_contracts.jsonl": "effect_contracts",
}
_EXPECTED_LEDGER_COUNTS = {
    "agentdojo": {
        "calls": 7446,
        "cases": 2162,
        "grader_contracts": 35,
        "promises": 10,
        "verdicts": 4845,
    },
    "tau": {
        "calls": 2333,
        "cases": 328,
        "effect_contracts": 19,
        "effects": 900,
        "fires": 74,
        "monitor_manifest": 1,
    },
}
_TAU_MONITOR_SOURCES = (
    REPO_ROOT / "eval" / "predicates.py",
    REPO_ROOT / "eval" / "tau" / "adapter.py",
    REPO_ROOT / "eval" / "tau" / "promises.py",
    REPO_ROOT / "eval" / "tau" / "trace_adapter.py",
)
_COMMON_RUNTIME_SOURCES = tuple(
    (REPO_ROOT / "packages" / "commons" / "aa_commons").rglob("*.py")
)
_AGENTDOJO_RUNTIME_SOURCES = tuple(
    (REPO_ROOT / "packages" / "sdk" / "aa_sdk").rglob("*.py")
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_PROVENANCE_KEYS = frozenset(
    {
        "adjudicator",
        "cwd",
        "finished_at",
        "generated_at",
        "generated_at_utc",
        "host",
        "host_id",
        "host_name",
        "hostname",
        "label_author",
        "label_model",
        "llm_label",
        "llm_labels",
        "machine",
        "manual_label",
        "manual_labels",
        "reviewer",
        "run_timestamp",
        "started_at",
        "timestamp",
    }
)
_FORBIDDEN_PROVENANCE_PREFIXES = ("human_", "judge_", "llm_", "manual_")
_STATIC_DATASET_FILES = frozenset(
    {
        "MODEL_SELECTION.md",
        "MIGRATION.md",
        "README.md",
        "baseline.json",
        "cohort.lock.json",
        "dataset.json",
    }
)
_STATIC_DATASET_DIRS = frozenset({"corpus", "seal"})


def _validate_portable(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{location} JSON object key must be a string")
            folded_key = key.casefold()
            if folded_key in _FORBIDDEN_PROVENANCE_KEYS or folded_key.startswith(
                _FORBIDDEN_PROVENANCE_PREFIXES
            ):
                raise ValueError(f"forbidden provenance key at {location}: {key}")
            _validate_portable(child, f"{location}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_portable(child, f"{location}[{index}]")
    elif isinstance(value, str) and (
        Path(value).is_absolute()
        or value.startswith("\\\\")
        or value.casefold().startswith("file:")
        or _WINDOWS_ABSOLUTE_PATH.match(value)
    ):
        raise ValueError(f"absolute path at {location}: {value!r}")


def _require_rows(result: dict[str, Any], key: str, benchmark: str) -> list[dict[str, Any]]:
    rows = result.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{benchmark} {key} must be a list of objects")
    return rows


def _validate_summary(
    benchmark: str,
    result: dict[str, Any],
    summarize: Any,
) -> None:
    summary = result.get("summary")
    if not isinstance(summary, dict):
        raise ValueError(f"{benchmark} summary must be an object")
    expected = summarize(_require_rows(result, "case_rows", benchmark))
    if summary != expected:
        raise ValueError(f"{benchmark} summary is not the pure case fold")


def _cohort_identity(evaluation_cohort: EvaluationCohort) -> tuple[Any, ...]:
    cohort = evaluation_cohort.cohort
    shards = tuple(
        sorted(
            (
                shard.benchmark,
                shard.model_id,
                shard.kind,
                str(shard.path.resolve()),
                shard.root_sha256,
                shard.accepted_count,
            )
            for shard in cohort.accepted_shards
        )
    )
    return (
        cohort.cohort_id,
        evaluation_cohort.outer_sha256,
        str(cohort.lock_path.resolve()),
        str(cohort.outer_manifest_path.resolve()),
        shards,
    )


def _require_verified_cohort(
    supplied: EvaluationCohort, verified: EvaluationCohort
) -> None:
    if _cohort_identity(supplied) != _cohort_identity(verified):
        raise ValueError("cohort does not match freshly verified paper_main_v1")


def _require_linked_case_ids(
    benchmark: str,
    case_rows: list[dict[str, Any]],
    linked_ledgers: dict[str, list[dict[str, Any]]],
) -> None:
    case_ids: set[str] = set()
    case_keys: set[str] = set()
    for row in case_rows:
        case_id = row.get("case_id")
        case_key = row.get("case_key")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{benchmark} case_id must be a non-empty string")
        if case_id in case_ids:
            raise ValueError(f"duplicate {benchmark} case_id: {case_id}")
        if not isinstance(case_key, str) or not case_key:
            raise ValueError(f"{benchmark} case_key must be a non-empty string")
        if case_key in case_keys:
            raise ValueError(f"duplicate {benchmark} case_key: {case_key}")
        case_ids.add(case_id)
        case_keys.add(case_key)
    parent_by_id = {row["case_id"]: row for row in case_rows}
    identity_fields = {
        "agentdojo": (
            "case_key",
            "model_id",
            "suite",
            "user_task_id",
            "injection_task_id",
        ),
        "tau": ("case_key", "model_id", "domain", "task_id", "trial"),
    }[benchmark]
    for ledger, rows in linked_ledgers.items():
        for row in rows:
            case_id = row.get("case_id")
            if case_id not in case_ids:
                raise ValueError(
                    f"unknown {benchmark} case_id in {ledger}: {case_id!r}"
                )
            parent = parent_by_id[case_id]
            for field in identity_fields:
                if field not in row or row[field] != parent.get(field):
                    raise ValueError(
                        f"{benchmark} parent identity mismatch in {ledger}: "
                        f"{case_id} {field}"
                    )


_LEDGER_KEY_FIELDS = {
    ("agentdojo", "calls"): ("case_id", "seq"),
    ("agentdojo", "verdicts"): ("case_id", "promise_id"),
    ("agentdojo", "grader_contracts"): ("suite", "injection_task_id"),
    ("agentdojo", "promises"): ("promise_id",),
    ("tau", "calls"): ("case_id", "seq"),
    ("tau", "effects"): ("case_id", "path"),
    ("tau", "fires"): (
        "case_id",
        "seq",
        "predicate",
        "arm",
        "targets",
        "detail",
    ),
    ("tau", "effect_contracts"): ("arm",),
}


def _require_unique_ledger_rows(
    benchmark: str, ledger: str, rows: list[dict[str, Any]]
) -> None:
    fields = _LEDGER_KEY_FIELDS[(benchmark, ledger)]
    seen: set[bytes] = set()
    for index, row in enumerate(rows):
        missing = [field for field in fields if field not in row]
        if missing:
            raise ValueError(
                f"{benchmark} {ledger} row {index} lacks key field {missing[0]}"
            )
        key = canonical_json_bytes({field: row[field] for field in fields})
        if key in seen:
            raise ValueError(f"duplicate {benchmark} {ledger} natural key")
        seen.add(key)


def _require_field_references(
    label: str,
    rows: list[dict[str, Any]],
    field: str,
    allowed: set[Any],
) -> None:
    for row in rows:
        if row.get(field) not in allowed:
            raise ValueError(f"unknown {field} in {label}: {row.get(field)!r}")


def _require_exact_manifest(
    label: str,
    actual: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> None:
    if canonical_jsonl_bytes(actual) != canonical_jsonl_bytes(expected):
        raise ValueError(f"{label} mismatch")


_AGENTDOJO_VERDICT_EVIDENCE_FIELDS = (
    "promise_id",
    "suite",
    "tool",
    "recipient_key",
    "violated",
    "seq",
    "reason",
    "raw_target",
    "parse_error",
)


def _validate_agentdojo_exact_matches(
    cases: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
) -> None:
    call_by_key = {(row["case_id"], row["seq"]): row for row in calls}
    verdict_by_key = {
        (row["case_id"], row["promise_id"]): row for row in verdicts
    }
    for case in cases:
        matches = case.get("exact_matches")
        if not isinstance(matches, list):
            raise ValueError("agentdojo exact_matches must be a list")
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError("agentdojo exact match must be an object")
            call_evidence = match.get("call")
            verdict_evidence = match.get("verdict")
            if not isinstance(call_evidence, dict) or not isinstance(
                verdict_evidence, dict
            ):
                raise ValueError("agentdojo exact match lacks call or verdict evidence")
            if call_evidence.get("seq") != verdict_evidence.get("seq"):
                raise ValueError("agentdojo exact match is not the same causal event")

            call = call_by_key.get((case["case_id"], call_evidence.get("seq")))
            verdict = verdict_by_key.get(
                (case["case_id"], verdict_evidence.get("promise_id"))
            )
            if call is None or verdict is None:
                raise ValueError("agentdojo exact match references absent evidence")
            if (
                call.get("seq") != verdict.get("seq")
                or call.get("tool") != verdict.get("tool")
                or verdict.get("recipient_key") is None
                or verdict.get("parse_error") is not None
                or not verdict.get("violated")
            ):
                raise ValueError("agentdojo exact match is not the same causal event")

            recipient_key = verdict["recipient_key"]
            raw_target = verdict.get("raw_target")
            declared = (call.get("args") or {}).get(recipient_key)
            declared_values = declared if isinstance(declared, list) else [declared]
            if raw_target not in declared_values:
                raise ValueError("agentdojo exact target is absent from the causal call")
            expected_call = {
                "seq": call["seq"],
                "tool": call["tool"],
                "recipient_key": recipient_key,
                "raw_target": raw_target,
                "execution_status": call.get("execution_status"),
                "replay_status": call.get("replay_status"),
            }
            if call_evidence != expected_call:
                raise ValueError("agentdojo exact call evidence disagrees with ledger")
            expected_verdict = {
                field: verdict.get(field)
                for field in _AGENTDOJO_VERDICT_EVIDENCE_FIELDS
            }
            if verdict_evidence != expected_verdict:
                raise ValueError("agentdojo exact verdict evidence disagrees with ledger")
            if (
                expected_call["execution_status"] != "successful"
                or expected_call["replay_status"] != "successful"
            ):
                raise ValueError("agentdojo exact match call is not successful")


def _case_membership(
    rows: list[dict[str, Any]], outer_sha256: str
) -> dict[str, tuple[str, str, str]]:
    return {
        str(row.get("case_id")): (
            str(row.get("case_key")),
            str(row.get("model_id")),
            str(row.get("cohort_outer_sha256")),
        )
        for row in rows
        if row.get("cohort_outer_sha256") == outer_sha256
    }


def _require_case_membership(
    benchmark: str,
    actual: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    outer_sha256: str,
) -> None:
    if _case_membership(actual, outer_sha256) != _case_membership(
        expected, outer_sha256
    ) or len(actual) != len(expected):
        raise ValueError(f"{benchmark} case membership mismatch")


def _validate_agentdojo_ledgers(result: dict[str, Any]) -> None:
    cases = _require_rows(result, "case_rows", "agentdojo")
    calls = _require_rows(result, "call_rows", "agentdojo")
    verdicts = _require_rows(result, "verdict_rows", "agentdojo")
    contracts = _require_rows(result, "grader_contracts", "agentdojo")
    promises = _require_rows(result, "promise_manifest", "agentdojo")
    for ledger, rows in (
        ("calls", calls),
        ("verdicts", verdicts),
        ("grader_contracts", contracts),
        ("promises", promises),
    ):
        _require_unique_ledger_rows("agentdojo", ledger, rows)

    _require_exact_manifest(
        "agentdojo grader contract manifest", contracts, build_grader_contracts()
    )
    _, expected_promises = register_shipped_promises()
    _require_exact_manifest("agentdojo promise manifest", promises, expected_promises)

    promise_by_id = {row["promise_id"]: row for row in promises}
    _require_field_references(
        "agentdojo verdicts", verdicts, "promise_id", set(promise_by_id)
    )
    case_by_id = {row["case_id"]: row for row in cases}
    for verdict in verdicts:
        promise = promise_by_id[verdict["promise_id"]]
        parent = case_by_id[verdict["case_id"]]
        if promise.get("suite") != parent.get("suite"):
            raise ValueError("agentdojo verdict promise is not applicable to parent suite")
        for field in ("suite", "tool", "recipient_key"):
            if verdict.get(field) != promise.get(field):
                raise ValueError(
                    f"agentdojo verdict disagrees with promise manifest: {field}"
                )
    _validate_agentdojo_exact_matches(cases, calls, verdicts)


def _validate_tau_exact_links(
    cases: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    effects: list[dict[str, Any]],
    fires: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    implementation: Any,
) -> None:
    case_ids = {row.get("case_id") for row in cases}
    call_by_key = {(row.get("case_id"), row.get("seq")): row for row in calls}
    effect_by_key = {
        (row.get("case_id"), canonical_json_bytes(row.get("path"))): row
        for row in effects
    }
    contract_by_arm = {row.get("arm"): row for row in contracts}
    fire_links_by_case: dict[Any, set[bytes]] = {
        case_id: set() for case_id in case_ids
    }

    for fire in fires:
        case_id = fire.get("case_id")
        if case_id not in case_ids:
            raise ValueError("tau fire references an absent case")
        call = call_by_key.get((case_id, fire.get("seq")))
        if call is None or call.get("tool") != fire.get("tool"):
            raise ValueError("tau fire does not reference its causal call")
        if fire.get("implementation") != implementation:
            raise ValueError("tau fire implementation disagrees with monitor manifest")

        links = fire.get("exact_links")
        if not isinstance(links, list):
            raise ValueError("tau fire exact_links must be a list")
        for link in links:
            if (
                not isinstance(link, dict)
                or link.get("arm") != fire.get("arm")
                or link.get("seq") != fire.get("seq")
                or link.get("tool") != fire.get("tool")
            ):
                raise ValueError("tau exact link disagrees with fire")

            contract = contract_by_arm.get(link.get("arm"))
            if contract is None or link.get("protected_projection") != contract.get(
                "protected_projection"
            ):
                raise ValueError("tau exact link protected projection disagrees with contract")

            paths = link.get("effect_paths")
            if not isinstance(paths, list) or not paths:
                raise ValueError("tau exact link lacks effect paths")
            path_keys = [canonical_json_bytes(path) for path in paths]
            if len(path_keys) != len(set(path_keys)):
                raise ValueError("tau exact link has a duplicate effect path")
            for path_key in path_keys:
                effect = effect_by_key.get((case_id, path_key))
                if (
                    effect is None
                    or effect.get("writer_seq") != fire.get("seq")
                    or effect.get("writer_tool") != fire.get("tool")
                ):
                    raise ValueError("tau exact link does not reference its written effect")

            link_key = canonical_json_bytes(link)
            if link_key in fire_links_by_case[case_id]:
                raise ValueError("tau fire ledger has a duplicate exact link")
            fire_links_by_case[case_id].add(link_key)

    for case in cases:
        links = case.get("exact_links")
        if not isinstance(links, list):
            raise ValueError("tau case exact_links must be a list")
        link_keys = [canonical_json_bytes(link) for link in links]
        if len(link_keys) != len(set(link_keys)):
            raise ValueError("tau case has a duplicate exact link")
        if set(link_keys) != fire_links_by_case[case.get("case_id")]:
            raise ValueError("tau case exact links do not equal the fire ledger")


def _validate_tau_ledgers(result: dict[str, Any]) -> None:
    cases = _require_rows(result, "case_rows", "tau")
    calls = _require_rows(result, "call_rows", "tau")
    effects = _require_rows(result, "effect_rows", "tau")
    fires = _require_rows(result, "fire_rows", "tau")
    contracts = _require_rows(result, "effect_contracts", "tau")
    monitor = result.get("monitor_manifest")
    if not isinstance(monitor, dict):
        raise ValueError("tau monitor_manifest must be an object")
    for ledger, rows in (
        ("calls", calls),
        ("effects", effects),
        ("fires", fires),
        ("effect_contracts", contracts),
    ):
        _require_unique_ledger_rows("tau", ledger, rows)

    _require_exact_manifest(
        "tau effect contract manifest", contracts, effect_contract_manifest()
    )
    _require_exact_manifest("monitor manifest", [monitor], [compiled_monitor_manifest()])
    arms = {row["arm"] for row in contracts}
    _require_field_references("tau fires", fires, "arm", arms)
    _validate_tau_exact_links(
        cases,
        calls,
        effects,
        fires,
        contracts,
        monitor.get("implementation"),
    )


def _benchmark_ledger_counts(
    benchmark: str, result: dict[str, Any]
) -> dict[str, int]:
    row_files = _AGENTDOJO_ROW_FILES if benchmark == "agentdojo" else _TAU_ROW_FILES
    counts = {
        Path(filename).stem: len(_require_rows(result, key, benchmark))
        for filename, key in row_files.items()
    }
    if benchmark == "tau":
        counts["monitor_manifest"] = (
            1 if isinstance(result.get("monitor_manifest"), dict) else 0
        )
    return counts


def _ledger_counts(
    agentdojo_result: dict[str, Any], tau_result: dict[str, Any]
) -> dict[str, dict[str, int]]:
    return {
        "agentdojo": _benchmark_ledger_counts("agentdojo", agentdojo_result),
        "tau": _benchmark_ledger_counts("tau", tau_result),
    }


def _validate_paper_main_benchmark_result(
    evaluation_cohort: EvaluationCohort,
    benchmark: str,
    result: dict[str, Any],
) -> None:
    _validate_portable(result, benchmark)
    summarize = summarize_agentdojo if benchmark == "agentdojo" else summarize_tau
    _validate_summary(benchmark, result, summarize)
    counts = _benchmark_ledger_counts(benchmark, result)
    expected = _EXPECTED_LEDGER_COUNTS[benchmark]
    if counts != expected:
        raise ValueError(f"{benchmark} ledger count mismatch: {counts} != {expected}")

    cases = _require_rows(result, "case_rows", benchmark)
    if benchmark == "agentdojo":
        _require_linked_case_ids(
            benchmark,
            cases,
            {
                "calls": _require_rows(result, "call_rows", benchmark),
                "verdicts": _require_rows(result, "verdict_rows", benchmark),
            },
        )
        _validate_agentdojo_ledgers(result)
        expected_cases = load_agentdojo_cases(evaluation_cohort)
    else:
        _require_linked_case_ids(
            benchmark,
            cases,
            {
                "calls": _require_rows(result, "call_rows", benchmark),
                "effects": _require_rows(result, "effect_rows", benchmark),
                "fires": _require_rows(result, "fire_rows", benchmark),
            },
        )
        _validate_tau_ledgers(result)
        expected_cases = load_tau_cases(evaluation_cohort)
    _require_case_membership(
        benchmark,
        cases,
        expected_cases,
        evaluation_cohort.outer_sha256,
    )


def _validate_paper_main_results(
    evaluation_cohort: EvaluationCohort,
    agentdojo_result: dict[str, Any],
    tau_result: dict[str, Any],
) -> None:
    _validate_paper_main_benchmark_result(
        evaluation_cohort, "agentdojo", agentdojo_result
    )
    _validate_paper_main_benchmark_result(evaluation_cohort, "tau", tau_result)


def _source_manifest(benchmark: str) -> list[dict[str, str]]:
    shared = (
        REPO_ROOT / "eval" / "artifacts.py",
        REPO_ROOT / "eval" / "dataset.py",
    )
    local_root = (
        REPO_ROOT / "eval" / "agentdojo"
        if benchmark == "agentdojo"
        else REPO_ROOT / "eval" / benchmark
    )
    local = tuple(
        sorted(
            local_root.glob("*.py"),
            key=lambda path: path.as_posix().encode("utf-8"),
        )
    )
    if benchmark == "tau":
        extra = (*_TAU_MONITOR_SOURCES, *_COMMON_RUNTIME_SOURCES)
    else:
        extra = (*_COMMON_RUNTIME_SOURCES, *_AGENTDOJO_RUNTIME_SOURCES)
    paths = sorted(
        {*shared, *local, *extra}, key=lambda path: path.as_posix().encode("utf-8")
    )
    return [source_file_record(path, root=REPO_ROOT) for path in paths]


def _source_provenance() -> dict[str, Any]:
    return {
        "evaluator_sources": {
            "agentdojo": _source_manifest("agentdojo"),
            "tau": _source_manifest("tau"),
        },
        "bundle_runner_source": source_file_record(Path(__file__), root=REPO_ROOT),
    }


_IMPORTED_SOURCE_PROVENANCE = _source_provenance()


def _require_unchanged_sources(expected: dict[str, Any]) -> None:
    if _source_provenance() != expected:
        raise ValueError("source files changed during evaluation")


def _allowed_output_files() -> set[str]:
    agentdojo = {
        f"agentdojo/{name}" for name in (*_AGENTDOJO_ROW_FILES, "summary.json")
    }
    tau = {
        f"tau/{name}"
        for name in (*_TAU_ROW_FILES, "monitor_manifest.jsonl", "summary.json")
    }
    return {
        "index.json",
        "SHA256SUMS",
        "agentdojo/SHA256SUMS",
        "tau/SHA256SUMS",
        *agentdojo,
        *tau,
    }


def _preflight_output(output_dir: Path) -> None:
    destination = Path(output_dir)
    if not destination.exists():
        return
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError(f"output directory must be a regular directory: {destination}")
    consolidated_root = destination.resolve(strict=True) == Path(
        PAPER_MAIN_V1_ROOT
    ).resolve(strict=True)
    allowed_files = _allowed_output_files()
    allowed_dirs = {"agentdojo", "tau"}
    for existing in destination.rglob("*"):
        relative = existing.relative_to(destination).as_posix()
        if existing.is_symlink():
            raise ValueError(f"symlink output artifact is not allowed: {relative}")
        parts = Path(relative).parts
        if consolidated_root and (
            (existing.is_file() and relative in _STATIC_DATASET_FILES)
            or (
                parts
                and parts[0] in _STATIC_DATASET_DIRS
                and (destination / parts[0]).is_dir()
            )
        ):
            continue
        if existing.is_dir():
            if relative not in allowed_dirs:
                raise ValueError(f"unexpected output artifact: {relative}")
        elif relative not in allowed_files:
            raise ValueError(f"unexpected output artifact: {relative}")


def _root_sums(output_dir: Path, index_bytes: bytes) -> bytes:
    entries = {
        "agentdojo/SHA256SUMS": sha256_file(output_dir / "agentdojo" / "SHA256SUMS"),
        "index.json": sha256_bytes(index_bytes),
        "tau/SHA256SUMS": sha256_file(output_dir / "tau" / "SHA256SUMS"),
    }
    return b"".join(
        f"{entries[name]}  {name}\n".encode("utf-8")
        for name in sorted(entries, key=lambda value: value.encode("utf-8"))
    )


def _benchmark_index(
    *, hashes: dict[str, str], row_files: dict[str, list[dict[str, Any]]], sums_path: Path
) -> dict[str, Any]:
    return {
        "artifact_hashes": dict(sorted(hashes.items())),
        "ledger_counts": {
            Path(filename).stem: len(rows)
            for filename, rows in sorted(row_files.items())
        },
        "sha256sums_sha256": sha256_file(sums_path),
        "summary_sha256": hashes["summary.json"],
    }


def _benchmark_rows(
    benchmark: str, result: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    row_mapping = (
        _AGENTDOJO_ROW_FILES if benchmark == "agentdojo" else _TAU_ROW_FILES
    )
    rows = {
        filename: _require_rows(result, key, benchmark)
        for filename, key in row_mapping.items()
    }
    if benchmark == "tau":
        monitor_manifest = result.get("monitor_manifest")
        if not isinstance(monitor_manifest, dict):
            raise ValueError("tau monitor_manifest must be an object")
        rows["monitor_manifest.jsonl"] = [monitor_manifest]
    return rows


def _write_benchmark_artifact(
    output_dir: Path,
    *,
    benchmark: str,
    result: dict[str, Any],
    _expected_sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one deterministic child ledger without a combined root index."""

    if benchmark not in {"agentdojo", "tau"}:
        raise ValueError(f"unsupported benchmark: {benchmark}")
    source_provenance = _expected_sources or _IMPORTED_SOURCE_PROVENANCE
    _require_unchanged_sources(source_provenance)
    _validate_portable(result, benchmark)
    summarize = summarize_agentdojo if benchmark == "agentdojo" else summarize_tau
    _validate_summary(benchmark, result, summarize)
    rows = _benchmark_rows(benchmark, result)
    destination = Path(output_dir) / benchmark
    hashes = write_artifact_bundle(
        destination,
        row_files=rows,
        summary=result["summary"],
    )
    _require_unchanged_sources(source_provenance)
    index = _benchmark_index(
        hashes=hashes,
        row_files=rows,
        sums_path=destination / "SHA256SUMS",
    )
    if benchmark == "tau":
        index["monitor_parity_mismatch_count"] = len(
            _require_rows(result, "monitor_parity_mismatches", "tau")
        )
    return index


def _write_artifact_tree(
    output_dir: Path,
    *,
    evaluation_cohort: EvaluationCohort,
    agentdojo_result: dict[str, Any],
    tau_result: dict[str, Any],
    _expected_sources: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize a caller-validated result tree; production uses the strict wrapper."""

    source_provenance = _expected_sources or _IMPORTED_SOURCE_PROVENANCE
    _require_unchanged_sources(source_provenance)
    if evaluation_cohort.cohort.cohort_id != "paper_main_v1":
        raise ValueError("evaluation bundle requires paper_main_v1")
    _validate_portable(agentdojo_result, "agentdojo")
    _validate_portable(tau_result, "tau")
    _validate_summary("agentdojo", agentdojo_result, summarize_agentdojo)
    _validate_summary("tau", tau_result, summarize_tau)
    _preflight_output(output_dir)

    agentdojo_rows = {
        filename: _require_rows(agentdojo_result, key, "agentdojo")
        for filename, key in _AGENTDOJO_ROW_FILES.items()
    }
    tau_rows = {
        filename: _require_rows(tau_result, key, "tau")
        for filename, key in _TAU_ROW_FILES.items()
    }
    monitor_manifest = tau_result.get("monitor_manifest")
    if not isinstance(monitor_manifest, dict):
        raise ValueError("tau monitor_manifest must be an object")
    tau_rows["monitor_manifest.jsonl"] = [monitor_manifest]

    destination = Path(output_dir)
    agentdojo_hashes = write_artifact_bundle(
        destination / "agentdojo",
        row_files=agentdojo_rows,
        summary=agentdojo_result["summary"],
    )
    tau_hashes = write_artifact_bundle(
        destination / "tau",
        row_files=tau_rows,
        summary=tau_result["summary"],
    )
    _require_unchanged_sources(source_provenance)

    index = {
        "schema_version": 1,
        "cohort": {
            "cohort_id": evaluation_cohort.cohort.cohort_id,
            "outer_sha256": evaluation_cohort.outer_sha256,
        },
        **source_provenance,
        "benchmarks": {
            "agentdojo": _benchmark_index(
                hashes=agentdojo_hashes,
                row_files=agentdojo_rows,
                sums_path=destination / "agentdojo" / "SHA256SUMS",
            ),
            "tau": {
                **_benchmark_index(
                    hashes=tau_hashes,
                    row_files=tau_rows,
                    sums_path=destination / "tau" / "SHA256SUMS",
                ),
                "monitor_parity_mismatch_count": len(
                    _require_rows(tau_result, "monitor_parity_mismatches", "tau")
                ),
            },
        },
    }
    index_bytes = canonical_json_bytes(index)
    (destination / "index.json").write_bytes(index_bytes)
    (destination / "SHA256SUMS").write_bytes(_root_sums(destination, index_bytes))
    return index


def _write_verified_evaluation_bundle(
    output_dir: Path,
    *,
    evaluation_cohort: EvaluationCohort,
    agentdojo_result: dict[str, Any],
    tau_result: dict[str, Any],
) -> dict[str, Any]:
    """Internal validator/serializer for results produced by this module."""

    verified = load_paper_main_v1()
    _require_verified_cohort(evaluation_cohort, verified)
    _validate_paper_main_results(verified, agentdojo_result, tau_result)
    return _write_artifact_tree(
        output_dir,
        evaluation_cohort=verified,
        agentdojo_result=agentdojo_result,
        tau_result=tau_result,
        _expected_sources=_IMPORTED_SOURCE_PROVENANCE,
    )


def _write_verified_benchmark_bundle(
    output_dir: Path,
    *,
    evaluation_cohort: EvaluationCohort,
    benchmark: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Validate and serialize one selected paper-main benchmark."""

    verified = load_paper_main_v1()
    _require_verified_cohort(evaluation_cohort, verified)
    _validate_paper_main_benchmark_result(verified, benchmark, result)
    return _write_benchmark_artifact(
        output_dir,
        benchmark=benchmark,
        result=result,
        _expected_sources=_IMPORTED_SOURCE_PROVENANCE,
    )


def generate_evaluation_bundle(
    output_dir: Path = DEFAULT_OUTPUT_DIR, *, tau_root: Path | None = None
) -> dict[str, Any]:
    """Generate the complete paper-main evaluation bundle."""

    source_provenance = _IMPORTED_SOURCE_PROVENANCE
    _require_unchanged_sources(source_provenance)
    evaluation_cohort = load_paper_main_v1()
    agentdojo_result = evaluate_agentdojo(evaluation_cohort)
    tau_result = evaluate_tau(evaluation_cohort, tau_root=tau_root)
    _require_unchanged_sources(source_provenance)
    return _write_verified_evaluation_bundle(
        output_dir,
        evaluation_cohort=evaluation_cohort,
        agentdojo_result=agentdojo_result,
        tau_result=tau_result,
    )


def generate_benchmark_bundle(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    benchmark: str,
    tau_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate and write exactly one paper-main benchmark child bundle."""

    if benchmark not in {"agentdojo", "tau"}:
        raise ValueError(f"unsupported benchmark: {benchmark}")
    source_provenance = _IMPORTED_SOURCE_PROVENANCE
    _require_unchanged_sources(source_provenance)
    evaluation_cohort = load_paper_main_v1()
    if benchmark == "agentdojo":
        result = evaluate_agentdojo(evaluation_cohort)
    else:
        result = evaluate_tau(evaluation_cohort, tau_root=tau_root)
    _require_unchanged_sources(source_provenance)
    return _write_verified_benchmark_bundle(
        output_dir,
        evaluation_cohort=evaluation_cohort,
        benchmark=benchmark,
        result=result,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subcommands.add_parser("fetch", help="restore the sealed corpus")
    fetch_parser.add_argument("--archive", type=Path)
    fetch_parser.add_argument("--sha256")
    fetch_parser.add_argument("--bytes", type=int)

    subcommands.add_parser("verify", help="verify the sealed corpus offline")

    evaluate_parser = subcommands.add_parser(
        "evaluate", help="evaluate the verified corpus offline"
    )
    evaluate_parser.add_argument(
        "--benchmark", choices=("all", "agentdojo", "tau"), required=True
    )
    evaluate_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    evaluate_parser.add_argument("--tau-root", type=Path)
    args = parser.parse_args(argv)

    if args.command == "fetch":
        restored = fetch_paper_main_v1(
            args.archive,
            archive_sha256=args.sha256,
            archive_bytes=args.bytes,
        )
        print(
            f"RESTORED: {restored.cohort.cohort_id} "
            f"({len(restored.cohort.accepted_shards)} accepted shards)"
        )
        return 0
    if args.command == "verify":
        verified = verify_paper_main_v1()
        print(
            f"PASS: {verified.cohort.cohort_id} "
            f"({len(verified.cohort.accepted_shards)} accepted shards)"
        )
        return 0

    if args.benchmark == "all":
        record = generate_evaluation_bundle(args.output, tau_root=args.tau_root)
    else:
        record = generate_benchmark_bundle(
            args.output,
            benchmark=args.benchmark,
            tau_root=args.tau_root,
        )
    print(sha256_bytes(canonical_json_bytes(record)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
