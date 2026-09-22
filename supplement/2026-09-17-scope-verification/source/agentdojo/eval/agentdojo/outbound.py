"""Versioned outbound observations and complete AAP-4 witnesses.

This adapter does not decide whether a destination is allowed. Every decision is
made by the shipped AAP-4 evaluator, with the frozen authorization parameters.
Calendar observations describe native notification effects, never grants.
"""
from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import yaml
from pydantic import EmailStr, TypeAdapter

from .runtime import Accountability, PREDICATE
from aa_commons import ActionRecord
from aa_commons.predicates.egress_within_allowlist import evaluate
from aa_commons.trace import is_blocked


NOTIFICATION_KEY = "notification_recipients"
CALENDAR_SENDS = {"create_calendar_event", "reschedule_calendar_event", "cancel_calendar_event"}
EXTRA_CONFIG = (
    ("workspace", "send_email", "cc"),
    ("workspace", "send_email", "bcc"),
    ("travel", "send_email", "cc"),
    ("travel", "send_email", "bcc"),
    ("workspace", "create_calendar_event", NOTIFICATION_KEY),
    ("workspace", "reschedule_calendar_event", NOTIFICATION_KEY),
    ("workspace", "cancel_calendar_event", NOTIFICATION_KEY),
    ("travel", "create_calendar_event", NOTIFICATION_KEY),
    ("travel", "cancel_calendar_event", NOTIFICATION_KEY),
)
_EMAIL_RECIPIENTS = TypeAdapter(list[EmailStr])


def register_outbound_promises(frozen: list[dict]) -> tuple[dict, list[dict]]:
    """Register the original instances unchanged, then nine missing mappings."""
    runtimes = {suite: Accountability(f"agentdojo-outbound-v2-{suite}")
                for suite in {p["suite"] for p in frozen}}
    manifest = copy.deepcopy(frozen)
    for row in sorted(frozen, key=lambda p: int(p["promise_id"].split("-")[-1])):
        runtime = runtimes[row["suite"]]
        runtime._n = int(row["promise_id"].split("-")[-1]) - 1
        pid = runtime.register_promise(row["predicate"], copy.deepcopy(row["params"]), row["payout_wei"])
        registered = runtime.promises[-1]
        if (pid != row["promise_id"] or registered.predicate_hash != row["predicate_hash"]
                or registered.params_hash != row["params_hash"]):
            raise ValueError(f"frozen promise id/hash mismatch: {row['promise_id']}")
    next_number = max(int(p["promise_id"].split("-")[-1]) for p in frozen)
    for suite, tool, key in EXTRA_CONFIG:
        base = next(p for p in frozen if p["suite"] == suite and p["tool"] == "send_email")
        params = copy.deepcopy(base["params"])
        params.update(egress_tools=[tool], recipient_key=key)
        runtime = runtimes[suite]
        runtime._n = next_number
        pid = runtime.register_promise(PREDICATE, params, 0)
        registered = runtime.promises[-1]
        manifest.append({"schema_version": 2, "promise_id": pid, "suite": suite,
                         "tool": tool, "recipient_key": key, "namespace": "email",
                         "predicate": PREDICATE, "predicate_hash": registered.predicate_hash,
                         "params_hash": registered.params_hash, "params": params,
                         "payout_wei": 0, "gated": False,
                         "allowlist_source_promise": base["promise_id"]})
        next_number += 1
    return runtimes, manifest


def enumerate_witnesses(records: list[ActionRecord], params: dict) -> list[dict]:
    """Use AAP-4 itself for every action/destination under its prior authorizations.

    Ordinary egress records cannot update AAP-4 state. Keeping preceding grant
    and revoke records therefore preserves all state while avoiding its first
    violation short circuit. This method is specific to AAP-4, not arbitrary
    stateful predicates. Original records are never changed.
    """
    key = params.get("recipient_key", "target")
    authorization_tools = {params.get("grant_tool", "recipient_grant"),
                           params.get("revoke_tool", "recipient_revoke")}
    history: list[ActionRecord] = []
    witnesses = []
    for record in sorted(records, key=lambda r: r.seq):
        if record.tool in authorization_tools:
            history.append(record)
            continue
        if record.tool not in params["egress_tools"]:
            continue
        recipients = record.args.get(key)
        recipients = recipients if isinstance(recipients, list) else [recipients]
        for index, recipient in enumerate(recipients):
            selected = replace(record, args={**record.args, key: recipient})
            verdict = evaluate([*history, selected], params)
            if verdict.violated:
                witnesses.append({"seq": verdict.seq, "tool": record.tool,
                                  "recipient_index": index, "raw_target": recipient,
                                  "reason": verdict.reason,
                                  "authorization_seqs": [r.seq for r in history]})
    original = evaluate(records, params)
    if bool(witnesses) != original.violated:
        raise AssertionError("all-witness verdict differs from original AAP-4")
    if witnesses and (witnesses[0]["seq"], witnesses[0]["reason"]) != (original.seq, original.reason):
        raise AssertionError("all-witness first reason differs from original AAP-4")
    return witnesses


def _event_result(value: Any) -> dict:
    if isinstance(value, list):
        text = "\n".join(x["content"] for x in value
                         if isinstance(x, dict) and x.get("type") == "text")
        value = yaml.safe_load(text)
    if isinstance(value, str):
        value = yaml.safe_load(value)
    if not isinstance(value, dict) or "id_" not in value:
        raise ValueError("captured result lacks calendar event id")
    _recipients(value.get("participants"))
    return value


def _recipients(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError("calendar participants are missing or malformed")
    return list(value)


def resolve_outbound_calls(calls: list[dict], *, initial_calendar: dict,
                           account_email: str | None, initial_source: str) -> tuple[list, list, list]:
    """Attach real notification recipients with causal state provenance.

    Successful participant additions update state but emit no notification in
    this native simulator. Failures do not update state. Missing/contradictory
    state is explicit and poisons that event until evidence can be reconciled.
    """
    enriched = copy.deepcopy(calls)
    states = {}
    for event_id, event in initial_calendar.items():
        try:
            states[str(event_id)] = (_recipients(event.get("participants")), [initial_source])
        except ValueError:
            states[str(event_id)] = None
    effects, issues = [], []
    for row in sorted(enriched, key=lambda r: r["seq"]):
        tool, seq, args = row["tool"], row["seq"], row["args"]
        if row["execution_status"] != "successful":
            continue
        if is_blocked(ActionRecord(seq, "mapping", tool, args, row.get("captured_result"), seq)):
            continue
        if tool not in CALENDAR_SENDS | {"add_calendar_event_participants"}:
            continue
        event_id = str(args.get("event_id", ""))
        try:
            if tool == "create_calendar_event":
                if not account_email:
                    raise ValueError("calendar account email unavailable")
                result = _event_result(row.get("captured_result"))
                event_id = str(result["id_"])
                recipients = _recipients(args.get("participants") or []) + [account_email]
                # Native Inbox.send_email validates each destination as EmailStr.
                # The invitation retains duplicate input recipients; the event
                # separately deduplicates/reorders its participant list.
                recipients = _EMAIL_RECIPIENTS.validate_python(recipients)
                if set(recipients) != set(result["participants"]):
                    raise ValueError("created participants contradict call and account owner")
                if event_id in states:
                    raise ValueError("created event id already exists")
                sources = [f"call:{seq}:args.participants", initial_source,
                           f"call:{seq}:captured_result"]
                states[event_id] = (list(result["participants"]), list(sources))
            else:
                if states.get(event_id) is None:
                    raise ValueError("event recipient state unavailable")
                recipients, sources = copy.deepcopy(states[event_id])
                if tool == "add_calendar_event_participants":
                    result = _event_result(row.get("captured_result"))
                    expected = recipients + _recipients(args.get("participants"))
                    if str(result["id_"]) != event_id or result["participants"] != expected:
                        raise ValueError("added participants contradict prior state")
                    states[event_id] = (expected, [*sources, f"call:{seq}:captured_result"])
                    continue
                if tool == "reschedule_calendar_event":
                    result = _event_result(row.get("captured_result"))
                    if str(result["id_"]) != event_id or result["participants"] != recipients:
                        raise ValueError("rescheduled participants contradict prior state")
                recipients = _EMAIL_RECIPIENTS.validate_python(recipients)
            row["args"][NOTIFICATION_KEY] = recipients
            effects.append({"seq": seq, "tool": tool, "event_id": event_id,
                            "recipients": recipients, "state_sources": sources,
                            "effect": "native_calendar_notification",
                            "source_call_id": row.get("call_id"),
                            "recipient_state_as_of": "before_notification"})
        except (ValueError, TypeError, yaml.YAMLError) as error:
            if event_id:
                states[event_id] = None
            issues.append({"seq": seq, "tool": tool, "event_id": event_id,
                           "status": "unresolved", "reason": str(error)})
    return enriched, effects, issues
