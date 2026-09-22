"""Select the reported promise set from the v4 replay without changing labels."""
from __future__ import annotations
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXCLUDED_ARMS = {"one_tool_at_a_time", "tool_with_response"}
EXCLUDED_RULES = {"airline-p05-s06", "retail-p08-s12", "retail-p08-s13"}
THEMES = {
    "Unsupported factual statements": {"airline-p04-s05", "retail-p07-s11"},
    "Missing explicit approval": {"airline-p03-s04", "retail-p06-s10"},
    "Missing complete modification/exchange list": {"retail-p37-s64", "retail-p45-s75"},
    "Incorrect or missing handoff": {"airline-p07-s09", "retail-p10-s15"},
    "Missing cancellation order/reason confirmation": {"retail-p30-s52"},
    "Missing user-selected payment/refund method": {"airline-p30-s96", "retail-p39-s67", "retail-p42-s71", "retail-p47-s78"},
}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def project(raw):
    rows = []
    for row in raw:
        keep = {i for i, fire in enumerate(row["fires"]) if fire["arm"] not in EXCLUDED_ARMS}
        citations = [{**c, "matching_fire_indexes": sorted(keep & set(c.get("matching_fire_indexes", [])))}
                     for c in row["citations"] if c["rule_id"] not in EXCLUDED_RULES]
        rules = []
        for rule in row["union_rule_assessments"]:
            if rule["rule_id"] in EXCLUDED_RULES:
                continue
            matched = sorted(keep & set(rule.get("matching_fire_indexes", [])))
            rules.append({**rule, "matching_original_fire_indexes": matched, "matched": bool(matched)})
        matches = sorted({i for c in citations for i in c["matching_fire_indexes"]})
        rows.append({**{key: row.get(key) for key in ("bench", "model_id", "task", "case_id", "quality_group", "label")},
                     "fires": [{**fire, "original_fire_index": i} for i, fire in enumerate(row["fires"]) if i in keep],
                     "fired": bool(keep), "same_reason": bool(matches), "matching_original_fire_indexes": matches,
                     "citations": citations, "rules": rules})
    return rows


def scopes(rows):
    counts = {scope: sum(any(scope in rule["scope_categories"] for rule in row["rules"]) for row in rows)
              for scope in ("should_catch", "could_catch", "out_of_scope", "unresolved")}
    by_rule = defaultdict(list)
    for row in rows:
        for rule in row["rules"]:
            by_rule[rule["rule_id"]].append((row, rule))
    return {"task_counts": counts, "distinct_rules": len(by_rule), "task_rule_pairs": sum(map(len, by_rule.values())),
            "by_rule": [{"rule_id": key, "policy_text": items[0][1]["policy_text"], "tasks": len(items),
                         "categories": sorted({scope for _, rule in items for scope in rule["scope_categories"]}),
                         "members": [{"model_id": row["model_id"], "task": row["task"],
                                      "scope_components": rule["scope_components"], "allegation_quality": rule.get("allegation_quality", [])}
                                     for row, rule in items]}
                        for key, items in sorted(by_rule.items())]}


def panel(rows):
    positive = [row for row in rows if row["label"] == "violation"]
    no_fire = [row for row in positive if not row["fired"]]
    clean = [row for row in rows if row["label"] == "no_violation"]
    assert len(positive) + len(clean) == len(rows)
    return {"n": len(rows), "violation_fire": len(positive) - len(no_fire), "violation_no_fire": len(no_fire),
            "clean_fire": sum(row["fired"] for row in clean), "clean_no_fire": sum(not row["fired"] for row in clean),
            "same_reason_tasks": sum(row["same_reason"] for row in positive),
            "unmatched_firing_tasks": [row["task"] for row in positive if row["fired"] and not row["same_reason"]],
            "clean_firing_tasks": [row["task"] for row in clean if row["fired"]],
            "no_fire_scope": scopes(no_fire), "all_positive_scope": scopes(positive),
            "no_fire_themes": {name: sum(bool(ids & {rule["rule_id"] for rule in row["rules"]}) for row in no_fire)
                               for name, ids in THEMES.items()}}


def generate():
    source = HERE / "results_v4/report/runs.jsonl"
    rows = project(read_rows(source))
    previous = read_rows(HERE / "presentation_successful_operations/tasks.jsonl")
    identity = lambda row: (row["bench"], row["model_id"], row["task"])
    before = {identity(row): row for row in previous}
    assert len(rows) == len(previous) == len(before) == 388
    assert {identity(row) for row in rows} == set(before)
    changes = []
    for row in rows:
        old = before[identity(row)]
        assert (row["label"], row["quality_group"]) == (old["label"], old["quality_group"])
        fire_key = lambda f: (f["arm"], f["seq"], tuple(f.get("targets", [])))
        new_fires, old_fires = {fire_key(f) for f in row["fires"]}, {fire_key(f) for f in old["fires"]}
        if new_fires != old_fires or row["same_reason"] != old["same_reason"]:
            changes.append({"bench": row["bench"], "model_id": row["model_id"], "task": row["task"], "label": row["label"],
                            "before_fired": old["fired"], "after_fired": row["fired"],
                            "before_same_reason": old["same_reason"], "after_same_reason": row["same_reason"],
                            "removed": sorted(old_fires - new_fires), "added": sorted(new_fires - old_fires)})
    panels = {
        "Tau GLM": panel([r for r in rows if r["model_id"] == "glm47"]),
        "Tau Qwen": panel([r for r in rows if r["model_id"] == "qwen3_30b"]),
        "ClawsBench, 49-run subset": panel([r for r in rows if r["bench"] == "clawsbench" and r["quality_group"] == "primary"]),
        "ClawsBench, all runs": panel([r for r in rows if r["bench"] == "clawsbench"]),
    }
    assert [p["n"] for p in panels.values()] == [164, 164, 49, 60]
    report = ["# Updated evaluation", "", "Labels and the 49/60 ClawsBench cohorts are unchanged. Counts use the selected promises from the complete v4 SDK/resolved-evaluator replay.", "",
              "| Benchmark | Tasks | Violation + fire | Violation + no fire | Clean + fire | Clean + no fire | Same reason |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for name, p in panels.items():
        report.append(f"| {name} | {p['n']} | {p['violation_fire']} | {p['violation_no_fire']} | {p['clean_fire']} | {p['clean_no_fire']} | {p['same_reason_tasks']}/{p['violation_fire']} |")
    report += ["", "Scope classifications remain an analysis of the cited rules. They are not new labels. A task may have multiple scope categories. Allegation-quality flags stay separate from capability: a mechanically checkable allegation is not a confirmed miss when its factual premise is contradicted by the trace.", "",
               "The complete rule inventory and per-task evidence are in rule_review.md, summary.json, and tasks.jsonl. Changes against the previous selected results are in changes.jsonl.", ""]
    review = ["# Rule review inventory", "", "Review each distinct policy sentence and its conditions. Counts deduplicate each rule within a task. The same rule can have different obligations or evidence availability in different tasks; those components and citations are retained in tasks.jsonl.", ""]
    no_fire = [r for r in rows if r["label"] == "violation" and not r["fired"] and r["quality_group"] == "primary"]
    combined = scopes(no_fire)
    for rule in combined["by_rule"]:
        review += [f"## {rule['rule_id']}", "", rule["policy_text"], "",
                   f"Current categories: {', '.join(rule['categories'])}. Tasks: {rule['tasks']}.", ""]
        components = sorted({(c['obligation'], c['scope'], c['reason']) for item in rule['members'] for c in item['scope_components']})
        review += [f"- {obligation}: {scope}. {reason}" for obligation, scope, reason in components]
        review += ["", "Tasks: " + "; ".join(f"{m['model_id']} {m['task']}" for m in rule['members']) + ".", ""]
    inputs = [source, HERE / "results_v4/manifest.json", HERE / "presentation_successful_operations/tasks.jsonl", Path(__file__)]
    method = {"labels_changed": False, "new_agent_runs": False, "replay": "results_v4", "excluded_arms": sorted(EXCLUDED_ARMS),
              "excluded_display_rule_ids": sorted(EXCLUDED_RULES), "themes": {k: sorted(v) for k, v in THEMES.items()},
              "rule_union": "Every cited rule from an accepted positive-voting LLM, conditional on a positive frozen task label.",
              "scope_status": "Exploratory rule-level classification; exact rules, conditions and disputed premises retained for human review.",
              "inputs": {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}}
    outputs = {"tasks.jsonl": ''.join(json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n' for row in rows),
               "summary.json": dumps({"panels": panels, "combined_no_fire_scope": combined}), "method.json": dumps(method),
               "changes.jsonl": ''.join(json.dumps(row, sort_keys=True) + '\n' for row in changes),
               "report.md": '\n'.join(report), "rule_review.md": '\n'.join(review)}
    outputs["SHA256SUMS"] = ''.join(f"{hashlib.sha256(value.encode()).hexdigest()}  {name}\n" for name, value in sorted(outputs.items()))
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    destination = HERE / "presentation_v4"
    outputs = generate()
    for name, value in outputs.items():
        if args.check:
            assert (destination / name).read_text() == value, name
        else:
            destination.mkdir(exist_ok=True)
            (destination / name).write_text(value)
    print(f"{'Verified' if args.check else 'Wrote'} {len(outputs)} selected-presentation artifacts.")


if __name__ == "__main__":
    main()
