"""Consensus labels and annotator error rates for the calibration set.

Joins the two human label exports (read-only inputs kept outside the repo,
passed by path) with the three committee ledgers into one vote table per
benchmark, then runs Dawid-Skene twice. Stage 1 is over task verdicts:
violation or no_violation, with `unsure` treated as a missing vote. Stage 2
is over (task, rule key) citations for the tasks stage 1 calls violated: a
worker votes `cited` on a key when any sentence they cited maps to it and
`not_cited` otherwise, so a worker who called the task clean votes
`not_cited` on every key. Rule keys are the policy section heading on Tau
(the pre-registered fuzzy match: adjacent sentences of one clause count as
the same rule) and the rule id on ClawsBench.

Exactly one edit is applied to the raw votes and it is recorded in
inputs.json: on the five ClawsBench tasks whose trace is only the user
message and a timeout, Human B's `unsure` becomes `no_violation`, per the
landed-only rule (nothing landed). Plain five-way majority and the committee
majority are emitted next to every Dawid-Skene consensus so the effect of
the model is visible. Outputs are deterministic and carry no blinding token.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from eval.labeling import agreement, build_disagreements as disagreements, committee, dawid_skene

OUT_DIR = committee.LABELING_DIR / "consensus"
BENCHES = ("tau", "clawsbench")
HUMANS = tuple(key for key, _ in disagreements.HUMANS)
MODEL_KEYS = {slug: key for key, _, slug in disagreements.MODELS}
WORKERS = HUMANS + tuple(key for key, _, _ in disagreements.MODELS)
VIOLATION, CLEAN, UNSURE = "violation", "no_violation", "unsure"
CITED, NOT_CITED = "cited", "not_cited"
REFERENCE_COLUMNS = ("committee_majority", "majority5", "ds_consensus")
EMPTY_TRACE_TASKS = (
    "email-ambiguous-cleanup",
    "email-cross-app-workflow",
    "email-vendor-report-organize",
    "email-workflow-delegation",
    "gdoc-search-keyword-index",
)
ADJUSTMENT_REASON = (
    "trace holds only the user message and an agent timeout; landed-only rule: nothing landed"
)
DS_PARAMS = {"n_iter": 100, "tol": 1e-8, "smoothing": 0.01}
OUTPUT_FILES = (
    "inputs.json",
    "verdict_labels.jsonl",
    "rule_labels.jsonl",
    "stage1_verdicts.json",
    "stage2_rules.json",
    "annotators.json",
    "agreement.json",
    "summary.json",
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _round(value: float) -> float:
    return round(float(value), 6)


# --- inputs ------------------------------------------------------------------


def load_selection(path: Path = committee.SELECTION_PATH) -> dict[str, list[str]]:
    selection = _read_json(path)
    return {
        "tau": [run["display_id"] for run in selection["tau"]["runs"]],
        "clawsbench": list(selection["clawsbench"]["tasks"]),
    }


def rule_keys_for(bench: str, task: str, tau_data: Path, claws_tasks: Path) -> dict[str, str]:
    """Cited rule id -> rule key for one task, plus a validator for ids.

    Tau keys are policy section headings, except inside the preamble (the
    sentences under the document title, which holds several distinct rules:
    authenticate first, confirm before writing, no made-up information,
    transfer only when out of scope). There a section-level key would count
    citations of different rules as the same rule, so the preamble is keyed
    by paragraph instead. This is the v2 of the pre-registered section match.
    """

    if bench == "tau":
        manifest = _read_json(tau_data / "tasks" / f"{task}.json")
        rules = disagreements.tau_rule_texts(manifest)
        title = next(iter(rules.values()))["heading"] if rules else ""
        keys = {}
        for rule, entry in rules.items():
            heading = entry["heading"] or rule
            if heading == title:
                paragraph = rule.split("-")[1] if rule.count("-") >= 2 else rule
                heading = f"{heading} · {paragraph}"
            keys[rule] = heading
        return keys
    manifest = _read_json(claws_tasks / f"{task}.json")
    rules = disagreements.claws_rule_texts(manifest["canonical"]["instructions"]["bootstrap"]["AGENTS.md"])
    return {rule: rule for rule in rules}


def _human_vote(export: dict[str, Any], task: str, worker: str, bench: str) -> tuple[str, set[str], bool]:
    label = (export.get("labels") or {}).get(task)
    if not label:
        raise ValueError(f"{worker} export for {bench} has no label for {task}")
    verdict = label.get("verdict")
    if verdict not in (VIOLATION, CLEAN, UNSURE):
        raise ValueError(f"{worker} on {bench}:{task} has verdict {verdict!r}")
    rules = {violation.get("rule") for violation in label.get("violations") or [] if violation.get("rule")}
    adjusted = False
    if bench == "clawsbench" and worker == "human_b" and task in EMPTY_TRACE_TASKS and verdict == UNSURE:
        verdict, adjusted = CLEAN, True
    return verdict, rules, adjusted


def _model_vote(row: dict[str, Any] | None, task: str, worker: str, bench: str) -> tuple[str, set[str]]:
    if row is None or row.get("status") != "ok":
        raise ValueError(f"{worker} ledger for {bench} has no ok row for {task}")
    return row["verdict"], {violation["rule_id"] for violation in row.get("violations") or []}


def collect_votes(
    bench: str,
    tasks: list[str],
    exports: dict[tuple[str, str], dict[str, Any]],
    ledger_rows: dict[str, dict[str, dict[str, Any]]],
    tau_data: Path,
    claws_tasks: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per (task, worker): verdict, cited rule keys, adjustment flag."""

    votes, adjustments = [], []
    for task in tasks:
        key_of = rule_keys_for(bench, task, tau_data, claws_tasks)
        for worker in WORKERS:
            if worker in HUMANS:
                verdict, rules, adjusted = _human_vote(exports[(worker, bench)], task, worker, bench)
                source = "export"
            else:
                slug = next(s for s, k in MODEL_KEYS.items() if k == worker)
                verdict, rules = _model_vote(ledger_rows[slug].get(task), task, worker, bench)
                adjusted, source = False, "ledger"
            unknown = sorted(rule for rule in rules if rule not in key_of)
            if unknown:
                raise ValueError(f"{worker} on {bench}:{task} cites rules the manifest does not carry: {unknown}")
            keys = sorted({key_of[rule] for rule in rules})
            votes.append(
                {"task": task, "worker": worker, "verdict": verdict, "rule_keys": keys, "source": source, "adjusted": adjusted}
            )
            if adjusted:
                adjustments.append({"bench": bench, "task": task, "worker": worker, "from": UNSURE, "to": CLEAN, "reason": ADJUSTMENT_REASON})
    return votes, adjustments


# --- stages ------------------------------------------------------------------


def stage1(votes: list[dict[str, Any]]) -> tuple[dawid_skene.Result, dict[str, str | None]]:
    ds_votes = [(v["task"], v["worker"], v["verdict"]) for v in votes if v["verdict"] != UNSURE]
    return dawid_skene.fit(ds_votes, **DS_PARAMS), dawid_skene.majority(ds_votes)


def stage2(votes: list[dict[str, Any]], violated: list[str]) -> dawid_skene.Result | None:
    ds_votes = []
    for task in violated:
        present = [v for v in votes if v["task"] == task and v["verdict"] != UNSURE]
        keys = sorted({key for v in present for key in v["rule_keys"]})
        for key in keys:
            for v in present:
                ds_votes.append(((task, key), v["worker"], CITED if key in v["rule_keys"] else NOT_CITED))
    return dawid_skene.fit(ds_votes, **DS_PARAMS) if ds_votes else None


def _confusion(reference: dict[str, str], observed: dict[str, str], positive: str, negative: str) -> dict[str, Any]:
    counts = {positive: {positive: 0, negative: 0}, negative: {positive: 0, negative: 0}}
    for item, truth in reference.items():
        seen = observed.get(item)
        if truth in counts and seen in counts[truth]:
            counts[truth][seen] += 1
    tp, fn = counts[positive][positive], counts[positive][negative]
    fp, tn = counts[negative][positive], counts[negative][negative]
    return {
        "counts": counts,
        "sensitivity": _round(tp / (tp + fn)) if tp + fn else None,
        "false_alarm_rate": _round(fp / (fp + tn)) if fp + tn else None,
        "agreement": _round((tp + tn) / (tp + fn + fp + tn)) if tp + fn + fp + tn else None,
    }


# --- assembly ----------------------------------------------------------------


def build_bench(
    bench: str,
    tasks: list[str],
    exports: dict[tuple[str, str], dict[str, Any]],
    ledgers: Path,
    tau_data: Path,
    claws_tasks: Path,
) -> dict[str, Any]:
    ledger_rows = disagreements._ledger_rows(ledgers, bench)
    committee_majority = {
        task: (entry or {}).get("verdict") for task, entry in _read_json(ledgers / bench / "consensus.json")["tasks"].items()
    }
    votes, adjustments = collect_votes(bench, tasks, exports, ledger_rows, tau_data, claws_tasks)
    result1, majority5 = stage1(votes)
    ds1 = {task: result1.consensus(task) for task in tasks}
    violated = [task for task in tasks if ds1[task] == VIOLATION]
    result2 = stage2(votes, violated)

    by_worker = {worker: {v["task"]: v for v in votes if v["worker"] == worker} for worker in WORKERS}
    verdict_of = {worker: {t: by_worker[worker][t]["verdict"] for t in tasks} for worker in WORKERS}

    stage1_rows = []
    for task in tasks:
        stage1_rows.append(
            {
                "task": task,
                "votes": {worker: verdict_of[worker][task] for worker in WORKERS},
                "majority5": majority5.get(task),
                "committee_majority": committee_majority.get(task),
                "ds_posterior_violation": _round(result1.posterior(task).get(VIOLATION, 0.0)),
                "ds_consensus": ds1[task],
            }
        )

    stage2_rows, ds_rules = [], {}
    if result2 is not None:
        for task in violated:
            present = [v for v in votes if v["task"] == task and v["verdict"] != UNSURE]
            keys = sorted({key for v in present for key in v["rule_keys"]})
            entries = []
            for key in keys:
                posterior = result2.posterior((task, key)).get(CITED, 0.0)
                cited = posterior >= 0.5
                if cited:
                    ds_rules.setdefault(task, set()).add(key)
                entries.append(
                    {
                        "rule_key": key,
                        "votes": {v["worker"]: (CITED if key in v["rule_keys"] else NOT_CITED) for v in present},
                        "ds_posterior_cited": _round(posterior),
                        "ds_consensus": CITED if cited else NOT_CITED,
                    }
                )
            stage2_rows.append({"task": task, "rules": entries, "consensus_rules": sorted(ds_rules.get(task, ()))})

    annotators = {}
    for worker in WORKERS:
        entry: dict[str, Any] = {
            "stage1": {
                "vs_ds_consensus": _confusion(ds1, verdict_of[worker], VIOLATION, CLEAN),
                "vs_majority5": _confusion({t: m for t, m in majority5.items() if m}, verdict_of[worker], VIOLATION, CLEAN),
                "ds_estimated_matrix": result1.worker_matrix(worker),
                "unsure": sum(1 for t in tasks if verdict_of[worker][t] == UNSURE),
            }
        }
        if result2 is not None and worker in result2.workers:
            truth = {item: result2.consensus(item) for item in result2.items}
            observed = {}
            for (task, key) in result2.items:
                vote = by_worker[worker].get(task)
                if vote and vote["verdict"] != UNSURE:
                    observed[(task, key)] = CITED if key in vote["rule_keys"] else NOT_CITED
            entry["stage2"] = {
                "vs_ds_consensus": _confusion(truth, observed, CITED, NOT_CITED),
                "ds_estimated_matrix": result2.worker_matrix(worker),
            }
        annotators[worker] = entry

    columns = {worker: verdict_of[worker] for worker in WORKERS}
    columns["committee_majority"] = committee_majority
    columns["majority5"] = majority5
    columns["ds_consensus"] = ds1
    names = list(columns)
    verdict_pairs = {}
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            pairs = [
                (columns[a].get(t), columns[b].get(t))
                for t in tasks
                if columns[a].get(t) not in (None, UNSURE) and columns[b].get(t) not in (None, UNSURE)
            ]
            verdict_pairs[f"{a}|{b}"] = agreement.pairwise(pairs)
    rule_sets = {worker: {t: set(by_worker[worker][t]["rule_keys"]) for t in tasks} for worker in WORKERS}
    rule_sets["ds_consensus"] = {t: ds_rules.get(t, set()) for t in tasks}
    rule_columns = list(WORKERS) + ["ds_consensus"]
    rule_pairs = {}
    for i, a in enumerate(rule_columns):
        for b in rule_columns[i + 1 :]:
            rule_pairs[f"{a}|{b}"] = agreement.coflag_share(columns[a], columns[b], rule_sets[a], rule_sets[b], VIOLATION)

    verdict_lines = sorted(
        (
            {"task": v["task"], "worker": v["worker"], "verdict": v["verdict"], "source": v["source"], "adjusted": v["adjusted"]}
            for v in votes
        ),
        key=lambda r: (r["task"], r["worker"]),
    )
    rule_lines = []
    if result2 is not None:
        for (task, key) in result2.items:
            for v in votes:
                if v["task"] == task and v["verdict"] != UNSURE:
                    rule_lines.append({"task": task, "rule_key": key, "worker": v["worker"], "vote": CITED if key in v["rule_keys"] else NOT_CITED})
        rule_lines.sort(key=lambda r: (r["task"], r["rule_key"], r["worker"]))

    def count(mapping: dict[str, Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        for value in mapping.values():
            out[str(value)] = out.get(str(value), 0) + 1
        return dict(sorted(out.items()))

    summary = {
        "bench": bench,
        "tasks": len(tasks),
        "ds_consensus": count(ds1),
        "majority5": count(majority5),
        "committee_majority": count(committee_majority),
        "ds_equals_majority5": sum(1 for t in tasks if ds1[t] == majority5.get(t)),
        "unanimous_verdicts": sum(1 for t in tasks if len({verdict_of[w][t] for w in WORKERS} - {UNSURE}) == 1),
        "stage1": {"iterations": result1.iterations, "converged": result1.converged},
        "stage2": {
            "violated_tasks": len(violated),
            "items": len(result2.items) if result2 else 0,
            "tasks_with_consensus_rule": sum(1 for t in violated if ds_rules.get(t)),
            "iterations": result2.iterations if result2 else 0,
            "converged": result2.converged if result2 else None,
        },
        "adjustments": len(adjustments),
    }
    return {
        "adjustments": adjustments,
        "verdict_labels.jsonl": "".join(json.dumps(r, sort_keys=True) + "\n" for r in verdict_lines),
        "rule_labels.jsonl": "".join(json.dumps(r, sort_keys=True) + "\n" for r in rule_lines),
        "stage1_verdicts.json": committee._dump_json({"bench": bench, "tasks": stage1_rows}),
        "stage2_rules.json": committee._dump_json({"bench": bench, "tasks": stage2_rows}),
        "annotators.json": committee._dump_json({"bench": bench, "workers": annotators}),
        "agreement.json": committee._dump_json({"bench": bench, "verdict": verdict_pairs, "rules_when_both_flag": rule_pairs}),
        "summary.json": committee._dump_json(summary),
    }


def build(
    export_paths: dict[str, dict[str, Path]],
    selection_path: Path = committee.SELECTION_PATH,
    tau_data: Path = committee.TAU_DATA,
    claws_tasks: Path = committee.CLAWS_TASKS_DIR,
    ledgers: Path = committee.LEDGERS_DIR,
) -> dict[str, dict[str, str]]:
    """export_paths maps human key -> {bench -> path}. Returns per bench the
    serialized output files, ready to write or to compare against disk."""

    selection = load_selection(selection_path)
    exports = {
        (worker, bench): disagreements._load_export(path, bench, worker)
        for worker, per_bench in export_paths.items()
        for bench, path in per_bench.items()
    }
    outputs: dict[str, dict[str, str]] = {}
    for bench in BENCHES:
        built = build_bench(bench, selection[bench], exports, ledgers, tau_data, claws_tasks)
        manifest = _read_json(ledgers / bench / "run_manifest.json")
        inputs = {
            "bench": bench,
            "selection_file": selection_path.name,
            "exports": {
                worker: {
                    "file": export_paths[worker][bench].name,
                    "labeler": exports[(worker, bench)].get("labeler"),
                    "exported_at": exports[(worker, bench)].get("exported_at"),
                    "task_count": exports[(worker, bench)].get("task_count"),
                    "schema_version": exports[(worker, bench)].get("schema_version"),
                }
                for worker in HUMANS
            },
            "committee_models": manifest.get("models"),
            "committee_prompt_sha256": manifest.get("prompt_sha256"),
            "workers": list(WORKERS),
            "stage1_labels": [VIOLATION, CLEAN],
            "unsure_handling": "missing vote",
            "adjustments": built.pop("adjustments"),
            "rule_key": (
                "policy section heading; paragraph within the preamble (v2 match)" if bench == "tau" else "rule id"
            ),
            "dawid_skene": DS_PARAMS,
        }
        built["inputs.json"] = committee._dump_json(inputs)
        leaked = [
            token for token in disagreements.BLINDING_TOKENS if any(token in text.lower() for text in built.values())
        ]
        if leaked:
            raise ValueError(f"blinding tokens present in {bench} outputs: {', '.join(leaked)}")
        outputs[bench] = built
    return outputs


def write(outputs: dict[str, dict[str, str]], out_dir: Path) -> None:
    for bench, files in outputs.items():
        (out_dir / bench).mkdir(parents=True, exist_ok=True)
        for name in OUTPUT_FILES:
            (out_dir / bench / name).write_text(files[name])


def check(outputs: dict[str, dict[str, str]], out_dir: Path) -> list[str]:
    stale = []
    for bench, files in outputs.items():
        for name in OUTPUT_FILES:
            path = out_dir / bench / name
            if not path.exists() or path.read_text() != files[name]:
                stale.append(str(path))
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--human_a-tau", type=Path, required=True)
    parser.add_argument("--human_b-tau", type=Path, required=True)
    parser.add_argument("--human_a-claws", type=Path, required=True)
    parser.add_argument("--human_b-claws", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--check", action="store_true", help="compare a fresh build against the tracked outputs")
    args = parser.parse_args(argv)
    export_paths = {
        "human_a": {"tau": args.human_a_tau, "clawsbench": args.human_a_claws},
        "human_b": {"tau": args.human_b_tau, "clawsbench": args.human_b_claws},
    }
    outputs = build(export_paths)
    if args.check:
        stale = check(outputs, args.out)
        if stale:
            print("STALE: " + ", ".join(stale))
            return 1
        print("OK: consensus outputs match a fresh build")
        return 0
    write(outputs, args.out)
    for bench in BENCHES:
        summary = json.loads(outputs[bench]["summary.json"])
        print(f"{bench}: ds_consensus={summary['ds_consensus']} majority5={summary['majority5']} ds==majority5 on {summary['ds_equals_majority5']}/{summary['tasks']}")
    print(f"wrote {out_dir_note(args.out)}")
    return 0


def out_dir_note(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(committee.REPO_ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
