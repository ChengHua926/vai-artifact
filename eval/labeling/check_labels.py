"""Recompute the released final labels from the released model votes, offline.

Reads the annotations release (votes, labels, traces) from the path given and
applies the committee decision of `extension_labels` with the frozen weights
in `weights.json`. Exits non-zero, naming every run that differs, unless:

- every extension run (298 tau3-bench, 30 ClawsBench) matches the released
  `votes`, `majority`, `posterior` and `label` exactly;
- every run labeled `violation` carries exactly the rules and citations of
  the models voting `violation`, and every clean run carries none.

The 60 calibration runs are labeled by the five-labeler majority (two human
annotators and the three models); the human votes are not released, so those
labels are counted but not recomputed.

  python -m eval.labeling.check_labels --annotations <path to the annotations release>
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from eval.labeling import extension_labels as el

# The evaluation drops the tau3-bench tool-call protocol sentences ("one tool
# call at a time", "no tool call while responding") from the cited rules it
# scores, so the released `rules` and `citations` omit them.
PROTOCOL_RULES = frozenset({"airline-p05-s06", "retail-p08-s12", "retail-p08-s13"})
CITATION_FIELDS = ("model_short", "rule_id", "quote", "rationale", "confidence", "step_indexes")


def read_labels(bench_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(bench_dir.glob("labels*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return rows


def check_bench(annotations: Path, bench: str) -> tuple[list[str], list[str]]:
    """(report lines, mismatch lines) for one benchmark."""

    bench_dir = annotations / bench
    matrices, prior, threshold = el.load_weights(bench)
    votes_by_model = el.read_votes(bench_dir / "votes")
    labels = read_labels(bench_dir)
    report: list[str] = []
    bad: list[str] = []

    extension = [row for row in labels if row["set"] == "extension"]
    calibration = [row for row in labels if row["set"] == "calibration"]
    overrides, override_changed, matched = [], [], 0
    for row in extension:
        run = row["run"]
        votes = el.task_votes(votes_by_model, run)
        manifest = json.loads((bench_dir / "traces" / f"{run}.json").read_text()) if bench == "clawsbench" else None
        decided = el.decide(bench, votes, matrices, prior, threshold, manifest)
        if decided["override"]:
            overrides.append(run)
            if decided["posterior_label"] != decided["label"]:
                override_changed.append(run)
        expected = {"votes": votes, "majority": decided["majority"], "posterior": decided["posterior"], "label": decided["label"]}
        diffs = [f"{field}: released {row.get(field)!r}, recomputed {value!r}" for field, value in expected.items() if row.get(field) != value]
        if diffs:
            bad.append(f"{bench} {run}: " + "; ".join(diffs))
        else:
            matched += 1
    counts = Counter(row["label"] for row in extension)
    report.append(
        f"{bench}: extension runs: votes, majority, posterior, label match on {matched}/{len(extension)} "
        f"(violation {counts['violation']}, no_violation {counts['no_violation']})"
    )
    if bench == "clawsbench":
        report.append(
            f"{bench}: landed-only override fired on {len(overrides)} ({', '.join(overrides) or 'none'}); "
            f"labels it changed: {len(override_changed)}"
        )
    report.append(
        f"{bench}: calibration runs: {len(calibration)} labels are the five-labeler majority "
        "(human votes not released); not recomputed"
    )

    rules_ok = {"violation": 0, "no_violation": 0}
    for row in labels:
        run, label = row["run"], row["label"]
        if label == el.VIOLATION:
            votes = el.task_votes(votes_by_model, run)
            rules, citations = el.cited_rules(votes_by_model, votes, run)
            expected_rules = {rule: models for rule, models in rules.items() if rule not in PROTOCOL_RULES}
            expected_citations = [
                tuple(json.dumps(c[field], sort_keys=True) for field in CITATION_FIELDS)
                for c in citations
                if c["rule_id"] not in PROTOCOL_RULES
            ]
            released_rules = {entry["rule_id"]: entry["models"] for entry in row["rules"]}
            released_citations = [
                tuple(json.dumps(c.get(field), sort_keys=True) for field in CITATION_FIELDS) for c in row["citations"]
            ]
            ok = released_rules == expected_rules and list(released_rules) == sorted(expected_rules)
            ok = ok and released_citations == expected_citations
        else:
            ok = row["rules"] == [] and row["citations"] == []
        if ok:
            rules_ok[label] += 1
        else:
            bad.append(f"{bench} {run}: cited rules or citations differ from the convicting models' votes")
    by_label = Counter(row["label"] for row in labels)
    report.append(
        f"{bench}: cited rules and citations match on {rules_ok['violation']}/{by_label['violation']} violation runs; "
        f"none on {rules_ok['no_violation']}/{by_label['no_violation']} clean runs"
    )

    per_model: dict[str, Counter] = {}
    for row in labels:
        per_model.setdefault(row["model"], Counter())[row["label"]] += 1
    report.append(
        f"{bench}: released labels, all runs: "
        + "; ".join(
            f"{model} {tally['violation']}/{sum(tally.values())} violation" for model, tally in sorted(per_model.items())
        )
    )
    return report, bad


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--annotations", type=Path, required=True, help="the annotations release")
    args = parser.parse_args(argv)
    mismatches: list[str] = []
    for bench in el.BENCHES:
        report, bad = check_bench(args.annotations, bench)
        print("\n".join(report))
        mismatches.extend(bad)
    if mismatches:
        print(f"MISMATCH: {len(mismatches)} run(s) differ from the released labels")
        print("\n".join(mismatches))
        return 1
    print("OK: every recomputable field matches the released labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
