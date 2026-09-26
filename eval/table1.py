"""Reproduce Table 1's Violations, Detected and False alarms columns offline.

AgentDojo: regenerate the frozen ledgers from the sealed corpus, then replay the
outbound (AAP-4) check with the initial-destinations allowlist through
eval.agentdojo.outbound_review and eval.agentdojo.allowlist_sensitivity.
tau3-bench and ClawsBench: evaluate the captured action records with the shared
AAP catalog (eval.catalog_replay.runtime) and cross-tabulate against the reference
labels of the annotations release. The tau3-bench records are first re-derived
from the sealed corpus and must equal the captured ones.

No model calls, no new labels, no network. Usage (after restoring eval/data, see
eval/README.md):

    python -m eval.table1

The reference labels are read from eval/data/labels (copies of the four
label files of the annotations release); --annotations points elsewhere.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "eval" / "data"
PAPER = REPO / "eval" / "data" / "runs"
RECORDS = DATA / "records" / "captured_records.jsonl.gz"
RECORDS_SHA256 = "7545131853ca44a0a0975011e91b18e22f73db7a87c95a02789dffa4bb14f371"
ANNOTATIONS = DATA / "labels"
REVIEW_FILES = ("other_objective_alarms.jsonl", "other_objective_cases.jsonl",
                "benign-actions.json", "benign-runs.json", "resisted-other-actions.json")
# The two message-format checks are not part of the reported promise set
# (same exclusion as the paper's selected presentation).
EXCLUDED_ARMS = {"one_tool_at_a_time", "tool_with_response"}
MODELS = {"glm47": "GLM-4.7-Flash", "qwen3_30b": "Qwen3-30B-A3B"}
PAPER_TABLE = {  # violations, detected, (false alarms, clean runs)
    ("AgentDojo", "glm47"): (266, 156, (7, 97)),
    ("AgentDojo", "qwen3_30b"): (161, 92, (9, 97)),
    ("tau3-bench", "glm47"): (111, 15, (0, 53)),
    ("tau3-bench", "qwen3_30b"): (154, 31, (0, 10)),
    ("ClawsBench", "glm-5.2"): (4, 0, (2, 56)),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def agentdojo_ledgers() -> None:
    """Regenerate eval/data/runs/agentdojo from the corpus and check its hashes."""
    from eval.run import generate_benchmark_bundle
    ledger = PAPER / "agentdojo"
    if not (ledger / "cases.jsonl").is_file():
        print("Regenerating the AgentDojo ledgers from the sealed corpus", flush=True)
        generate_benchmark_bundle(PAPER, benchmark="agentdojo")
    expected = {}
    for line in (DATA / "agentdojo-ledgers.SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        expected[name] = digest
    for name, digest in expected.items():
        if sha256(ledger / name) != digest:
            raise SystemExit(f"AgentDojo ledger differs from the frozen ledger: {name}")
    print(f"AgentDojo ledgers match the frozen checksums ({len(expected)} files)", flush=True)


def review_root(work: Path) -> Path:
    """Key the reviews by this copy's case ids (eval/data/runs/case_id_map.json)."""
    mapping = json.loads((PAPER / "case_id_map.json").read_text())["agentdojo"]
    root = work / "reviews"
    root.mkdir()
    for name in REVIEW_FILES:
        source = DATA / "reviews" / name
        rows = read_rows(source) if name.endswith(".jsonl") else json.loads(source.read_text())
        for row in rows:
            if row["case_id"] not in mapping:
                raise SystemExit(f"review case id missing from case_id_map.json: {row['case_id']}")
            row["case_id"] = mapping[row["case_id"]]
        text = ("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
                if name.endswith(".jsonl") else json.dumps(rows, sort_keys=True))
        (root / name).write_text(text)
    return root


def agentdojo(work: Path) -> dict:
    from eval.agentdojo import allowlist_sensitivity, outbound_review
    agentdojo_ledgers()
    reviews = review_root(work)
    print("AgentDojo 1/3: outbound replay with the original allowlists", flush=True)
    outbound_review.run_review(PAPER, work / "outbound-v2", reviews)
    print("AgentDojo 2/3: calendar-participant allowlist", flush=True)
    allowlist_sensitivity.run(PAPER, work / "outbound-v2", reviews, work / "calendar", "calendar")
    print("AgentDojo 3/3: initial-destinations allowlist", flush=True)
    summary = allowlist_sensitivity.run(PAPER, work / "outbound-v2", reviews, work / "initial-destinations",
                                        "initial-destinations", work / "calendar")
    result = {}
    for model in MODELS:
        attacks, benign = summary["models"][model]["successful_attack"], summary["models"][model]["benign"]
        result["AgentDojo", model] = (attacks["runs"], attacks["variant_attack_related_runs"],
                                      (benign["variant_alarm_runs"], benign["runs"]))
    return result


def labels(annotations: Path) -> dict:
    index = json.loads((annotations / "tau" / "index.json").read_text())
    found = {}
    for name in ("labels-1.jsonl", "labels-2.jsonl"):
        for row in read_rows(annotations / "tau" / name):
            entry = index[row["run"]]
            if entry["model"] != row["model"]:
                raise SystemExit(f"annotation index disagrees with label: {row['run']}")
            found[entry["case_id"]] = ("tau3-bench", row["model"], row["label"])
    for row in read_rows(annotations / "clawsbench" / "labels.jsonl"):
        found[row["run"]] = ("ClawsBench", "glm-5.2", row["label"])
    return found


def check_tau_records(captures: list[dict]) -> None:
    """Re-derive the tau3-bench records from the sealed corpus; they must be identical."""
    from eval.catalog_replay import runtime
    from eval.tau.corpus import load_tau_cases
    paper_id = {new: old for old, new in json.loads((PAPER / "case_id_map.json").read_text())["tau"].items()}
    captured = {c["task"]: c for c in captures if c["benchmark"].startswith("tau_")}
    cases = load_tau_cases()
    if len(cases) != len(captured):
        raise SystemExit("the corpus and the captured records hold different tau3-bench runs")
    for case in cases:
        task = paper_id[case["case_id"]]
        records = [r.to_dict() for r in runtime.make_tau_records(case["trace"], case["turns"], task)]
        capture = captured.get(task)
        if capture is None or capture["benchmark"] != "tau_" + case["domain"] or capture["records"] != records:
            raise SystemExit(f"captured tau3-bench records differ from the sealed corpus: {task}")
    print(f"tau3-bench records re-derived from the sealed corpus match the captured records ({len(cases)} runs)",
          flush=True)


def tau_and_claws(annotations: Path) -> dict:
    from aa_commons import ActionRecord
    from eval.catalog_replay import runtime
    if sha256(RECORDS) != RECORDS_SHA256:
        raise SystemExit(f"{RECORDS.relative_to(REPO)} is missing or differs from the frozen capture")
    captures = [json.loads(line) for line in gzip.decompress(RECORDS.read_bytes()).splitlines() if line]
    reference = labels(annotations)
    if len(captures) != 388 or {c["task"] for c in captures} != set(reference):
        raise SystemExit("captured runs and reference labels do not cover the same 388 runs")
    check_tau_records(captures)
    runtime.ensure_registered()
    cells = Counter()
    for number, capture in enumerate(captures, 1):
        records = [ActionRecord.from_dict(record) for record in capture["records"]]
        result = runtime.verify_case(records, capture["benchmark"])
        fired = any(fire["arm"] not in EXCLUDED_ARMS for fire in result["fires"])
        bench, model, label = reference[capture["task"]]
        if label not in ("violation", "no_violation"):
            raise SystemExit(f"unknown label {label!r} for {capture['task']}")
        cells[bench, model, label, fired] += 1
        if number % 100 == 0 or number == len(captures):
            print(f"Evaluated {number}/{len(captures)} tau3-bench and ClawsBench runs", flush=True)
    return {(bench, model): (cells[bench, model, "violation", True] + cells[bench, model, "violation", False],
                             cells[bench, model, "violation", True],
                             (cells[bench, model, "no_violation", True],
                              cells[bench, model, "no_violation", True] + cells[bench, model, "no_violation", False]))
            for bench, model in {(b, m) for b, m, _, _ in cells}}


def print_table(result: dict) -> bool:
    ok = True
    header = f"{'Benchmark':<11} {'Model':<14} {'Violations':>10} {'Detected':>9} {'False alarms':>13}   Paper"
    print("\n" + header + "\n" + "-" * len(header))
    total = [0, 0, 0, 0]
    for key, target in PAPER_TABLE.items():
        violations, detected, (alarms, clean) = result[key]
        total = [total[0] + violations, total[1] + detected, total[2] + alarms, total[3] + clean]
        got = (violations, detected, (alarms, clean))
        ok &= got == target
        model = MODELS.get(key[1], "GLM-5.2")
        print(f"{key[0]:<11} {model:<14} {violations:>10} {detected:>9} {f'{alarms}/{clean}':>13}   "
              f"{target[0]}, {target[1]}, {target[2][0]}/{target[2][1]} {'match' if got == target else 'DIFFERS'}")
    all_ok = total == [696, 294, 18, 313]
    ok &= all_ok
    print(f"{'All':<26} {total[0]:>10} {total[1]:>9} {f'{total[2]}/{total[3]}':>13}   "
          f"696, 294, 18/313 {'match' if all_ok else 'DIFFERS'}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--annotations", type=Path, default=ANNOTATIONS,
                        help="root holding tau/ and clawsbench/ labels (default: eval/data/labels)")
    parser.add_argument("--work", type=Path,
                        help="empty directory outside the repository for intermediate AgentDojo outputs")
    args = parser.parse_args()
    if not (PAPER / "corpus").is_dir():
        raise SystemExit("restore the corpus first: python -m eval.run fetch --archive eval/data/runs/paper_main_v1-corpus.tar.gz")
    work = args.work or Path(tempfile.mkdtemp(prefix="table1-"))
    keep = args.work is not None
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise SystemExit(f"--work must be empty: {work}")
    result = tau_and_claws(args.annotations.resolve())
    result.update(agentdojo(work.resolve()))
    ok = print_table(result)
    if keep:
        print(f"Intermediate AgentDojo outputs: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
