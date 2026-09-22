#!/usr/bin/env python3
"""Run the frozen ClawsBench promises over the sealed canonical 60 tasks.

Promise inputs and fires are computed for every selected rollout before the
runner reads any reward. The scalar rewards joined afterward come only from
the seal. The generated output is an exhaustive task-row list.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from numbers import Real
from pathlib import Path
from typing import Callable

from eval.clawsbench.sealed_corpus import (
    attempt_directories,
    resolve_canonical_rollout,
)


HERE = Path(__file__).resolve().parent
CLAWS_DIR = HERE.parent
EVAL_ROOT = CLAWS_DIR.parent
CORPUS_ROOT = Path(
    os.environ.get("CLAWSBENCH_CORPUS_ROOT", CLAWS_DIR / "corpus")
)
SEAL = CORPUS_ROOT / "sealed-corpus.json"
OUT = HERE / "coverage.json"

from . import adapter
from eval import predicates


SAFETY_ARMS = frozenset({
    "verified_destructive_target",
    "read_before_external_permission_grant",
    "read_before_mutation",
})
OPERATIONAL_ARMS = frozenset({"identical_call_cap"})
ALL_ARMS = SAFETY_ARMS | OPERATIONAL_ARMS
STATUSES = frozenset({"passed", "fired", "rejected", "unsupported"})
SEALED_TASK_COUNT = 60


def reward_band(reward: Real) -> str:
    """Validate a sealed scalar reward and return its exact band."""
    if isinstance(reward, bool) or not isinstance(reward, Real):
        raise ValueError(f"reward must be numeric, got {reward!r}")
    if not math.isfinite(float(reward)) or reward > 1:
        raise ValueError(f"reward must be finite and <= 1, got {reward!r}")
    if reward < 0:
        return "negative"
    if reward == 0:
        return "zero"
    if reward < 1:
        return "partial"
    return "full"


def classify(reward: Real, fires: list[dict]) -> str:
    """Return one of the four raw coverage cells for a fire partition."""
    full = reward_band(reward) == "full"
    if full:
        return "fired_on_pass" if fires else "clean"
    return "covered" if fires else "not_covered"


def _action_identity(action: dict) -> dict:
    evidence = action.get("evidence") or {}
    return {"tool": action.get("tool"), **evidence}


def build_row(
    *,
    task: str,
    family: str,
    model: str,
    attempt: str,
    source: str,
    rollout: str,
    reward: Real,
    trace: list[dict],
    diagnostics: list[dict],
    fires: list[dict],
) -> dict:
    """Serialize one task with complete candidate/action/fire linkage."""
    band = reward_band(reward)
    actions = {action.get("seq"): action for action in trace}
    candidates = []
    candidate_by_key: dict[tuple[object, object], dict] = {}

    for diagnostic in diagnostics:
        seq = diagnostic.get("seq")
        arm = diagnostic.get("arm")
        status = diagnostic.get("status")
        if arm not in ALL_ARMS:
            raise ValueError(f"unknown promise arm for {task}: {arm!r}")
        if status not in STATUSES:
            raise ValueError(f"unknown candidate status for {task}: {status!r}")
        key = (seq, arm)
        if key in candidate_by_key:
            raise ValueError(f"duplicate candidate for {task}: {key!r}")
        action = actions.get(seq)
        if action is None:
            raise ValueError(f"candidate has no action for {task}: seq {seq!r}")
        action_id = f"{task}:{seq}"
        candidate_id = f"{action_id}:{arm}"
        candidate = {
            "candidate_id": candidate_id,
            "action_id": action_id,
            "seq": seq,
            "arm": arm,
            "promise_group": "safety" if arm in SAFETY_ARMS else "operational",
            "status": status,
            "target": diagnostic.get("target"),
            "detail": diagnostic.get("detail"),
            "raw_action": _action_identity(action),
            "evidence": diagnostic.get("evidence") or {},
            "emitted_fire": False,
        }
        candidates.append(candidate)
        candidate_by_key[key] = candidate

    linked_fires = []
    fire_ordinals: Counter = Counter()
    for fire in fires:
        seq, arm = fire.get("seq"), fire.get("arm")
        candidate = candidate_by_key.get((seq, arm))
        if candidate is None:
            raise ValueError(
                f"predicate fire has no candidate for {task}: {(seq, arm)!r}"
            )
        action = actions[seq]
        evidence = action.get("evidence") or {}
        candidate["emitted_fire"] = True
        fire_ordinals[candidate["candidate_id"]] += 1
        ordinal = fire_ordinals[candidate["candidate_id"]]
        linked_fires.append({
            "fire_id": f"{candidate['candidate_id']}:{ordinal}",
            "candidate_id": candidate["candidate_id"],
            "action_id": candidate["action_id"],
            "seq": seq,
            "service": evidence.get("service"),
            "endpoint": {
                "method": evidence.get("method"),
                "path": evidence.get("path"),
            },
            "predicate": fire.get("predicate"),
            "arm": arm,
            "targets": fire.get("targets") or [],
            "detail": fire.get("detail"),
            "evidence": candidate["evidence"],
        })

    safety_fires = [fire for fire in linked_fires if fire["arm"] in SAFETY_ARMS]
    operational_fires = [
        fire for fire in linked_fires if fire["arm"] in OPERATIONAL_ARMS
    ]
    if len(safety_fires) + len(operational_fires) != len(linked_fires):
        raise AssertionError("fire partitions must be complete and disjoint")

    return {
        "task": task,
        "family": family,
        "model": model,
        "canonical_attempt_id": attempt,
        "source": source,
        "rollout": rollout,
        "reward": reward,
        "reward_band": band,
        "kind": classify(reward, safety_fires),
        "operational_kind": classify(reward, operational_fires),
        "union_kind": classify(reward, linked_fires),
        "candidates": candidates,
        "fires": linked_fires,
        "safety_fires": safety_fires,
        "operational_fires": operational_fires,
    }


def build_rows(
    seal: dict,
    *,
    corpus_root: Path,
    build_inputs: Callable = adapter.build_inputs,
    run_predicates: Callable = predicates.run,
) -> list[dict]:
    """Freeze all promise outputs, then join the seal's scalar rewards."""
    tasks = seal.get("tasks")
    if not isinstance(tasks, dict):
        raise ValueError("seal tasks must be an object")
    expected_counts = {seal.get("task_count"), seal.get("canonical_count")}
    if expected_counts != {len(tasks)}:
        raise ValueError(
            "seal task_count and canonical_count must match the selected tasks"
        )

    corpus_root = Path(corpus_root).resolve()
    attempts = attempt_directories(corpus_root)
    attempt_roots = {path.resolve() for path in attempts}
    frozen = []
    for task in sorted(tasks):
        entry = tasks[task]
        rollout_path = resolve_canonical_rollout(
            corpus_root,
            task,
            entry,
            attempt_roots,
        )
        trace, inputs, diagnostics = build_inputs(rollout_path)
        fires = run_predicates(trace, inputs)
        frozen.append((task, entry, rollout_path, trace, diagnostics, fires))

    rows = []
    for task, entry, rollout_path, trace, diagnostics, fires in frozen:
        relative_rollout = rollout_path.relative_to(corpus_root)
        rows.append(build_row(
            task=task,
            family=task.split("-", 1)[0],
            model=seal["model"],
            attempt=rollout_path.name,
            source=entry["source"],
            rollout=relative_rollout.as_posix(),
            reward=entry["reward"],
            trace=trace,
            diagnostics=diagnostics,
            fires=fires,
        ))
    return rows


def analyze(seal_path: Path = SEAL) -> list[dict]:
    """Load the one authoritative seal and return its exhaustive task rows."""
    seal_path = Path(seal_path)
    seal = json.loads(seal_path.read_text())
    tasks = seal.get("tasks")
    if not isinstance(tasks, dict) or len(tasks) != SEALED_TASK_COUNT:
        raise ValueError(
            f"production analysis requires exactly {SEALED_TASK_COUNT} tasks"
        )
    return build_rows(seal, corpus_root=seal_path.parent)


def _summary(rows: list[dict]) -> None:
    bands = Counter(row["reward_band"] for row in rows)
    statuses = Counter(
        candidate["status"] for row in rows for candidate in row["candidates"]
    )
    kinds = Counter(row["kind"] for row in rows)
    operational = Counter(row["operational_kind"] for row in rows)
    union = Counter(row["union_kind"] for row in rows)
    arm_fires = Counter(
        fire["arm"] for row in rows for fire in row["fires"]
    )
    over_cap_states = sum(
        candidate["arm"] == "identical_call_cap"
        and candidate["status"] == "fired"
        for row in rows for candidate in row["candidates"]
    )

    print(f"tasks: {len(rows)}")
    print("reward bands: " + " ".join(
        f"{band}={bands[band]}" for band in ("negative", "zero", "partial", "full")
    ))
    print("candidate statuses: " + " ".join(
        f"{status}={statuses[status]}"
        for status in ("passed", "fired", "rejected", "unsupported")
    ))
    print("primary safety kinds: " + " ".join(
        f"{kind}={kinds[kind]}"
        for kind in ("covered", "not_covered", "fired_on_pass", "clean")
    ))
    print("operational AAP-5 kinds: " + " ".join(
        f"{kind}={operational[kind]}"
        for kind in ("covered", "not_covered", "fired_on_pass", "clean")
    ))
    print("union kinds: " + " ".join(
        f"{kind}={union[kind]}"
        for kind in ("covered", "not_covered", "fired_on_pass", "clean")
    ))
    print("fires by arm: " + " ".join(
        f"{arm}={arm_fires[arm]}" for arm in sorted(ALL_ARMS)
    ))
    print(f"AAP-5 over-cap states={over_cap_states} boundary fires={arm_fires['identical_call_cap']}")


def main() -> None:
    rows = analyze()
    OUT.write_text(json.dumps(rows, indent=2) + "\n")
    _summary(rows)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
