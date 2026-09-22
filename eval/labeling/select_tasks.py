"""Seeded, stratified selection of the human-calibration labeling sets.

Tau: 30 of the 328 sealed cohort runs, stratified over (model, domain,
native pass/fail) with largest-remainder proportional allocation, sampled
and shuffled by one recorded seed. Runs get anonymized display ids
(run-01..run-30) so labelers never see which model produced a run.

ClawsBench: 30 of the 60 published tasks, plain seeded sample.

The tracked output `selection_v1.json` is the single source of truth for
both sets. `--check` re-derives everything and fails on any difference,
so the file can never drift from the algorithm that claims to produce it.

Extension: the exact complement of the calibration selection -- the 298
tau runs and 30 ClawsBench tasks `selection_v1.json` did not take. Its
own seed (20260909) shuffles the tau runs into display ids ext-001.. and
it records the hash of the v1 file it was derived from. `--extension`
writes `extension/selection.json`; `--extension --check` re-derives it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TAU_CASES = REPO_ROOT / "eval" / "paper_main_v1" / "tau" / "cases.jsonl"
CLAWS_INDEX = (
    REPO_ROOT / "eval" / "viewer" / "app" / "server-data" / "clawsbench" / "data" / "index.json"
)
SELECTION_PATH = Path(__file__).resolve().parent / "selection_v1.json"
CLAWS_LABEL_SET_PATH = (
    REPO_ROOT / "eval" / "viewer" / "app" / "app" / "lib" / "clawsbench-label-set.json"
)

TAU_SEED = 20260824
CLAWS_SEED = 20260824
TAU_TARGET = 30
CLAWS_TARGET = 30

EXT_SEED = 20260909
EXT_DIR = Path(__file__).resolve().parent / "extension"
EXT_SELECTION_PATH = EXT_DIR / "selection.json"
EXT_CREATED = "2026-09-09"


def _tau_rows() -> list[dict]:
    rows = []
    with TAU_CASES.open() as handle:
        for line in handle:
            case = json.loads(line)
            rows.append(
                {
                    "case_id": case["case_id"],
                    "model_id": case["model_id"],
                    "domain": case["domain"],
                    "task_id": str(case["task_id"]),
                    "trial": case["trial"],
                    "native_pass": case["native_reward"] == 1,
                    "source": case["source"],
                }
            )
    return rows


def _largest_remainder(counts: dict[str, int], target: int) -> dict[str, int]:
    total = sum(counts.values())
    quotas = {key: value * target / total for key, value in counts.items()}
    allocation = {key: int(quota) for key, quota in quotas.items()}
    shortfall = target - sum(allocation.values())
    remainders = sorted(
        counts, key=lambda key: (quotas[key] - allocation[key], key), reverse=True
    )
    for key in remainders[:shortfall]:
        allocation[key] += 1
    return allocation


def select_tau() -> dict:
    rows = _tau_rows()
    strata: dict[str, list[dict]] = {}
    for row in rows:
        key = f"{row['model_id']}/{row['domain']}/{'pass' if row['native_pass'] else 'fail'}"
        strata.setdefault(key, []).append(row)
    allocation = _largest_remainder(
        {key: len(members) for key, members in strata.items()}, TAU_TARGET
    )
    rng = random.Random(TAU_SEED)
    chosen: list[dict] = []
    for key in sorted(strata):
        members = sorted(strata[key], key=lambda row: row["case_id"])
        chosen.extend(rng.sample(members, allocation[key]))
    rng.shuffle(chosen)
    runs = []
    for position, row in enumerate(chosen, start=1):
        runs.append({"display_id": f"run-{position:02d}", **row})
    return {
        "seed": TAU_SEED,
        "strata_allocation": {key: allocation[key] for key in sorted(allocation)},
        "runs": runs,
    }


def select_clawsbench() -> dict:
    index = json.loads(CLAWS_INDEX.read_text())
    task_ids = sorted(row["task"] for row in index["tasks"])
    rng = random.Random(CLAWS_SEED)
    tasks = sorted(rng.sample(task_ids, CLAWS_TARGET))
    return {"seed": CLAWS_SEED, "tasks": tasks}


def build_selection() -> dict:
    return {
        "schema_version": 1,
        "created": "2026-08-24",
        "tau": select_tau(),
        "clawsbench": select_clawsbench(),
    }


def build_extension_selection() -> dict:
    """Everything the calibration selection left behind, as its own set."""
    base_text = SELECTION_PATH.read_text()
    base = json.loads(base_text)
    base_sha = hashlib.sha256(base_text.encode()).hexdigest()

    chosen = {row["case_id"] for row in base["tau"]["runs"]}
    rows = [row for row in _tau_rows() if row["case_id"] not in chosen]
    rows.sort(key=lambda row: row["case_id"])
    random.Random(EXT_SEED).shuffle(rows)
    runs = [
        {"display_id": f"ext-{position:03d}", **row}
        for position, row in enumerate(rows, start=1)
    ]

    index = json.loads(CLAWS_INDEX.read_text())
    task_ids = sorted(row["task"] for row in index["tasks"])
    base_tasks = set(base["clawsbench"]["tasks"])
    tasks = sorted(set(task_ids) - base_tasks)

    base_display_ids = {row["display_id"] for row in base["tau"]["runs"]}
    tau_overlap = sorted(base_display_ids & {run["display_id"] for run in runs})
    if tau_overlap:
        raise ValueError(
            f"extension tau display ids collide with the calibration set: {tau_overlap}"
        )
    claws_overlap = sorted(base_tasks & set(tasks))
    if claws_overlap:
        raise ValueError(
            f"extension clawsbench tasks collide with the calibration set: {claws_overlap}"
        )

    return {
        "schema_version": 1,
        "created": EXT_CREATED,
        "derived_from": {
            "file": "eval/labeling/selection_v1.json",
            "sha256": base_sha,
        },
        "tau": {"seed": EXT_SEED, "count": len(runs), "runs": runs},
        "clawsbench": {"count": len(tasks), "tasks": tasks},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="re-derive the selection and fail if the tracked files differ",
    )
    parser.add_argument(
        "--extension",
        action="store_true",
        help=(
            "build or --check the extension selection (complement of "
            "selection_v1) instead of the calibration files"
        ),
    )
    args = parser.parse_args(argv)
    if args.extension:
        ext_text = json.dumps(build_extension_selection(), indent=1, sort_keys=True) + "\n"
        if args.check:
            if not EXT_SELECTION_PATH.exists() or EXT_SELECTION_PATH.read_text() != ext_text:
                print("STALE: " + str(EXT_SELECTION_PATH))
                return 1
            print("OK: extension selection matches the derivation")
            return 0
        EXT_DIR.mkdir(parents=True, exist_ok=True)
        EXT_SELECTION_PATH.write_text(ext_text)
        print(f"wrote {EXT_SELECTION_PATH}")
        return 0
    selection = build_selection()
    selection_text = json.dumps(selection, indent=1, sort_keys=True) + "\n"
    claws_text = (
        json.dumps(
            {"schema_version": 1, "tasks": selection["clawsbench"]["tasks"]},
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    if args.check:
        problems = []
        if not SELECTION_PATH.exists() or SELECTION_PATH.read_text() != selection_text:
            problems.append(str(SELECTION_PATH))
        if not CLAWS_LABEL_SET_PATH.exists() or CLAWS_LABEL_SET_PATH.read_text() != claws_text:
            problems.append(str(CLAWS_LABEL_SET_PATH))
        if problems:
            print("STALE: " + ", ".join(problems))
            return 1
        print("OK: selection files match the derivation")
        return 0
    SELECTION_PATH.write_text(selection_text)
    CLAWS_LABEL_SET_PATH.write_text(claws_text)
    print(f"wrote {SELECTION_PATH}")
    print(f"wrote {CLAWS_LABEL_SET_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
