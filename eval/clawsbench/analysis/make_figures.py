#!/usr/bin/env python3
"""Recompute the frozen ClawsBench attribution reference and figures."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


HERE = Path(__file__).resolve().parent
EVAL_ROOT = HERE.parents[1]
MISS_CLASSES = {
    "no_relevant_action", "all_candidates_compliant", "unsupported_evidence",
    "detector_miss", "outside_registered_surface", "competence_or_underaction",
}
REVIEWED_MISS_CLASSES = MISS_CLASSES | {"verifier_mismatch", "verifier_false_failure"}
VERDICT_QUALITY = {"supported", "verifier_mismatch", "unclear"}
OUTCOME_SUPPORT = {"supported_nonfull", "false_failure", "unclear", "not_applicable"}
VALIDITY = {"confirmed_violation", "overfire", "unclear", "under_verification"}
RELATION = {"same_reason", "same_action_different_rule", "different_failure", "ungraded", "unclear"}
CANDIDATE_STATUSES = ("passed", "fired", "rejected", "unsupported")
FIRE_VALIDITIES = ("confirmed_violation", "overfire", "unclear", "under_verification")
FULL_REWARD_DISPOSITIONS = (
    "benchmark_oversight", "under_verification", "overfire", "unclear",
)


def _count(items: list[dict[str, Any]], field: str, values: tuple[str, ...]) -> dict[str, int]:
    return {value: sum(item.get(field) == value for item in items) for value in values}


def _primary_miss(review: dict[str, Any]) -> str:
    return review.get("primary_miss", review["miss"])


def _md(value: Any) -> str:
    """Return one deterministic, table-safe Markdown cell."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _endpoint(value: dict[str, Any] | None) -> str:
    value = value or {}
    return " ".join(part for part in (value.get("method"), value.get("path")) if part)


def _action_count(candidates: list[dict[str, Any]]) -> int:
    return len({candidate["action_id"] for candidate in candidates})


def _review(coverage: list[dict[str, Any]], attribution: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    coverage_tasks = [row.get("task") for row in coverage]
    if (
        any(not isinstance(task, str) or not task for task in coverage_tasks)
        or len(coverage_tasks) != len(set(coverage_tasks))
    ):
        raise ValueError("frozen coverage must contain every task exactly once")
    task_rows = {row["task"]: row for row in coverage}
    reviews = attribution.get("tasks")
    review_tasks = [item.get("task") for item in reviews] if isinstance(reviews, list) else []
    if (
        not isinstance(reviews, list)
        or len(review_tasks) != len(task_rows)
        or len(review_tasks) != len(set(review_tasks))
        or set(review_tasks) != set(task_rows)
    ):
        raise ValueError("attribution must review every frozen coverage task exactly once")
    review_by_task = {item["task"]: item for item in reviews}
    for task, review in review_by_task.items():
        if review.get("reward") != task_rows[task]["reward"]:
            raise ValueError(f"{task}: reviewed reward differs from coverage")
        if not isinstance(review.get("benchmark_reason"), str) or not review["benchmark_reason"]:
            raise ValueError(f"{task}: missing benchmark reason")
        if not review.get("evidence"):
            raise ValueError(f"{task}: missing evidence reference")
        if review.get("verdict_quality") not in VERDICT_QUALITY:
            raise ValueError(f"{task}: invalid verdict quality")
        if review.get("outcome_support") not in OUTCOME_SUPPORT:
            raise ValueError(f"{task}: invalid outcome support")
        if task_rows[task]["reward_band"] != "full":
            if review.get("miss") not in REVIEWED_MISS_CLASSES:
                raise ValueError(f"{task}: invalid reviewed miss class")
            if _primary_miss(review) not in MISS_CLASSES:
                raise ValueError(f"{task}: missing primary miss class")
        if task_rows[task]["reward_band"] == "full" and review["outcome_support"] != "not_applicable":
            raise ValueError(f"{task}: full reward must be not_applicable")

    safety_ids = {fire["fire_id"] for row in coverage for fire in row["safety_fires"]}
    frozen_rows = [
        (row["task"], fire) for row in coverage for fire in row["fires"]
    ]
    frozen_ids = [fire["fire_id"] for _, fire in frozen_rows]
    if len(frozen_ids) != len(set(frozen_ids)):
        raise ValueError("frozen fire IDs must occur exactly once")
    frozen = {fire["fire_id"]: (task, fire) for task, fire in frozen_rows}
    expanded: list[dict[str, Any]] = []
    for group in attribution.get("fire_groups", []):
        for key, allowed in (("validity", VALIDITY), ("relation", RELATION)):
            if group.get(key) not in allowed:
                raise ValueError(f"invalid fire group {key}")
        if group.get("literal_match") is not True:
            raise ValueError("every fire group must record literal_match=true")
        selected = [
            fire for task, fire in frozen.values()
            if task == group.get("task") and fire["arm"] == group.get("arm")
        ]
        if len(selected) != group.get("expected_count"):
            raise ValueError(f"{group.get('task')} {group.get('arm')}: expected count mismatch")
        for fire in selected:
            expanded.append({
                **fire,
                "task": group["task"],
                "promise_group": "safety" if fire["fire_id"] in safety_ids else "aap5",
                "literal_match": True,
                "validity": group["validity"],
                "relation": group["relation"],
                "review_note": group.get("note", ""),
            })
    ids = [fire["fire_id"] for fire in expanded]
    if len(ids) != len(set(ids)) or set(ids) != set(frozen):
        raise ValueError("reviewed fire IDs must equal frozen fire IDs exactly once")
    return review_by_task, sorted(expanded, key=lambda fire: fire["fire_id"])


def _task_overlap(rows: list[dict[str, Any]], review_by_task: dict[str, dict[str, Any]]) -> dict[str, Any]:
    nonfull = [row for row in rows if row["reward_band"] != "full"]
    safety = sum(bool(row["safety_fires"]) for row in nonfull)
    aap5 = sum(bool(row["operational_fires"]) for row in nonfull)
    union = sum(bool(row["fires"]) for row in nonfull)
    supported = [row for row in nonfull if review_by_task[row["task"]]["verdict_quality"] == "supported"]
    return {
        "nonfull_count": len(nonfull),
        "literal_nonfull_overlap": {"safety": safety, "aap5": aap5, "union": union},
        "supported_nonfull_count": len(supported),
        "supported_literal_nonfull_overlap": {
            "safety": sum(bool(row["safety_fires"]) for row in supported),
            "aap5": sum(bool(row["operational_fires"]) for row in supported),
            "union": sum(bool(row["fires"]) for row in supported),
        },
    }


def _raw_to_aligned(
    rows: list[dict[str, Any]], fires: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fires_by_task: dict[str, list[dict[str, Any]]] = {}
    for fire in fires:
        fires_by_task.setdefault(fire["task"], []).append(fire)
    composition = []
    for row in rows:
        if row["reward_band"] == "full" or not row["fires"]:
            continue
        task_fires = fires_by_task[row["task"]]
        valid = any(fire["validity"] == "confirmed_violation" for fire in task_fires)
        confirmed_safety = any(
            fire["validity"] == "confirmed_violation"
            and fire["promise_group"] == "safety"
            for fire in task_fires
        )
        aligned = any(
            fire["validity"] == "confirmed_violation"
            and fire["relation"] == "same_reason"
            and fire["promise_group"] == "safety"
            for fire in task_fires
        )
        if aligned:
            gap = "aligned"
        elif not valid:
            gap = "no confirmed violation"
        elif not confirmed_safety:
            gap = "confirmed operational fire only"
        else:
            gap = "confirmed safety fire has a different benchmark reason"
        composition.append({
            "task": row["task"],
            "family": row["family"],
            "safety_fires": len(row["safety_fires"]),
            "aap5_fires": len(row["operational_fires"]),
            "fires": len(row["fires"]),
            "validities": ", ".join(sorted({fire["validity"] for fire in task_fires})),
            "relations": ", ".join(sorted({fire["relation"] for fire in task_fires})),
            "valid": valid,
            "aligned": aligned,
            "gap": gap,
        })
    return composition


def _full_reward_fires(
    rows: list[dict[str, Any]], fires: list[dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    full_tasks = {row["task"] for row in rows if row["reward_band"] == "full"}
    disposition = {
        "confirmed_violation": "benchmark_oversight",
        "under_verification": "under_verification",
        "overfire": "overfire",
        "unclear": "unclear",
    }
    selected = [fire for fire in fires if fire["task"] in full_tasks]
    counts = {
        label: sum(disposition[fire["validity"]] == label for fire in selected)
        for label in FULL_REWARD_DISPOSITIONS
    }
    grouped = Counter(
        (fire["task"], fire["arm"], disposition[fire["validity"]])
        for fire in selected
    )
    named_rows = [
        {"task": task, "arm": arm, "disposition": label, "fires": count}
        for (task, arm, label), count in sorted(grouped.items())
    ]
    return counts, named_rows


def _family_counts(
    rows: list[dict[str, Any]],
    fires: list[dict[str, Any]],
    raw_to_aligned: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    overlap_tasks = {row["task"] for row in raw_to_aligned}
    valid_tasks = {row["task"] for row in raw_to_aligned if row["valid"]}
    aligned_tasks = {row["task"] for row in raw_to_aligned if row["aligned"]}
    fires_by_task = Counter(fire["task"] for fire in fires)
    result = {}
    for family in sorted({row["family"] for row in rows}):
        family_rows = [row for row in rows if row["family"] == family]
        family_candidates = [
            candidate for row in family_rows for candidate in row["candidates"]
        ]
        family_tasks = {row["task"] for row in family_rows}
        result[family] = {
            "tasks": len(family_rows),
            "nonfull": sum(row["reward_band"] != "full" for row in family_rows),
            "candidates": len(family_candidates),
            "actions": _action_count(family_candidates),
            "fires": sum(fires_by_task[task] for task in family_tasks),
            "overlap": len(family_tasks & overlap_tasks),
            "valid": len(family_tasks & valid_tasks),
            "aligned": len(family_tasks & aligned_tasks),
        }
    return result


def _markdown(summary: dict[str, Any], rows: list[dict[str, Any]], reviews: dict[str, dict[str, Any]], fires: list[dict[str, Any]]) -> str:
    denominator = summary["nonfull_count"]
    overlap = summary["literal_nonfull_overlap"]
    lines = [
        "# ClawsBench post-freeze attribution — reference",
        "",
        "This reference reads frozen `coverage.json` and the separately reviewed attribution record. It never changes a candidate or fire.",
        "",
        "## Sealed denominator and reconciliation",
        "",
        f"Sealed denominator: {denominator} non-full tasks. Literal overlap is safety {overlap['safety']}/{denominator}, AAP-5 {overlap['aap5']}/{denominator}, and union {overlap['union']}/{denominator}.",
        f"Substantively valid promise overlap is {summary['valid_nonfull_count']}/{denominator}; confirmed same-reason safety alignment is {summary['aligned_nonfull_count']}/{denominator}.",
        f"Frozen candidates: {summary['candidate_status_counts']['passed']} passed, {summary['candidate_status_counts']['fired']} fired, {summary['candidate_status_counts']['rejected']} rejected, and {summary['candidate_status_counts']['unsupported']} unsupported. Exact fires: {summary['fire_count']}.",
        f"Candidate accounting records {summary['candidate_status_counts']['fired']} fired candidate states and {summary['fire_count']} exact boundary fires. They differ because AAP-5 keeps later matching requests in the fired state after a signature crosses its cap, while only the first crossing emits a boundary fire.",
        "",
        "## Aggregate counts",
        "",
        f"Tasks: {summary['task_count']}. Actions: {summary['action_count']:,}. Safety fires: {summary['safety_fire_count']}. AAP-5 fires: {summary['aap5_fire_count']}.",
        "This is the scored agent-phase action count, not the raw action-log count.",
        "",
        "| candidate accounting | count |",
        "| --- | ---: |",
        f"| passed | {summary['candidate_status_counts']['passed']} |",
        f"| fired | {summary['candidate_status_counts']['fired']} |",
        f"| evaluated subtotal | {summary['evaluated_candidate_count']} |",
        f"| rejected | {summary['candidate_status_counts']['rejected']} |",
        f"| unsupported | {summary['candidate_status_counts']['unsupported']} |",
        f"| candidate total | {summary['candidate_count']} |",
        "",
        "Evaluated is the passed-plus-fired subtotal. Rejected and unsupported candidates are outside that subtotal.",
    ]
    lines += ["", "| fire validity | exact fires |", "| --- | ---: |"]
    for label, value in summary["valid_fire_counts"].items():
        lines.append(f"| {label} | {value} |")
    lines += ["", "| arm | candidates | exact fires |", "| --- | ---: | ---: |"]
    for arm, value in summary["arm_candidate_counts"].items():
        lines.append(f"| {arm} | {value} | {summary['arm_fire_counts'].get(arm, 0)} |")
    lines += [
        "",
        "Family overlap is the number of non-full tasks with any literal fire. Valid counts tasks with a confirmed violation. Aligned requires a confirmed same-reason safety fire. Actions are unique scored agent-phase action IDs.",
        "",
        "| family | tasks | nonfull | candidates | actions | fires | overlap | valid | aligned |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for family, value in summary["family_counts"].items():
        lines.append(
            f"| {family} | {value['tasks']} | {value['nonfull']} | {value['candidates']} | "
            f"{value['actions']} | {value['fires']} | {value['overlap']} | {value['valid']} | {value['aligned']} |"
        )
    lines += ["", "| reward band | tasks |", "| --- | ---: |"]
    for band, value in summary["reward_band_counts"].items():
        lines.append(f"| {band} | {value} |")
    lines += ["", "| reviewed miss class | non-full tasks |", "| --- | ---: |"]
    for label, value in summary["miss_counts"].items():
        lines.append(f"| {label} | {value} |")
    lines += ["", "| primary promise miss class | non-full tasks |", "| --- | ---: |"]
    for label, value in summary["primary_miss_counts"].items():
        lines.append(f"| {label} | {value} |")
    lines += [
        "",
        "| verdict quality | non-full tasks |",
        "| --- | ---: |",
    ]
    for label, value in summary["nonfull_verdict_quality_counts"].items():
        lines.append(f"| {label} | {value} |")
    supported = summary["supported_nonfull_count"]
    supported_overlap = summary["supported_literal_nonfull_overlap"]
    lines += [
        "",
        f"Supported-verdict sensitivity keeps {supported} non-full tasks: safety {supported_overlap['safety']}/{supported}, AAP-5 {supported_overlap['aap5']}/{supported}, union {supported_overlap['union']}/{supported}.",
        "",
        "| outcome support | tasks |",
        "| --- | ---: |",
    ]
    for label, value in summary["outcome_support_counts"].items():
        lines.append(f"| {label} | {value} |")

    lines += [
        "",
        "## Raw-to-aligned composition",
        "",
        "Each named row has literal overlap on a non-full task. Valid requires at least one confirmed violation. Aligned additionally requires that confirmed violation to be a same-reason safety fire.",
        "",
        "| task | family | safety fires | AAP-5 fires | exact fires | validity | relation | valid | aligned | gap |",
        "| --- | --- | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in summary["raw_to_aligned_rows"]:
        lines.append(
            f"| {_md(row['task'])} | {_md(row['family'])} | {row['safety_fires']} | {row['aap5_fires']} | "
            f"{row['fires']} | {_md(row['validities'])} | {_md(row['relations'])} | {_md(row['valid'])} | "
            f"{_md(row['aligned'])} | {_md(row['gap'])} |"
        )

    lines += [
        "",
        "## Full-reward fires",
        "",
        "A confirmed violation on a full-reward task is a benchmark oversight. The other dispositions follow the reviewed fire-validity label.",
        "",
        "| disposition | exact fires |",
        "| --- | ---: |",
    ]
    for label, value in summary["full_reward_fire_disposition"].items():
        lines.append(f"| {label} | {value} |")
    lines += [
        "",
        "| task | arm | disposition | exact fires |",
        "| --- | --- | --- | ---: |",
    ]
    for row in summary["full_reward_fire_rows"]:
        lines.append(
            f"| {_md(row['task'])} | {_md(row['arm'])} | {_md(row['disposition'])} | {row['fires']} |"
        )

    lines += [
        "",
        "## Complete task table",
        "",
        "Actions are unique scored agent-phase action IDs. Evidence paths identify the recorded verifier reward artifact or a reviewed recovery artifact; all are read-only post-freeze evidence.",
        "",
        "| task | family | reward | band | actions | exact fires | safety / AAP-5 / union | candidates P/F/R/U | verdict quality | outcome support | reviewed miss / primary miss | benchmark reason | evidence |",
        "| --- | --- | ---: | --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        review = reviews[row["task"]]
        candidate = _count(row["candidates"], "status", CANDIDATE_STATUSES)
        reviewed_miss = review.get("miss", "—")
        primary_miss = _primary_miss(review) if row["reward_band"] != "full" else "—"
        lines.append("| {task} | {family} | {reward:g} | {band} | {actions} | {fire_count} | {safety}/{aap5}/{union} | {passed}/{fired}/{rejected}/{unsupported} | {quality} | {outcome} | {miss} | {reason} | {evidence} |".format(
            task=_md(row["task"]), family=_md(row["family"]), reward=row["reward"], band=_md(row["reward_band"]),
            actions=summary["task_action_counts"][row["task"]], fire_count=len(row["fires"]),
            safety=row["kind"], aap5=row["operational_kind"], union=row["union_kind"],
            quality=_md(review["verdict_quality"]), outcome=_md(review["outcome_support"]),
            miss=f"{_md(reviewed_miss)} / {_md(primary_miss)}",
            reason=_md(review["benchmark_reason"]), evidence=_md("; ".join(review["evidence"])), **candidate,
        ))
    lines += [
        "",
        "## Complete fire table",
        "",
        "Literal match reports whether frozen code fired. Validity and relation are post-freeze review labels.",
        "",
        "| fire id | candidate id | action id | seq | service | endpoint | targets | detail | task | arm | promise | literal | validity | relation | frozen evidence | review note |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for fire in fires:
        evidence = fire.get("evidence", {}).get("artifact", "")
        targets = "; ".join(_md(target) for target in fire.get("targets", [])) or "—"
        lines.append(
            f"| {_md(fire['fire_id'])} | {_md(fire['candidate_id'])} | {_md(fire['action_id'])} | {fire['seq']} | "
            f"{_md(fire.get('service'))} | {_md(_endpoint(fire.get('endpoint')))} | {targets} | {_md(fire.get('detail'))} | "
            f"{_md(fire['task'])} | {_md(fire['arm'])} | {_md(fire['promise_group'])} | true | {_md(fire['validity'])} | "
            f"{_md(fire['relation'])} | {_md(evidence)} | {_md(fire['review_note'])} |"
        )

    nonpass = [
        {**candidate, "task": row["task"]}
        for row in rows
        for candidate in row["candidates"]
        if candidate["status"] != "passed"
    ]
    nonpass.sort(key=lambda candidate: (
        candidate["task"], candidate["seq"], candidate["arm"], candidate["candidate_id"],
    ))
    lines += [
        "",
        "## Complete non-pass candidate table",
        "",
        f"This table contains all {summary['candidate_status_counts']['fired']} fired states, {summary['candidate_status_counts']['rejected']} rejected states, and {summary['candidate_status_counts']['unsupported']} unsupported states. Passed candidate rows remain in `coverage.json`, the source for passed rows.",
        "",
        "| candidate id | action id | task | seq | arm | status | emitted fire | promise | service | endpoint | target | detail | evidence |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for candidate in nonpass:
        raw_action = candidate.get("raw_action") or {}
        evidence = (candidate.get("evidence") or {}).get("artifact", "")
        endpoint = {"method": raw_action.get("method"), "path": raw_action.get("path")}
        lines.append(
            f"| {_md(candidate['candidate_id'])} | {_md(candidate['action_id'])} | {_md(candidate['task'])} | {candidate['seq']} | "
            f"{_md(candidate['arm'])} | {_md(candidate['status'])} | {_md(candidate.get('emitted_fire', False))} | "
            f"{_md(candidate['promise_group'])} | {_md(raw_action.get('service'))} | {_md(_endpoint(endpoint))} | "
            f"{_md(candidate.get('target'))} | {_md(candidate.get('detail'))} | {_md(evidence)} |"
        )
    return "\n".join(lines) + "\n"


def _figures(summary: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "figure.dpi": 120})
    denominator = summary["nonfull_count"]
    overlap = summary["literal_nonfull_overlap"]
    fig, ax = plt.subplots(figsize=(5.5, 2.5))
    labels = ["Safety literal", "AAP-5 literal", "Union literal", "Valid", "Aligned"]
    values = [overlap["safety"], overlap["aap5"], overlap["union"], summary["valid_nonfull_count"], summary["aligned_nonfull_count"]]
    ax.barh(labels, values, color=["#bd6b57", "#4f7cac", "#7f9c75", "#3d8b5f", "#a7adb3"])
    ax.set_xlim(0, max(1, denominator * 1.15)); ax.set_xlabel(f"non-full tasks (sealed denominator = {denominator})")
    for i, value in enumerate(values): ax.text(value + .4, i, f"{value}/{denominator}", va="center")
    ax.invert_yaxis(); fig.tight_layout(); fig.savefig(output / "fig1_overlap.png", metadata={"Software": "ClawsBench attribution"}); plt.close(fig)
    fig, ax = plt.subplots(figsize=(5.5, 2.5))
    counts = summary["valid_fire_counts"]
    labels, values = list(counts), list(counts.values())
    ax.bar(labels, values, color=["#3d8b5f", "#bd6b57", "#a7adb3", "#d5a34e"])
    ax.set_ylabel("exact fires"); ax.set_title("Exact fire validity")
    for i, value in enumerate(values): ax.text(i, value + 4, str(value), ha="center")
    fig.tight_layout(); fig.savefig(output / "fig2_fire_validity.png", metadata={"Software": "ClawsBench attribution"}); plt.close(fig)
    fig, ax = plt.subplots(figsize=(5.5, 2.5))
    counts = summary["primary_miss_counts"]
    labels, values = list(counts), list(counts.values())
    ax.barh(labels, values, color="#4f7cac")
    ax.set_xlabel("non-full tasks"); ax.invert_yaxis(); ax.set_title("Primary promise miss class")
    for i, value in enumerate(values): ax.text(value + .15, i, str(value), va="center")
    fig.tight_layout(); fig.savefig(output / "fig3_miss_classes.png", metadata={"Software": "ClawsBench attribution"}); plt.close(fig)


def build(coverage: list[dict[str, Any]], attribution: dict[str, Any], output: Path) -> dict[str, Any]:
    rows = sorted(coverage, key=lambda row: row["task"])
    reviews, fires = _review(rows, attribution)
    task_summary = _task_overlap(rows, reviews)
    nonfull = [row for row in rows if row["reward_band"] != "full"]
    nonfull_tasks = {row["task"] for row in nonfull}
    valid_nonfull = {fire["task"] for fire in fires if fire["validity"] == "confirmed_violation" and fire["task"] in nonfull_tasks}
    aligned_nonfull = {
        fire["task"] for fire in fires
        if fire["validity"] == "confirmed_violation"
        and fire["relation"] == "same_reason"
        and fire["promise_group"] == "safety"
        and fire["task"] in nonfull_tasks
    }
    candidates = [candidate for row in rows for candidate in row["candidates"]]
    candidate_status_counts = _count(candidates, "status", CANDIDATE_STATUSES)
    reviewed_miss_counts = _count(
        [reviews[row["task"]] for row in nonfull],
        "miss",
        tuple(sorted(REVIEWED_MISS_CLASSES)),
    )
    primary_miss_counts = _count(
        [{"primary_miss": _primary_miss(reviews[row["task"]])} for row in nonfull],
        "primary_miss",
        tuple(sorted(MISS_CLASSES)),
    )
    raw_to_aligned = _raw_to_aligned(rows, fires)
    full_reward_disposition, full_reward_fire_rows = _full_reward_fires(rows, fires)
    family_counts = _family_counts(rows, fires, raw_to_aligned)
    safety_fire_count = sum(len(row["safety_fires"]) for row in rows)
    summary = {
        "task_count": len(rows), "fire_count": len(fires), "reviewed_fire_ids": [fire["fire_id"] for fire in fires],
        **task_summary,
        "valid_nonfull_count": len(valid_nonfull), "aligned_nonfull_count": len(aligned_nonfull),
        "candidate_count": len(candidates),
        "candidate_status_counts": candidate_status_counts,
        "evaluated_candidate_count": candidate_status_counts["passed"] + candidate_status_counts["fired"],
        "nonpass_candidate_count": len(candidates) - candidate_status_counts["passed"],
        "action_count": _action_count(candidates),
        "task_action_counts": {
            row["task"]: _action_count(row["candidates"]) for row in rows
        },
        "safety_fire_count": safety_fire_count,
        "aap5_fire_count": len(fires) - safety_fire_count,
        "valid_fire_counts": _count(fires, "validity", FIRE_VALIDITIES),
        "full_reward_fire_disposition": full_reward_disposition,
        "full_reward_fire_rows": full_reward_fire_rows,
        "raw_to_aligned_rows": raw_to_aligned,
        "nonfull_verdict_quality_counts": _count([reviews[row["task"]] for row in nonfull], "verdict_quality", ("supported", "verifier_mismatch", "unclear")),
        "outcome_support_counts": _count(list(reviews.values()), "outcome_support", ("supported_nonfull", "false_failure", "unclear", "not_applicable")),
        "miss_counts": reviewed_miss_counts,
        "reviewed_miss_counts": reviewed_miss_counts,
        "primary_miss_counts": primary_miss_counts,
        "family_counts": family_counts,
        "family_task_counts": dict(sorted(Counter(row["family"] for row in rows).items())),
        "reward_band_counts": _count(rows, "reward_band", ("negative", "zero", "partial", "full")),
        "arm_candidate_counts": dict(sorted(Counter(candidate["arm"] for candidate in candidates).items())),
        "arm_fire_counts": dict(sorted(Counter(fire["arm"] for fire in fires).items())),
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "clawsbench_reference.md").write_text(_markdown(summary, rows, reviews, fires))
    _figures(summary, output / "figures")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=EVAL_ROOT / "writeups/clawsbench")
    args = parser.parse_args()
    coverage = json.loads((HERE / "coverage.json").read_text())
    attribution = json.loads((HERE / "attribution.json").read_text())
    summary = build(coverage, attribution, args.output)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
