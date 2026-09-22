"""Build the blinded Tau labeling bundle served by the viewer.

Reads the tracked selection and the sealed corpus shards, and writes
byte-deterministic per-run manifests plus an index under
eval/viewer/app/server-data/tau/data. Every output object is built from
scratch from a fixed whitelist of source fields, so nothing about the
producing checkpoint, grading, or detector behavior can reach labelers;
a final token sweep fails the build if a blinding term slips through.

--result-limit truncates tool results ("none" keeps them whole, the default);
--label and --selection-file name a non-calibration set in index.json. main()
refuses a non-default selection aimed at the tracked calibration directory,
since write_bundle prunes task files the new selection does not name.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from eval.labeling.segment_policy import segment_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTION_PATH = Path(__file__).resolve().parent / "selection_v1.json"
CORPUS_ROOT = REPO_ROOT / "eval" / "paper_main_v1"
OUT_DIR = REPO_ROOT / "eval" / "viewer" / "app" / "server-data" / "tau" / "data"

RESULT_LIMIT = None  # full tool results; the 1000-char cut used during the labeling round is retired
BLINDING_TOKENS = ("reward", "glm", "qwen", "fire")
SCENARIO_SECTIONS = (
    ("reason_for_call", "Reason for call"),
    ("known_info", "Known info"),
    ("unknown_info", "Unknown info"),
    ("task_instructions", "Task instructions"),
)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def render_user_scenario(user_scenario: Any) -> str:
    if not isinstance(user_scenario, dict):
        raise ValueError("Tau task lacks a user_scenario object")
    instructions = user_scenario.get("instructions")
    if not isinstance(instructions, dict):
        raise ValueError("Tau user_scenario lacks an instructions object")
    parts = []
    for key, label in SCENARIO_SECTIONS:
        value = _text(instructions.get(key)).strip()
        if value:
            parts.append(f"{label}:\n{value}")
    if not parts:
        raise ValueError("Tau user_scenario instructions are empty")
    return "\n\n".join(parts)


def build_steps(
    messages: list[dict[str, Any]], result_limit: int | None = RESULT_LIMIT
) -> list[dict[str, Any]]:
    """Frozen positional steps: user/assistant text plus paired tool calls."""

    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        call_id = message.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Tau tool result is missing its call id")
        if call_id in results:
            raise ValueError(f"duplicate Tau tool result id: {call_id}")
        error = message.get("error")
        if not isinstance(error, bool):
            raise ValueError(f"Tau tool result lacks a boolean error flag: {call_id}")
        results[call_id] = {"content": _text(message.get("content")), "error": error}

    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "user":
            steps.append(
                {
                    "index": len(steps),
                    "type": "user_message",
                    "text": _text(message.get("content")),
                }
            )
            continue
        if role != "assistant":
            continue
        content = _text(message.get("content"))
        if content:
            steps.append(
                {"index": len(steps), "type": "assistant_message", "text": content}
            )
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Tau tool call is missing its id")
            if call_id in seen:
                raise ValueError(f"duplicate Tau tool call id: {call_id}")
            if call_id not in results:
                raise ValueError(f"Tau tool call has no result: {call_id}")
            seen.add(call_id)
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError(f"Tau tool arguments must be an object: {call_id}")
            result = results[call_id]["content"]
            steps.append(
                {
                    "index": len(steps),
                    "type": "tool_call",
                    "tool": _text(call.get("name")),
                    "args": dict(arguments),
                    "result": (result if result_limit is None else result[:result_limit]),
                    "result_truncated": result_limit is not None and len(result) > result_limit,
                    "error": results[call_id]["error"],
                    "tool_call_id": call_id,
                }
            )
    orphaned = sorted(set(results) - seen)
    if orphaned:
        raise ValueError(f"Tau tool results have no calls: {orphaned[0]}")
    return steps


def build_run_manifest(
    row: dict[str, Any],
    simulation: dict[str, Any],
    task: dict[str, Any],
    result_limit: int | None = RESULT_LIMIT,
) -> dict[str, Any]:
    policy = simulation.get("policy")
    if not isinstance(policy, str) or not policy.strip():
        raise ValueError(f"Tau simulation lacks a policy: {row['display_id']}")
    return {
        "run": row["display_id"],
        "benchmark": "tau",
        "schema_version": 1,
        "domain": row["domain"],
        "user_scenario": render_user_scenario(task.get("user_scenario")),
        "policy": {"segments": segment_policy(policy, row["domain"])},
        "steps": build_steps(simulation.get("messages") or [], result_limit),
    }


def _find_simulation(payload: dict[str, Any], task_id: str, trial: Any) -> dict[str, Any]:
    matches = [
        simulation
        for simulation in payload.get("simulations") or []
        if isinstance(simulation, dict)
        and str(simulation.get("task_id")) == task_id
        and simulation.get("trial") == trial
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one simulation for task {task_id} trial {trial}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _find_task(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    matches = [
        task
        for task in payload.get("tasks") or []
        if isinstance(task, dict) and str(task.get("id")) == task_id
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one task {task_id}, found {len(matches)}")
    return matches[0]


def _serialize(value: Any) -> str:
    return json.dumps(value, indent=1, sort_keys=True) + "\n"


def build_bundle(
    selection: dict[str, Any],
    corpus_root: Path,
    *,
    result_limit: int | None = RESULT_LIMIT,
    label: str = "Tau labeling set",
    selection_file: str = "eval/labeling/selection_v1.json",
) -> dict[str, str]:
    """Return {relative_path: serialized_json} for the whole bundle."""

    shards: dict[str, dict[str, Any]] = {}
    manifests = []
    for row in selection["tau"]["runs"]:
        source = row["source"]["path"]
        if source not in shards:
            shards[source] = json.loads((corpus_root / source).read_text())
        payload = shards[source]
        simulation = _find_simulation(payload, row["task_id"], row["trial"])
        task = _find_task(payload, row["task_id"])
        manifests.append(build_run_manifest(row, simulation, task, result_limit))
    manifests.sort(key=lambda manifest: manifest["run"])
    bundle = {}
    for manifest in manifests:
        path = f"tasks/{manifest['run']}.json"
        if path in bundle:
            raise ValueError(f"duplicate run id: {manifest['run']}")
        bundle[path] = _serialize(manifest)
    index = {
        "benchmark": "tau",
        "schema_version": 1,
        "label": label,
        "selection": {
            "file": selection_file,
            "count": len(manifests),
        },
        "domains": sorted({manifest["domain"] for manifest in manifests}),
        "runs": [
            {
                "run": manifest["run"],
                "domain": manifest["domain"],
                "steps": len(manifest["steps"]),
                "tool_calls": sum(
                    1 for step in manifest["steps"] if step["type"] == "tool_call"
                ),
            }
            for manifest in manifests
        ],
    }
    bundle["index.json"] = _serialize(index)
    for path, text in bundle.items():
        lowered = text.lower()
        for token in BLINDING_TOKENS:
            if token in lowered:
                raise ValueError(f"blinding token {token!r} in {path}")
    return bundle


def write_bundle(bundle: dict[str, str], out_dir: Path) -> None:
    tasks_dir = out_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    for existing in sorted(tasks_dir.glob("*.json")):
        if f"tasks/{existing.name}" not in bundle:
            existing.unlink()
    for relative, text in sorted(bundle.items()):
        (out_dir / relative).write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=SELECTION_PATH)
    parser.add_argument("--corpus-root", type=Path, default=CORPUS_ROOT)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument(
        "--result-limit",
        type=lambda s: None if s.strip().lower() in ("none", "") else int(s),
        default=RESULT_LIMIT,
    )
    parser.add_argument("--label", default="Tau labeling set")
    parser.add_argument("--selection-file", default=None)
    args = parser.parse_args(argv)
    if (
        args.out.resolve() == OUT_DIR.resolve()
        and args.selection.resolve() != SELECTION_PATH.resolve()
    ):
        print(
            "refusing to write a non-calibration selection into the tracked "
            "calibration bundle directory",
            file=sys.stderr,
        )
        return 2
    selection_file = args.selection_file
    if selection_file is None:
        try:
            selection_file = str(args.selection.resolve().relative_to(REPO_ROOT))
        except ValueError:
            selection_file = str(args.selection)
    selection = json.loads(args.selection.read_text())
    bundle = build_bundle(
        selection,
        args.corpus_root,
        result_limit=args.result_limit,
        label=args.label,
        selection_file=selection_file,
    )
    write_bundle(bundle, args.out)
    total = sum(len(text.encode("utf-8")) for text in bundle.values())
    print(
        f"wrote {len(bundle)} files to {args.out} "
        f"({total} bytes, result_limit={args.result_limit})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
