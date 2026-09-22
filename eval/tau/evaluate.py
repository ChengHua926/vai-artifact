"""End-to-end Tau replay, effect, monitor-fire, and summary ledgers."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from typing import Any

from eval.dataset import EvaluationCohort

from .contracts import effect_contract_manifest, exact_links_for_fire
from .corpus import load_tau_cases
from .effects import value_at
from .monitor import compiled_monitor_manifest, run_compiled_monitor
from .replay import replay_case


REPO_ROOT = Path(__file__).resolve().parents[2]


def _counts(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _case_identity(case: dict[str, Any]) -> dict[str, Any]:
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
            "domain",
            "task_id",
            "trial",
            "simulation_id",
            "termination_reason",
            "native_reward",
            "recorded_db_match",
            "source",
        )
    }


def _with_case(row: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case["case_id"],
        "case_key": case["case_key"],
        "model_id": case["model_id"],
        "domain": case["domain"],
        "task_id": case["task_id"],
        "trial": case["trial"],
        **row,
    }


def _legacy_monitor_evidence(
    case: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Independently adapt raw messages through the old trace adapter for parity."""

    commons = REPO_ROOT / "packages" / "commons"
    commons_text = str(commons)
    if commons_text not in sys.path:
        sys.path.append(commons_text)
    from .trace_adapter import tau2_to_trace, tau2_turns

    legacy_trace = [record.to_dict() for record in tau2_to_trace(case["simulation"])]
    legacy_turns = tau2_turns(case["simulation"])
    legacy_fires = run_compiled_monitor(case["domain"], legacy_trace, legacy_turns)["fires"]
    return legacy_trace, legacy_turns, legacy_fires


def _monitor_parity_problem(
    case: dict[str, Any], monitor_result: dict[str, Any]
) -> dict[str, Any] | None:
    legacy_trace, legacy_turns, legacy_fires = _legacy_monitor_evidence(case)
    current_core = [
        {key: call.get(key) for key in ("seq", "tool", "args", "result")}
        for call in case["trace"]
    ]
    legacy_core = [
        {key: call.get(key) for key in ("seq", "tool", "args", "result")}
        for call in legacy_trace
    ]
    reasons = []
    if current_core != legacy_core:
        reasons.append("trace_adapter_mismatch")
    if case["turns"] != legacy_turns:
        reasons.append("turn_adapter_mismatch")
    if monitor_result["fires"] != legacy_fires:
        reasons.append("fire_mismatch")
    if not reasons:
        return None
    return {"case_id": case["case_id"], "reasons": reasons}


def _change_persists(call: dict[str, Any], actual_final: dict[str, Any]) -> bool:
    for change in call.get("changes") or []:
        final_present, final = value_at(actual_final, change["path"])
        if final_present == change["after_present"] and final == change["after"]:
            return True
    return False


def _pass_fire_reason(call: dict[str, Any] | None, replay: dict[str, Any]) -> str:
    if (
        call is None
        or not call.get("mutating")
        or not call.get("accepted")
        or not call.get("changes")
    ):
        return "nonmutating_rejected_or_noop"
    if not _change_persists(call, replay["actual_state"]):
        return "transient_or_overwritten"
    return "persistent_gold_consistent"


def _no_fire_reason(
    replay: dict[str, Any], supplied_checks_by_seq: dict[str, list[str]]
) -> str | None:
    if replay["effect_shape"] == "response_only":
        return "response_only"
    writers = sorted(
        {
            effect["writer_seq"]
            for effect in replay["terminal_effects"]
            if effect.get("writer_seq") is not None
        }
    )
    if not writers:
        return "omission_no_writer"
    surface = [bool(supplied_checks_by_seq.get(str(seq))) for seq in writers]
    if all(not value for value in surface):
        return "no_applicable_check_on_wrong_writer"
    if all(surface):
        return "applicable_checks_passed_on_wrong_writer"
    return "mixed_monitor_surface_on_wrong_writers"


def classify_failure_relation(*, any_fire: bool, relations: set[str]) -> str:
    """Collapse per-fire causal relations without inventing a writer for language failures."""

    if not any_fire:
        return "no_fire"
    if "exact_protected_effect" in relations:
        return "exact_protected_effect"
    if "same_writer_unproven" in relations:
        return "same_writer_unproven"
    if "unknown" in relations:
        return "unknown"
    if relations == {"no_persistent_wrong_effect"}:
        return "no_persistent_wrong_effect"
    return "different_writer"


def _evaluate_case(
    case: dict[str, Any], *, tau_root: Path
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    monitor = run_compiled_monitor(case["domain"], case["trace"], case["turns"])
    parity_problem = _monitor_parity_problem(case, monitor)
    replay = replay_case(case, tau_root=tau_root)
    calls_by_seq = {call["seq"]: call for call in replay["calls"]}
    writer_seqs = {
        effect["writer_seq"]
        for effect in replay["terminal_effects"]
        if effect.get("writer_seq") is not None
    }

    fire_rows: list[dict[str, Any]] = []
    pass_reasons: set[str] = set()
    exact_links: list[dict[str, Any]] = []
    for fire in monitor["fires"]:
        call = calls_by_seq.get(fire.get("seq"))
        links = (
            []
            if replay["replay_errors"]
            else exact_links_for_fire(
                fire,
                call=call,
                terminal_effects=replay["terminal_effects"],
                actual_final=replay["actual_state"],
                gold_final=replay["gold_state"],
            )
        )
        exact_links.extend(links)
        if replay["replay_errors"]:
            relation = "unknown"
        elif not replay["terminal_effects"]:
            relation = "no_persistent_wrong_effect"
        elif links:
            relation = "exact_protected_effect"
        elif fire.get("seq") in writer_seqs:
            relation = "same_writer_unproven"
        else:
            relation = "different_writer"
        pass_reason = None
        if case["native_reward"] == 1.0:
            pass_reason = _pass_fire_reason(call, replay)
            pass_reasons.add(pass_reason)
        fire_rows.append(
            _with_case(
                {
                    **fire,
                    "implementation": "compiled_tau_monitor",
                    "writer_relation": relation,
                    "exact_links": links,
                    "gold_warnings": replay["gold_warnings"],
                    "pass_explanation": pass_reason,
                },
                case,
            )
        )

    any_fire = bool(monitor["fires"])
    relations = {row["writer_relation"] for row in fire_rows}
    if case["native_reward"] == 0.0:
        failure_relation = classify_failure_relation(
            any_fire=any_fire, relations=relations
        )
    else:
        failure_relation = None
    no_fire_reason = (
        _no_fire_reason(replay, monitor["supplied_checks_by_seq"])
        if case["native_reward"] == 0.0 and not any_fire and not replay["replay_errors"]
        else None
    )
    row = {
        **_case_identity(case),
        "computed_db_match": replay["computed_db_match"],
        "replay_status": "error" if replay["replay_errors"] else "ok",
        "replay_errors": replay["replay_errors"],
        "gold_warnings": replay["gold_warnings"],
        "effect_shape": replay["effect_shape"],
        "terminal_effect_count": len(replay["terminal_effects"]),
        "written_terminal_effect_count": sum(
            effect.get("writer_seq") is not None for effect in replay["terminal_effects"]
        ),
        "omitted_terminal_effect_count": sum(
            effect.get("writer_seq") is None for effect in replay["terminal_effects"]
        ),
        "monitor_implementation": "compiled_tau_monitor",
        "monitor_fire_count": len(monitor["fires"]),
        "any_fire": any_fire,
        "fire_arms": [str(fire.get("arm")) for fire in monitor["fires"]],
        "fire_writer_relations": _counts([str(row["writer_relation"]) for row in fire_rows]),
        "failure_relation": failure_relation,
        "exact_links": exact_links,
        "no_fire_reason": no_fire_reason,
        "pass_fire_reasons": sorted(pass_reasons),
        "monitor_supplied_checks_by_seq": monitor["supplied_checks_by_seq"],
        "monitor_rejected_seqs": monitor["rejected_seqs"],
        "monitor_parity": "mismatch" if parity_problem else "exact",
    }

    call_rows = []
    for call in replay["calls"]:
        portable = {
            key: value
            for key, value in call.items()
            if key not in {"before_state", "after_state"}
        }
        portable["monitor_supplied_arms"] = monitor["supplied_checks_by_seq"].get(
            str(call["seq"]), []
        )
        portable["terminal_writer"] = call["seq"] in writer_seqs
        call_rows.append(_with_case(portable, case))
    effect_rows = [_with_case(effect, case) for effect in replay["terminal_effects"]]
    return row, call_rows, effect_rows, fire_rows, parity_problem


def summarize_case_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure per-model fold over the Tau case ledger."""

    models: dict[str, Any] = {}
    for model_id in sorted({row["model_id"] for row in rows}):
        selected = [row for row in rows if row["model_id"] == model_id]
        failures = [row for row in selected if row["native_reward"] == 0.0]
        passes = [row for row in selected if row["native_reward"] == 1.0]
        failure_fires = [row for row in failures if row["any_fire"]]
        pass_fires = [row for row in passes if row["any_fire"]]
        same_writer = [
            row
            for row in failure_fires
            if row["failure_relation"] in {"exact_protected_effect", "same_writer_unproven"}
        ]
        models[model_id] = {
            "captured": len(selected),
            "scorable": sum(row["replay_status"] == "ok" for row in selected),
            "replay_error_runs": sum(row["replay_status"] == "error" for row in selected),
            "native_passes": len(passes),
            "native_failures": len(failures),
            "recorded_db_successes": sum(row["recorded_db_match"] for row in selected),
            "recorded_db_failures": sum(not row["recorded_db_match"] for row in selected),
            "computed_db_successes": sum(row["computed_db_match"] for row in selected),
            "computed_db_failures": sum(not row["computed_db_match"] for row in selected),
            "failure_effect_shapes": _counts([str(row["effect_shape"]) for row in failures]),
            "any_fire_runs": sum(row["any_fire"] for row in selected),
            "no_fire_runs": sum(not row["any_fire"] for row in selected),
            "failure_fire_runs": len(failure_fires),
            "failure_no_fire_runs": len(failures) - len(failure_fires),
            "pass_fire_runs": len(pass_fires),
            "pass_no_fire_runs": len(passes) - len(pass_fires),
            "failure_exact_protected_effect_runs": sum(
                row["failure_relation"] == "exact_protected_effect" for row in failures
            ),
            "failure_same_writer_runs": len(same_writer),
            "failure_same_writer_unproven_runs": sum(
                row["failure_relation"] == "same_writer_unproven" for row in failures
            ),
            "failure_different_writer_runs": sum(
                row["failure_relation"] == "different_writer" for row in failures
            ),
            "failure_no_persistent_wrong_effect_runs": sum(
                row["failure_relation"] == "no_persistent_wrong_effect"
                for row in failures
            ),
            "failure_relations": _counts([str(row["failure_relation"]) for row in failures]),
            "failure_no_fire_reasons": _counts(
                [str(row["no_fire_reason"]) for row in failures if not row["any_fire"]]
            ),
            "pass_fire_reason_sets": _counts(
                ["+".join(row["pass_fire_reasons"]) for row in pass_fires]
            ),
            "fire_arms": _counts(
                [arm for row in selected for arm in row["fire_arms"]]
            ),
            "gold_warning_runs": sum(bool(row["gold_warnings"]) for row in selected),
            "gold_warning_count": sum(len(row["gold_warnings"]) for row in selected),
            "monitor_parity_mismatches": sum(
                row["monitor_parity"] != "exact" for row in selected
            ),
        }
    return {"schema_version": 1, "benchmark": "tau", "models": models}


def evaluate_tau(
    evaluation_cohort: EvaluationCohort | None = None,
    *,
    tau_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate all 328 sealed Tau simulations without writing downstream artifacts."""

    root = Path(tau_root) if tau_root is not None else REPO_ROOT.parent / "tau2-explore"
    case_rows: list[dict[str, Any]] = []
    call_rows: list[dict[str, Any]] = []
    effect_rows: list[dict[str, Any]] = []
    fire_rows: list[dict[str, Any]] = []
    parity_mismatches: list[dict[str, Any]] = []
    for case in load_tau_cases(evaluation_cohort):
        case_row, calls, effects, fires, parity_problem = _evaluate_case(
            case, tau_root=root
        )
        case_rows.append(case_row)
        call_rows.extend(calls)
        effect_rows.extend(effects)
        fire_rows.extend(fires)
        if parity_problem:
            parity_mismatches.append(parity_problem)
    return {
        "case_rows": case_rows,
        "call_rows": call_rows,
        "effect_rows": effect_rows,
        "fire_rows": fire_rows,
        "monitor_manifest": compiled_monitor_manifest(),
        "effect_contracts": effect_contract_manifest(),
        "monitor_parity_mismatches": parity_mismatches,
        "summary": summarize_case_rows(case_rows),
    }
