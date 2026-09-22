"""Build the annotation-disagreement cases served at /disagreements.

Joins the two human label exports (read-only inputs kept outside the repo)
with the committee ledgers and consensus for the fixed list of cases named in
disagreements_overrides.json, resolving every cited rule to the text the
annotators saw: a Tau policy segment or a ClawsBench AGENTS.md rule. The same
overrides file carries display-only condensed rationales for the human cards;
the raw exports are never edited, and an override that names a rule its party
did not cite fails the build. Every output object is built from a whitelist of
fields, so nothing beyond verdicts, citations, rationales, and the policy text
reaches the viewer, and the anonymized Tau runs stay anonymized; a final token
sweep fails the build if a blinding term slips in.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from eval.labeling import committee

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = REPO_ROOT / "eval" / "viewer" / "app" / "server-data" / "disagreements" / "cases.json"
OVERRIDES_PATH = Path(__file__).resolve().parent / "disagreements_overrides.json"
SCHEMA_VERSION = 2

HUMANS = (("human_a", "Human A"), ("human_b", "Human B"))
MODELS = (
    ("fable", "Fable 5", "anthropic/claude-fable-5"),
    ("sol", "GPT 5.6 Sol", "openai/gpt-5.6-sol"),
    ("kimi", "Kimi K3", "moonshotai/kimi-k3"),
)
PARTIES = tuple((key, name) for key, name in HUMANS) + tuple((key, name) for key, name, _ in MODELS)
BLINDING_TOKENS = ("reward", "glm", "qwen")
TRACE_ENDPOINTS = {"tau": "/api/tau/task/{task}", "clawsbench": "/api/clawsbench/task/{task}"}
REASON_KEY = "_reason"
OMIT_KEY = "_omit_rules"  # display-only: drop these cited rules from a party's card


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


# --- overrides ---------------------------------------------------------------


def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, Any]:
    """The overrides file as written: a `cases` list plus `rationales`."""

    return _read_json(path)


def case_list(overrides: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """(bench, task) pairs from the overrides' "bench:task" entries, in order."""

    cases = []
    for entry in overrides.get("cases") or []:
        bench, _, task = str(entry).partition(":")
        if bench not in TRACE_ENDPOINTS or not task:
            raise ValueError(f"malformed case entry in overrides: {entry!r}")
        cases.append((bench, task))
    if not cases:
        raise ValueError("the overrides name no cases")
    return tuple(cases)


def _check_override_targets(rationales: dict[str, Any], cases: tuple[tuple[str, str], ...]) -> None:
    """Every override must point at a case and a human party, so a stale or
    mistyped entry cannot be skipped silently."""

    known = set(cases)
    humans = {key for key, _ in HUMANS}
    for bench, tasks in rationales.items():
        if bench not in TRACE_ENDPOINTS:
            raise ValueError(f"overrides name an unknown benchmark {bench!r}")
        for task, parties in (tasks or {}).items():
            if (bench, task) not in known:
                raise ValueError(f"overrides name {bench}:{task}, which is not one of the cases")
            for party in parties or {}:
                if party not in humans:
                    raise ValueError(f"overrides name party {party!r} on {task}; only the human cards are condensed")


CASES = case_list(load_overrides())


# --- rule text ---------------------------------------------------------------


def tau_rule_texts(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Segment id -> sentence text and heading, keyed in policy order."""

    rules: dict[str, dict[str, Any]] = {}
    for order, segment in enumerate(manifest["policy"]["segments"]):
        rules[segment["id"]] = {
            "rule_text": segment["text"],
            "heading": segment.get("heading") or "",
            "order": order,
        }
    return rules


def tau_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    """The whole policy as the page renders it: id, heading, text per sentence."""

    return {
        "segments": [
            {"id": segment["id"], "heading": segment.get("heading") or "", "text": segment["text"]}
            for segment in manifest["policy"]["segments"]
        ]
    }


def claws_rule_texts(meta_text: str) -> dict[str, dict[str, Any]]:
    """Rule id -> title plus body, parsed the way the viewer's metaRules() does.

    Level-2 sections named in RULE_SECTIONS carry level-3 "N. Title" items;
    a rule's body is everything under its heading up to the next heading or
    horizontal rule. rule_text keeps the title on its first line so a reader
    can split title from body on the first blank line; title and body are
    also carried separately for the full rule list.
    """

    rules: dict[str, dict[str, Any]] = {}
    tag = None
    current: dict[str, Any] | None = None
    fenced = False
    for line in meta_text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            if current is not None:
                current["lines"].append(line)
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            tag = next((t for t, pattern in committee.RULE_SECTIONS if pattern.match(heading)), None)
            current = None
            continue
        item = re.match(r"^###\s+(\d+)\.\s*(.+?)\s*$", line)
        if item:
            current = None
            if tag:
                current = {"title": item.group(2), "lines": []}
                rules[f"{tag}{item.group(1)}"] = current
            continue
        if line.startswith("#") or line.strip() == "---":
            current = None
            continue
        if current is not None:
            current["lines"].append(line)
    resolved: dict[str, dict[str, Any]] = {}
    for order, (rule_id, rule) in enumerate(rules.items()):
        body = "\n".join(rule["lines"]).strip()
        resolved[rule_id] = {
            "rule_text": f"{rule['title']}\n\n{body}" if body else rule["title"],
            "title": rule["title"],
            "body": body,
            "order": order,
        }
    return resolved


def claws_rules(rules: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The full S/E rule list for the page, in AGENTS.md order."""

    return [
        {"id": rule_id, "title": rule["title"], "body": rule["body"]}
        for rule_id, rule in sorted(rules.items(), key=lambda item: item[1]["order"])
    ]


def _resolve(rules: dict[str, dict[str, Any]], rule_id: str, task: str, party: str) -> str:
    if rule_id not in rules:
        raise ValueError(f"{party} cites rule {rule_id!r} on {task}, which the manifest does not carry")
    return rules[rule_id]["rule_text"]


# --- annotations -------------------------------------------------------------


def step_indexes(values: Any) -> list[int]:
    """Sorted, deduplicated step indexes from ints or {index: ...} refs."""

    indexes = set()
    for value in values or []:
        index = value.get("index") if isinstance(value, dict) else value
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f"step reference without an integer index: {value!r}")
        indexes.add(index)
    return sorted(indexes)


def human_annotation(
    export: dict[str, Any],
    task: str,
    rules: dict[str, dict[str, Any]],
    name: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One human card: verdict, violations with display rationales, reason.

    `overrides` is this party's entry for the task: rule id -> condensed
    rationale, plus an optional `_reason` line shown when the verdict is not a
    violation. Without an override the raw rationale is kept and marked as
    such; a party whose verdict is "unsure" and has no `_reason` falls back to
    the labeler's own note. Notes are not emitted otherwise.
    """

    label = (export.get("labels") or {}).get(task)
    if label is None:
        raise ValueError(f"{name} has no label for {task}")
    overrides = dict(overrides or {})
    reason_override = overrides.pop(REASON_KEY, None)
    omit = set(overrides.pop(OMIT_KEY, None) or [])
    cited = {violation.get("rule") or "" for violation in label.get("violations") or []}
    if omit - cited:
        raise ValueError(f"{name} omits rules on {task} the label does not cite: {', '.join(sorted(omit - cited))}")
    used: set[str] = set()
    violations = []
    for violation in label.get("violations") or []:
        rule_id = violation.get("rule") or ""
        if rule_id in omit:
            continue
        condensed = overrides.get(rule_id)
        if condensed is not None:
            used.add(rule_id)
        entry = {
            "rule": rule_id,
            "rule_text": _resolve(rules, rule_id, task, name),
            "steps": step_indexes(violation.get("steps")),
            "confidence": violation.get("confidence") or "",
            "rationale": condensed if condensed is not None else (violation.get("rationale") or ""),
            "rationale_source": "condensed" if condensed is not None else "raw",
        }
        if violation.get("outcome"):
            entry["outcome"] = violation["outcome"]
        violations.append(entry)
    stale = sorted(set(overrides) - used)
    if stale:
        raise ValueError(f"{name} overrides on {task} name rules the label does not cite: {', '.join(stale)}")

    verdict = label.get("verdict") or ""
    if verdict == "violation":
        if reason_override is not None:
            raise ValueError(f"{name} has a {REASON_KEY} override on {task}, but that verdict is a violation")
        reason = None
    elif reason_override is not None:
        reason = reason_override
    elif verdict == "unsure":
        reason = label.get("notes") or None
    else:
        reason = None
    return {"verdict": verdict, "violations": violations, "reason": reason}


def model_annotation(row: dict[str, Any] | None, task: str, rules: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    if row is None or row.get("status") != "ok":
        raise ValueError(f"{name} has no ok ledger row for {task}")
    violations = [
        {
            "rule": violation["rule_id"],
            "rule_text": _resolve(rules, violation["rule_id"], task, name),
            "steps": step_indexes(violation.get("step_indexes")),
            "confidence": violation.get("confidence") or "",
            "rationale": violation.get("rationale") or "",
        }
        for violation in row.get("violations") or []
    ]
    return {"verdict": row["verdict"], "violations": violations}


def _cited_steps(parties: dict[str, dict[str, Any]]) -> dict[str, list[int]]:
    return {
        key: sorted({step for violation in annotation["violations"] for step in violation["steps"]})
        for key, annotation in parties.items()
    }


def _cited_rules(parties: dict[str, dict[str, Any]], rules: dict[str, dict[str, Any]], bench: str) -> list[dict[str, Any]]:
    names = dict(PARTIES)
    cited: dict[str, list[str]] = {}
    for key, annotation in parties.items():
        for violation in annotation["violations"]:
            citers = cited.setdefault(violation["rule"], [])
            if names[key] not in citers:
                citers.append(names[key])
    entries = []
    for rule_id in sorted(cited, key=lambda rule: rules[rule]["order"]):
        entry = {"rule": rule_id, "rule_text": rules[rule_id]["rule_text"], "cited_by": cited[rule_id]}
        if bench == "tau":
            entry["heading"] = rules[rule_id]["heading"]
        entries.append(entry)
    return entries


# --- assembly ----------------------------------------------------------------


def _ledger_rows(ledgers: Path, bench: str) -> dict[str, dict[str, dict[str, Any]]]:
    rows: dict[str, dict[str, dict[str, Any]]] = {}
    for _, _, model in MODELS:
        ledger = ledgers / bench / f"{committee._sanitize_slug(model)}.jsonl"
        by_task: dict[str, dict[str, Any]] = {}
        for line in ledger.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                by_task[row["task"]] = row
        rows[model] = by_task
    return rows


def _load_export(path: Path, bench: str, name: str) -> dict[str, Any]:
    export = _read_json(path)
    if export.get("benchmark") != bench:
        raise ValueError(f"{path} is a {export.get('benchmark')!r} export, expected {bench!r} for {name}")
    return export


def build_cases(
    exports: dict[str, dict[str, Path]],
    tau_data: Path = committee.TAU_DATA,
    claws_tasks: Path = committee.CLAWS_TASKS_DIR,
    ledgers: Path = committee.LEDGERS_DIR,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """exports maps labeler key -> {bench -> export path}; overrides defaults
    to the tracked disagreements_overrides.json."""

    if overrides is None:
        overrides = load_overrides()
    cases_wanted = case_list(overrides)
    rationales = overrides.get("rationales") or {}
    _check_override_targets(rationales, cases_wanted)

    names = dict(HUMANS)
    loaded = {
        (key, bench): _load_export(path, bench, names[key])
        for key, per_bench in exports.items()
        for bench, path in per_bench.items()
    }
    rows = {bench: _ledger_rows(ledgers, bench) for bench in TRACE_ENDPOINTS}
    consensus = {bench: _read_json(ledgers / bench / "consensus.json")["tasks"] for bench in TRACE_ENDPOINTS}

    cases = []
    for order, (bench, task) in enumerate(cases_wanted, start=1):
        case: dict[str, Any] = {
            "id": f"{bench}-{task}",
            "order": order,
            "benchmark": bench,
            "task": task,
            "trace_endpoint": TRACE_ENDPOINTS[bench].format(task=task),
        }
        if bench == "tau":
            manifest = _read_json(tau_data / "tasks" / f"{task}.json")
            rules = tau_rule_texts(manifest)
            case["domain"] = manifest["domain"]
            case["policy"] = tau_policy(manifest)
        else:
            manifest = _read_json(claws_tasks / f"{task}.json")
            rules = claws_rule_texts(manifest["canonical"]["instructions"]["bootstrap"]["AGENTS.md"])
            case["rules"] = claws_rules(rules)

        task_overrides = (rationales.get(bench) or {}).get(task) or {}
        parties: dict[str, dict[str, Any]] = {}
        for key, name in HUMANS:
            parties[key] = human_annotation(loaded[(key, bench)], task, rules, name, task_overrides.get(key))
        models: dict[str, dict[str, Any]] = {}
        for key, name, model in MODELS:
            models[key] = model_annotation(rows[bench][model].get(task), task, rules, name)
            parties[key] = models[key]
        majority = (consensus[bench].get(task) or {}).get("verdict")

        case["annotations"] = {
            "human_a": parties["human_a"],
            "human_b": parties["human_b"],
            "committee": {"majority": majority or "no majority", "models": models},
        }
        case["cited_steps"] = _cited_steps(parties)
        case["cited_rules"] = _cited_rules(parties, rules, bench)
        cases.append(case)

    payload = {"schema_version": SCHEMA_VERSION, "cases": cases}
    leaked = [token for token in BLINDING_TOKENS if token in serialize(payload).lower()]
    if leaked:
        raise ValueError(f"blinding tokens present in output: {', '.join(leaked)}")
    return payload


def serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--human_a-tau", type=Path, required=True)
    parser.add_argument("--human_b-tau", type=Path, required=True)
    parser.add_argument("--human_a-claws", type=Path, required=True)
    parser.add_argument("--human_b-claws", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_PATH)
    parser.add_argument("--tau-data", type=Path, default=committee.TAU_DATA)
    parser.add_argument("--claws-tasks", type=Path, default=committee.CLAWS_TASKS_DIR)
    parser.add_argument("--ledgers", type=Path, default=committee.LEDGERS_DIR)
    args = parser.parse_args(argv)

    exports = {
        "human_a": {"tau": args.human_a_tau, "clawsbench": args.human_a_claws},
        "human_b": {"tau": args.human_b_tau, "clawsbench": args.human_b_claws},
    }
    payload = build_cases(exports, args.tau_data, args.claws_tasks, args.ledgers, load_overrides(args.overrides))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(serialize(payload))
    print(f"wrote {len(payload['cases'])} cases to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
