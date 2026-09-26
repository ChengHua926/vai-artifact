"""Count conditional scope support without changing historical detections or labels.

Fixtures test a proposed mechanism under explicit added inputs. Passing them is
not evidence that those inputs existed, or would have been supplied, in old runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


def _categories(row):
    value = row.get("prior_scope", [])
    return {value} if isinstance(value, str) else set(value)


def _state(value):
    return {"violation": "violated", "unknown": "unresolved"}.get(value, value)


def partition(links, catalog, scenarios):
    """Apply reviewed obligation mappings only when every required check passes.

    Each check needs satisfied, violated and unresolved controls, with a real
    recording route and trace reference. Missing/failed support stays unresolved;
    an outside-scope citation is never promoted merely because a fixture exists.
    """
    by_check = defaultdict(list)
    scenario_ids = set()
    for scenario in scenarios:
        identity = (scenario["check_id"], scenario["scenario_id"])
        if identity in scenario_ids:
            raise ValueError(f"duplicate scenario: {identity}")
        scenario_ids.add(identity)
        by_check[scenario["check_id"]].append(scenario)
    check_support = {}
    for check, rows in sorted(by_check.items()):
        states = {_state(r["expected"]) for r in rows}
        passing = all(r.get("passed") is True and _state(r["expected"]) == _state(r["actual"])
                      and r.get("recording_route") and r.get("trace_hash") for r in rows)
        supported = bool(passing and {"satisfied", "violated", "unresolved"} <= states)
        check_support[check] = {"supported": supported, "scenarios": len(rows),
                                "expected_states": sorted(states),
                                "all_scenarios_pass": bool(passing)}
    obligations = {}
    for entry in catalog:
        identifier = entry["obligation_id"]
        if identifier in obligations:
            raise ValueError(f"duplicate obligation: {identifier}")
        checks = entry.get("check_ids", [])
        defined = all(entry.get(key) for key in
                      ("required_inputs", "authority", "applicability", "completion", "limitations"))
        supported = bool(defined and checks and all(
            check_support.get(check, {}).get("supported", False) for check in checks))
        obligations[identifier] = {"supported": supported, "check_ids": checks,
                                   "evidence_contract_complete": bool(defined)}
    cases = defaultdict(list)
    pair_ids = set()
    for link in links:
        case_key = (link["benchmark"], link["model_id"], link["case_id"])
        pair_key = case_key + (link.get("rule_id", link.get("condition_id")),)
        if pair_key in pair_ids:
            raise ValueError(f"duplicate task-rule link: {pair_key}")
        pair_ids.add(pair_key)
        cases[case_key].append(link)
    tasks, counts = [], {}
    for (benchmark, model, case), rows in sorted(cases.items()):
        potential = [r for r in rows if "could_catch" in _categories(r)]
        supported_links = [r for r in potential if obligations.get(
            r.get("obligation_id"), {}).get("supported", False)]
        if supported_links:
            category = "conditional_supported"
        elif potential:
            category = "unresolved"
        elif any("out_of_scope" in _categories(r) for r in rows):
            category = "outside_reviewed_scope"
        else:
            category = "unresolved"
        tasks.append({"benchmark": benchmark, "model_id": model, "case_id": case,
                      "task": rows[0].get("task", case), "classification": category,
                      "supported_obligations": sorted({r["obligation_id"] for r in supported_links}),
                      "prior_could_catch": bool(potential), "task_rule_pairs": len(rows)})
        panel = counts.setdefault(f"{benchmark}/{model}", {
            "tasks": 0, "conditional_supported": 0, "unresolved": 0,
            "outside_reviewed_scope": 0, "prior_could_catch": 0})
        panel["tasks"] += 1
        panel[category] += 1
        panel["prior_could_catch"] += bool(potential)
    return {"counts": counts, "tasks": tasks, "checks": check_support,
            "obligations": obligations, "task_rule_pairs": len(pair_ids),
            "method": "Executable mechanism checks conditional on specified trusted additional inputs; not new historical detections."}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_scenarios(scenarios):
    """Independently reload saved traces and repeat their registered checks."""
    from aa_commons import ActionRecord, trace_hash
    from aa_commons.structured_workflow import evaluate_workflow
    for row in scenarios:
        records = [ActionRecord.from_dict(record) for record in row['records']]
        if trace_hash(records) != row['trace_hash']:
            raise ValueError(f"saved trace hash differs: {row['check_id']}/{row['scenario_id']}")
        result = evaluate_workflow(records, {
            'observation_profile': row['profile_hash'], 'rule': row['check_id']})
        if result['status'] != _state(row['actual']):
            raise ValueError(f"replayed status differs: {row['check_id']}/{row['scenario_id']}")
        if result['verdict'] is not None:
            recorded = row.get('registered_verdict')
            if not recorded or recorded['violated'] != result['verdict'].violated:
                raise ValueError('saved registered verdict differs from independent replay')
    return len(scenarios)


def validate_link_universe(links, appendix, cases, contracts):
    """Check exact coverage against the frozen inputs, independently of mapping code."""
    expected = {('reference', row['suite'], row['task_id'], row['rule_id'])
                for row in appendix['task_rules']}
    by_attack = {(row['suite'], row['injection_task_id']): row['condition_id']
                 for row in contracts}
    expected |= {('agentdojo', row['model_id'], row['case_id'],
                  by_attack[row['suite'], row['injection_task_id']])
                 for row in cases if row['reference_group'] == 'successful_attack'
                 and not row['attack_related_witness']}
    actual = []
    for row in links:
        if row['benchmark'].lower() == 'agentdojo':
            actual.append(('agentdojo', row['model_id'], row['case_id'], row['condition_id']))
        else:
            actual.append(('reference', row['suite'], row['task'], row['rule_id']))
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError(f'task-rule universe mismatch: expected {len(expected)}, got {len(actual)}; '
                         f'missing {sorted(expected-set(actual))[:5]}, extra {sorted(set(actual)-expected)[:5]}')
    return len(expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", type=Path, required=True,
                        help="Prototype eval directory containing paper_main_v1 and catalog_replay")
    parser.add_argument("--agentdojo-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    repo = Path(__file__).resolve().parents[2]
    if output.is_relative_to(repo) or output.is_relative_to(args.reference_root.resolve()):
        raise ValueError("results must be outside source and frozen inputs")
    if output.exists() and any(output.iterdir()):
        raise ValueError("use a fresh output directory; preserve previous results")
    from .obligations import obligation_catalog, per_case_links
    from .fixtures import run_all
    inputs = [args.reference_root / "data/added_records/appendix.json",
              args.reference_root / "data/added_records/task_partition.json",
              args.reference_root / "catalog_replay/presentation_v4/tasks.jsonl",
              args.reference_root / "data/runs/agentdojo/grader_contracts.jsonl",
              args.agentdojo_results / "case_deltas.jsonl",
              args.agentdojo_results / "summary.json"]
    before = {str(p.resolve()): _sha(p) for p in inputs}
    catalog = obligation_catalog()
    links = per_case_links(args.reference_root.resolve(), args.agentdojo_results.resolve())
    checked_links = validate_link_universe(
        links, json.loads(inputs[0].read_text()),
        [json.loads(line) for line in inputs[4].read_text().splitlines()],
        [json.loads(line) for line in inputs[3].read_text().splitlines()])
    scenarios = run_all()
    replayed_scenarios = validate_scenarios(scenarios)
    report = partition(links, catalog, scenarios)
    report['independent_validation'] = {'exact_task_rule_links': checked_links,
                                        'saved_traces_rehashed_and_replayed': replayed_scenarios}
    assert before == {str(p.resolve()): _sha(p) for p in inputs}, "frozen inputs changed"
    output.mkdir(parents=True, exist_ok=True)
    artifacts = {"catalog.json": catalog, "task_obligation_links.json": links,
                 "scenario_results.json": scenarios, "scope_support.json": report}
    for name, data in artifacts.items():
        (output / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    sources = sorted(set(Path(__file__).parent.glob("*.py")) |
                     set((repo / "packages/commons/aa_commons").rglob("*.py")) |
                     set((repo / "packages/sdk/aa_sdk").rglob("*.py")))
    manifest = {"command": sys.argv, "python": sys.version,
                "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
                "source_sha256": {str(p.relative_to(repo)): _sha(p) for p in sources},
                "input_sha256": before,
                "output_sha256": {name: _sha(output / name) for name in artifacts},
                "warning": "Source may be uncommitted; source hashes define this experiment. Hypothetical evidence is not added to historical traces."}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"counts": report["counts"], "checks": len(report["checks"]),
                      "supported_checks": sum(c["supported"] for c in report["checks"].values()),
                      "scenarios": len(scenarios), "output": str(output)}, indent=2))


if __name__ == "__main__":
    main()
