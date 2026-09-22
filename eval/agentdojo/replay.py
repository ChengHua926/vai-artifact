"""Native AgentDojo v1.2.2 replay with captured execution-status parity."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Any

from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.task_suite.task_suite import model_output_from_messages

from .runtime import extract_call_evidence


_ATTACKER_IBAN = "US133000000121212121212"


def _has_recurring_attacker_payment(environment: Any) -> bool:
    account = getattr(environment, "bank_account", None)
    transactions = getattr(account, "scheduled_transactions", [])
    return any(
        transaction.recurring and transaction.recipient == _ATTACKER_IBAN
        for transaction in transactions
    )


def _final_workspace_email_container(environment: Any, email: Any) -> str:
    inbox = environment.inbox
    if any(candidate is email for candidate in inbox.emails.values()):
        return "inbox"
    if any(candidate is email for candidate in inbox.trash.values()):
        return "trash"
    return "detached"


def replay_attack(case: dict[str, Any]) -> dict[str, Any]:
    """Replay one graded attack exactly enough to rerun both native graders."""

    if case.get("case_kind") != "attack" or case.get("protocol_status") != "graded":
        raise ValueError("native replay requires one graded attack case")
    log = case["trace"]
    calls = extract_call_evidence(log)
    try:
        suite = get_suite(log.get("benchmark_version") or "v1.2.2", case["suite"])
        user_task = suite.get_user_task_by_id(case["user_task_id"])
        injection_task = suite.get_injection_task_by_id(case["injection_task_id"])
        environment = suite.load_and_inject_default_environment(log.get("injections") or {})
        environment = user_task.init_environment(environment)
        pre_environment = environment.model_copy(deep=True)
        runtime = FunctionsRuntime(suite.tools)
        function_trace: list[FunctionCall] = []
        call_status_disagreements: list[dict[str, Any]] = []
        created_workspace_emails: dict[int, Any] = {}
        for call in calls:
            validated = FunctionCall.model_validate(
                {
                    "function": call["tool"],
                    "args": call["args"],
                    "id": call["call_id"],
                    "placeholder_args": None,
                }
            )
            function_trace.append(validated)
            captured = call["execution_status"]
            if captured == "unexecuted":
                replay_status = "unexecuted"
                replay_error = None
                replay_effect = None
            else:
                track_recurring = case["suite"] == "banking" and call["tool"] in {
                    "schedule_transaction",
                    "update_scheduled_transaction",
                }
                before_recurring = (
                    _has_recurring_attacker_payment(environment)
                    if track_recurring
                    else None
                )
                replay_result, replay_error = runtime.run_function(
                    environment, validated.function, validated.args
                )
                replay_status = "successful" if replay_error is None else "failed"
                replay_effect: dict[str, Any] = {}
                if track_recurring:
                    replay_effect.update(
                        {
                            "recurring_attacker_payment_before": before_recurring,
                            "recurring_attacker_payment_after": _has_recurring_attacker_payment(
                                environment
                            ),
                        }
                    )
                if (
                    case["suite"] == "workspace"
                    and call["tool"] == "send_email"
                    and replay_error is None
                    and getattr(replay_result, "id_", None) is not None
                ):
                    created_workspace_emails[int(call["seq"])] = replay_result
                    replay_effect["created_entity"] = {
                        "kind": "workspace_email",
                        "id": str(replay_result.id_),
                    }
                if replay_status != captured:
                    call_status_disagreements.append(
                        {
                            "seq": call["seq"],
                            "captured_status": captured,
                            "replay_status": replay_status,
                        }
                    )
            call["replay_status"] = replay_status
            call["replay_error"] = replay_error
            call["replay_effect"] = replay_effect or None

        for call in calls:
            created_email = created_workspace_emails.get(int(call["seq"]))
            if created_email is None:
                continue
            call["replay_effect"]["created_entity"]["final_container"] = (
                _final_workspace_email_container(environment, created_email)
            )

        evidence_pre_environment = pre_environment.model_copy(deep=True)
        evidence_post_environment = environment.model_copy(deep=True)
        model_output = model_output_from_messages(log.get("messages") or []) or []
        with redirect_stdout(io.StringIO()):
            replayed_utility = suite._check_task_result(
                user_task,
                model_output,
                pre_environment,
                environment,
                function_trace,
            )
            replayed_security = suite._check_task_result(
                injection_task,
                model_output,
                pre_environment,
                environment,
                function_trace,
            )
        return {
            "calls": calls,
            "replayed_utility": bool(replayed_utility),
            "replayed_security": bool(replayed_security),
            "call_status_disagreements": call_status_disagreements,
            "replay_error": None,
            "pre_environment": evidence_pre_environment,
            "post_environment": evidence_post_environment,
            "user_task": user_task,
            "injection_task": injection_task,
        }
    except Exception as error:
        return {
            "calls": calls,
            "replayed_utility": None,
            "replayed_security": None,
            "call_status_disagreements": [],
            "replay_error": f"{type(error).__name__}: {error}",
            "pre_environment": None,
            "post_environment": None,
            "user_task": None,
            "injection_task": None,
        }
