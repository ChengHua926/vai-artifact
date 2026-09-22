"""Replay Tau gold and captured actions while retaining per-call DB deltas."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .effects import attribute_terminal_effects, logical_diff


PINNED_TAU_COMMIT = "7ac89f5128bc9ff86e37cf49a54687b35083e598"
_VERIFIED_TAU_ROOTS: set[Path] = set()


def _ensure_tau_runtime(tau_root: Path) -> None:
    # Importing Tau's package initializer imports LiteLLM even though replay uses
    # no model. Force bundled metadata so this evaluator remains offline.
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS"] = "True"
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    os.environ.setdefault("LOGURU_LEVEL", "ERROR")
    root = Path(tau_root).resolve(strict=True)
    source = root / "src"
    if not source.is_dir():
        raise ValueError(f"Tau source directory is missing: {source}")
    loaded_tau = sys.modules.get("tau2")
    loaded_file = getattr(loaded_tau, "__file__", None)
    if loaded_file is not None and not Path(loaded_file).resolve().is_relative_to(source):
        raise ValueError(f"Tau is already loaded from a different checkout: {loaded_file}")
    if root not in _VERIFIED_TAU_ROOTS:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        if completed.stdout.strip() != PINNED_TAU_COMMIT:
            raise ValueError(
                f"Tau checkout is not pinned at {PINNED_TAU_COMMIT}: {completed.stdout.strip()}"
            )
        dirty = subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--"],
            check=False,
        )
        if dirty.returncode != 0:
            raise ValueError(f"Tau checkout has tracked changes: {root}")
        _VERIFIED_TAU_ROOTS.add(root)
    source_text = str(source)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    for site_packages in sorted((root / ".venv" / "lib").glob("python*/site-packages")):
        site_text = str(site_packages)
        if site_text not in sys.path:
            sys.path.append(site_text)


def _snapshot(environment: Any) -> dict[str, Any]:
    def dump(toolkit: Any) -> Any:
        if toolkit is None or getattr(toolkit, "db", None) is None:
            return None
        return toolkit.db.model_dump(mode="json")

    return {"agent": dump(environment.tools), "user": dump(environment.user_tools)}


def _initial_state(task: Any) -> tuple[Any, Any, list[Any]]:
    initial = task.initial_state
    if initial is None:
        return None, None, []
    return (
        initial.initialization_data,
        initial.initialization_actions,
        list(initial.message_history or []),
    )


def _new_environment(domain: str, task: Any) -> Any:
    if domain == "airline":
        from tau2.domains.airline.environment import get_environment
    elif domain == "retail":
        from tau2.domains.retail.environment import get_environment
    else:
        raise ValueError(f"unsupported Tau domain: {domain}")
    environment = get_environment()
    data, actions, history = _initial_state(task)
    environment.set_state(data, actions, history)
    return environment


def _content_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _content_equal(left: Any, right: Any) -> bool:
    return _content_value(left) == _content_value(right)


def classify_effect_shape(
    native_reward: float,
    db_match: bool,
    terminal_effects: list[dict[str, Any]],
    replay_errors: list[str],
) -> str | None:
    """Separate response-only failure, omission, commission, and mixtures."""

    if replay_errors:
        return "unknown"
    if native_reward != 0.0:
        return None
    if db_match:
        return "response_only"
    has_writer = any(effect.get("writer_seq") is not None for effect in terminal_effects)
    has_omission = any(effect.get("writer_seq") is None for effect in terminal_effects)
    if has_writer and has_omission:
        return "mixed"
    if has_writer:
        return "commission_only"
    return "omission_only"


def replay_case(case: dict[str, Any], *, tau_root: Path) -> dict[str, Any]:
    """Mirror Tau's native DB evaluator and expose actual last-writer evidence."""

    _ensure_tau_runtime(tau_root)
    from loguru import logger
    from tau2.data_model.message import ToolCall
    from tau2.data_model.tasks import Task

    logger.disable("tau2")
    task = Task.model_validate(case["task"])
    gold_warnings: list[str] = []
    replay_errors: list[str] = []

    gold_environment = _new_environment(case["domain"], task)
    for action in task.evaluation_criteria.actions or []:
        try:
            gold_environment.make_tool_call(
                tool_name=action.name,
                requestor=action.requestor,
                **action.arguments,
            )
        except Exception as error:  # The native evaluator logs and continues too.
            gold_warnings.append(f"{action.name}: {type(error).__name__}: {error}")
    gold_state = _snapshot(gold_environment)

    actual_environment = _new_environment(case["domain"], task)
    calls: list[dict[str, Any]] = []
    mutating_deltas: list[dict[str, Any]] = []
    for captured in case["trace"]:
        seq = captured["seq"]
        tool = captured.get("tool")
        captured_error = captured.get("captured_error")
        captured_accepted = (
            not captured_error
            if isinstance(captured_error, bool)
            else not str(captured.get("result") or "").startswith("Error")
        )
        before = _snapshot(actual_environment)
        record: dict[str, Any] = {
            "seq": seq,
            "tool": tool,
            "args": captured.get("args") or {},
            "requestor": captured.get("requestor") or "assistant",
            "tool_call_id": captured.get("tool_call_id"),
            "captured_result": captured.get("result"),
            "captured_accepted": captured_accepted,
            "mutating": False,
            "accepted": captured_accepted,
            "status": None,
            "result_match": True,
            "replayed_result": None,
            "changes": [],
            "before_state": before,
            "after_state": before,
        }
        if not actual_environment._has_tool(tool):
            record["status"] = "unknown_tool_noop"
            record["accepted"] = False
            if captured_accepted:
                record["result_match"] = False
                replay_errors.append(f"seq {seq} {tool}: captured unknown tool as successful")
        elif not actual_environment._is_mutating_tool(tool):
            record["status"] = "nonmutating_skipped"
        else:
            record["mutating"] = True
            response = actual_environment.get_response(
                ToolCall(
                    id=f"tau-eval-{seq}",
                    name=tool,
                    arguments=record["args"],
                    requestor=record["requestor"],
                )
            )
            after = _snapshot(actual_environment)
            record["accepted"] = not response.error
            record["status"] = "accepted" if record["accepted"] else "rejected"
            record["replayed_result"] = response.content
            record["result_match"] = _content_equal(response.content, captured.get("result"))
            record["changes"] = logical_diff(before, after)
            record["after_state"] = after
            if record["accepted"] != captured_accepted:
                replay_errors.append(
                    f"seq {seq} {tool}: replay acceptance {record['accepted']} "
                    f"!= captured {captured_accepted}"
                )
            if not record["result_match"]:
                replay_errors.append(f"seq {seq} {tool}: replayed result differs from capture")
            mutating_deltas.append(record)
        calls.append(record)

    actual_state = _snapshot(actual_environment)
    terminal_effects = attribute_terminal_effects(gold_state, actual_state, mutating_deltas)
    computed_db_match = not terminal_effects
    if computed_db_match != case["recorded_db_match"]:
        replay_errors.append(
            f"computed db_match={computed_db_match} "
            f"!= recorded db_match={case['recorded_db_match']}"
        )
    return {
        "computed_db_match": computed_db_match,
        "recorded_db_match": case["recorded_db_match"],
        "replay_errors": replay_errors,
        "gold_warnings": gold_warnings,
        "gold_state": gold_state,
        "actual_state": actual_state,
        "calls": calls,
        "terminal_effects": terminal_effects,
        "effect_shape": classify_effect_shape(
            case["native_reward"], computed_db_match, terminal_effects, replay_errors
        ),
    }
