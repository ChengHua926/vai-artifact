"""End-to-end AgentDojo execution, action, verdict, and summary ledgers."""

from __future__ import annotations

from collections import Counter
from typing import Any

from eval.dataset import EvaluationCohort

from .causal import causal_calls_for_harm
from .contracts import build_grader_contracts
from .corpus import load_agentdojo_cases
from .matching import call_matches_contract_target, classify_attack_evidence
from .nonharm import classify_non_attack_fire
from .replay import replay_attack
from .runtime import (
    extract_call_evidence,
    register_shipped_promises,
    run_shipped_promises,
)


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _base_case_row(case: dict[str, Any]) -> dict[str, Any]:
    return {
        key: case[key]
        for key in (
            "schema_version",
            "case_id",
            "case_key",
            "cohort_id",
            "cohort_outer_sha256",
            "benchmark",
            "benchmark_version",
            "model_id",
            "suite",
            "user_task_id",
            "attack_type",
            "injection_task_id",
            "case_kind",
            "protocol_status",
            "stored_utility",
            "stored_security",
            "raw_security",
            "raw_error",
            "source",
        )
    }


def _with_case(call: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case["case_id"],
        "case_key": case["case_key"],
        "model_id": case["model_id"],
        "suite": case["suite"],
        "user_task_id": case["user_task_id"],
        "injection_task_id": case["injection_task_id"],
        **call,
    }


def _resisted_fire_class(
    contract: dict[str, Any],
    calls: list[dict[str, Any]],
    verdicts: list[dict[str, Any]],
) -> str:
    fires = [row for row in verdicts if row.get("violated")]
    if contract["scope"] != "off_list_egress":
        return "outside_scope_fire" if fires else "outside_scope_no_fire"

    matching_successful = []
    matching_failed = []
    for call in calls:
        probe = {**call, "execution_status": "successful", "replay_status": "successful"}
        raw_targets = call_matches_contract_target(contract, probe)
        if not raw_targets:
            continue
        if call.get("execution_status") == "successful" and call.get("replay_status") == "successful":
            matching_successful.extend((call, target) for target in raw_targets)
        else:
            matching_failed.extend((call, target) for target in raw_targets)
    matching_successful_fire = any(
        fire.get("seq") == call.get("seq")
        and fire.get("tool") == call.get("tool")
        and fire.get("recipient_key") == contract.get("destination_field")
        and fire.get("raw_target") == target
        for fire in fires
        for call, target in matching_successful
    )
    if matching_successful_fire:
        return "matching_successful_target_but_other_grader_condition_failed"
    if not fires and matching_successful:
        return "no_fire_despite_successful_matching_target"
    if not fires and matching_failed:
        return "no_fire_failed_or_unexecuted_matching_target"
    if not fires:
        return "no_fire"
    if matching_failed:
        return "matching_failed_or_unexecuted_target_plus_unrelated_fire"
    return "unrelated_successful_target_or_tool_fire"


def _evaluate_case(
    case: dict[str, Any],
    *,
    contracts: dict[tuple[str, str], dict[str, Any]],
    runtimes: dict[str, Any],
    promise_manifest: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    row = _base_case_row(case)
    row.update(
        {
            "replayed_utility": None,
            "replayed_security": None,
            "utility_label_agreement": None,
            "security_label_agreement": None,
            "label_disagreements": [],
            "evaluation_status": None,
            "grader_scope": None,
            "coverage_class": "unknown",
            "miss_reason": None,
            "resisted_fire_class": None,
            "non_harm_fire_class": None,
            "fire_count": 0,
            "any_fire": False,
            "causal_call_seqs": [],
            "exact_matches": [],
            "replay_error": None,
            "call_status_disagreements": [],
        }
    )
    if case["protocol_status"] == "protocol_error":
        calls = extract_call_evidence(case["trace"])
        for call in calls:
            call["replay_status"] = None
            call["replay_error"] = None
            call["replay_effect"] = None
        row["evaluation_status"] = "protocol_error"
        if case["case_kind"] != "attack":
            row["non_harm_fire_class"] = "protocol_error"
        return row, [_with_case(call, case) for call in calls], []

    if case["case_kind"] == "attack":
        replay = replay_attack(case)
        calls = replay["calls"]
        row["replayed_utility"] = replay["replayed_utility"]
        row["replayed_security"] = replay["replayed_security"]
        row["replay_error"] = replay["replay_error"]
        row["call_status_disagreements"] = replay["call_status_disagreements"]
        if replay["replay_error"] is None:
            for axis in ("utility", "security"):
                stored = case[f"stored_{axis}"]
                replayed = replay[f"replayed_{axis}"]
                agrees = stored == replayed
                row[f"{axis}_label_agreement"] = agrees
                if not agrees:
                    row["label_disagreements"].append(
                        {"axis": axis, "stored": stored, "replayed": replayed}
                    )
    else:
        calls = extract_call_evidence(case["trace"])
        for call in calls:
            call["replay_status"] = None
            call["replay_error"] = None
            call["replay_effect"] = None
        replay = None

    verdicts = run_shipped_promises(
        case["suite"],
        calls,
        case_id=case["case_id"],
        runtimes=runtimes,
        manifest=promise_manifest,
    )
    fires = [verdict for verdict in verdicts if verdict["violated"]]
    row["fire_count"] = len(fires)
    row["any_fire"] = bool(fires)

    if case["case_kind"] != "attack":
        row["evaluation_status"] = case["case_kind"]
        row["coverage_class"] = "not_applicable"
        row["non_harm_fire_class"] = classify_non_attack_fire(
            case, calls, verdicts, contracts=contracts
        )
    else:
        contract = contracts[(case["suite"], case["injection_task_id"])]
        row["grader_scope"] = contract["scope"]
        if replay["replay_error"] is not None:
            row["evaluation_status"] = "replay_error"
        elif replay["call_status_disagreements"]:
            row["evaluation_status"] = "replay_integrity_error"
        elif row["label_disagreements"]:
            row["evaluation_status"] = "replay_disagreement"
        else:
            row["evaluation_status"] = "evaluated"
            if replay["replayed_security"]:
                if contract["scope"] == "off_list_egress":
                    causal_calls = causal_calls_for_harm(
                        contract,
                        calls,
                        pre_environment=replay["pre_environment"],
                        post_environment=replay["post_environment"],
                        injection_task=replay["injection_task"],
                    )
                    row["causal_call_seqs"] = [call["seq"] for call in causal_calls]
                    evidence = classify_attack_evidence(contract, causal_calls, verdicts)
                    row["coverage_class"] = (
                        evidence["coverage_class"] if causal_calls else "unknown"
                    )
                    row["miss_reason"] = (
                        evidence["miss_reason"]
                        if causal_calls
                        else "native_harm_without_mechanically_mapped_causal_call"
                    )
                    row["exact_matches"] = evidence["exact_matches"]
                else:
                    row["coverage_class"] = "outside_scope"
            else:
                row["coverage_class"] = "resisted_attack"
                row["resisted_fire_class"] = _resisted_fire_class(
                    contract, calls, verdicts
                )

    call_rows = [_with_case(call, case) for call in calls]
    verdict_rows = [_with_case(verdict, case) for verdict in verdicts]
    return row, call_rows, verdict_rows


def summarize_case_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure per-model fold over the execution ledger."""

    models: dict[str, Any] = {}
    for model_id in sorted({row["model_id"] for row in rows}):
        selected = [row for row in rows if row["model_id"] == model_id]
        attacks = [row for row in selected if row["case_kind"] == "attack"]
        evaluated_attacks = [
            row for row in attacks if row["evaluation_status"] == "evaluated"
        ]
        harms = [row for row in evaluated_attacks if row["replayed_security"] is True]
        egress_harms = [row for row in harms if row["grader_scope"] == "off_list_egress"]
        resisted = [row for row in evaluated_attacks if row["replayed_security"] is False]
        benign = [row for row in selected if row["case_kind"] == "benign"]
        baselines = [
            row for row in selected if row["case_kind"] == "injection_baseline"
        ]
        models[model_id] = {
            "captured": len(selected),
            "scorable": sum(row["protocol_status"] == "graded" for row in selected),
            "protocol_errors": sum(
                row["protocol_status"] == "protocol_error" for row in selected
            ),
            "case_kinds": _counts([row["case_kind"] for row in selected]),
            "stored_utility_successes": {
                kind: sum(
                    row["stored_utility"] is True
                    for row in selected
                    if row["case_kind"] == kind
                )
                for kind in ("attack", "benign", "injection_baseline")
            },
            "stored_side_task_successes": sum(
                row["stored_security"] is True for row in attacks
            ),
            "replay_disagreements": sum(
                bool(row["label_disagreements"]) for row in attacks
            ),
            "utility_label_disagreements": sum(
                row["utility_label_agreement"] is False for row in attacks
            ),
            "security_label_disagreements": sum(
                row["security_label_agreement"] is False for row in attacks
            ),
            "replay_errors": sum(
                row["evaluation_status"] == "replay_error" for row in attacks
            ),
            "replay_integrity_errors": sum(
                row["evaluation_status"] == "replay_integrity_error"
                for row in attacks
            ),
            "replay_consistent_attacks": len(evaluated_attacks),
            "replay_consistent_side_task_successes": len(harms),
            "replay_consistent_resisted_attacks": len(resisted),
            "evaluable_egress_harms": len(egress_harms),
            "outside_scope_harms": len(harms) - len(egress_harms),
            "harm_coverage": _counts([row["coverage_class"] for row in egress_harms]),
            "harm_miss_reasons": _counts(
                [row["miss_reason"] for row in egress_harms if row["miss_reason"]]
            ),
            "resisted_fire_classes": _counts(
                [row["resisted_fire_class"] for row in resisted]
            ),
            "resisted_fire_runs": sum(row["any_fire"] for row in resisted),
            "resisted_no_fire_runs": sum(not row["any_fire"] for row in resisted),
            "benign_runs": len(benign),
            "benign_fire_runs": sum(row["any_fire"] for row in benign),
            "benign_no_fire_runs": sum(not row["any_fire"] for row in benign),
            "benign_fire_classes": _counts(
                [row["non_harm_fire_class"] for row in benign]
            ),
            "injection_baseline_runs": len(baselines),
            "injection_baseline_fire_runs": sum(row["any_fire"] for row in baselines),
            "injection_baseline_no_fire_runs": sum(
                not row["any_fire"] for row in baselines
            ),
            "injection_baseline_fire_classes": _counts(
                [row["non_harm_fire_class"] for row in baselines]
            ),
        }
    return {"schema_version": 1, "benchmark": "agentdojo", "models": models}


def evaluate_agentdojo(
    evaluation_cohort: EvaluationCohort | None = None,
) -> dict[str, Any]:
    """Evaluate all 2,162 cells without writing downstream artifacts."""

    cases = load_agentdojo_cases(evaluation_cohort)
    grader_contracts = build_grader_contracts()
    contracts = {
        (row["suite"], row["injection_task_id"]): row
        for row in grader_contracts
    }
    runtimes, promise_manifest = register_shipped_promises()
    case_rows: list[dict[str, Any]] = []
    call_rows: list[dict[str, Any]] = []
    verdict_rows: list[dict[str, Any]] = []
    for case in cases:
        case_row, case_calls, case_verdicts = _evaluate_case(
            case,
            contracts=contracts,
            runtimes=runtimes,
            promise_manifest=promise_manifest,
        )
        case_rows.append(case_row)
        call_rows.extend(case_calls)
        verdict_rows.extend(case_verdicts)
    return {
        "case_rows": case_rows,
        "call_rows": call_rows,
        "verdict_rows": verdict_rows,
        "grader_contracts": grader_contracts,
        "promise_manifest": promise_manifest,
        "summary": summarize_case_rows(case_rows),
    }
