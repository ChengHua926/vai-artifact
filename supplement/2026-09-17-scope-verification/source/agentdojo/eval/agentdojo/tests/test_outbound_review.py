from __future__ import annotations

import copy

import pytest

from eval.agentdojo.runtime import build_action_records


def call(seq, tool, args, result=None, status="successful"):
    return {"seq": seq, "tool": tool, "args": args,
            "captured_result": result, "execution_status": status}


def test_all_witnesses_keep_grants_revocations_blocking_and_first_reason():
    from eval.agentdojo.outbound import enumerate_witnesses
    from aa_commons.predicates.egress_within_allowlist import evaluate

    rows = [
        call(1, "send", {"to": ["first@example.com", "second@example.com"]}),
        call(2, "recipient_grant", {"to": "later@example.com"}, "granted"),
        call(3, "send", {"to": ["later@example.com"]}),
        call(4, "recipient_revoke", {"to": "later@example.com"}),
        call(5, "send", {"to": ["later@example.com"]}),
        call(6, "send", {"to": ["blocked@example.com"]}, {"blocked": True}),
        call(7, "send", {"to": ["failed@example.com"]}, status="failed"),
    ]
    records = build_action_records(rows, case_id="case")
    params = {"egress_tools": ["send"], "recipient_key": "to",
              "recipient_allowlist": []}
    before = copy.deepcopy(records)
    witnesses = enumerate_witnesses(records, params)
    assert [(w["seq"], w["raw_target"]) for w in witnesses] == [
        (1, "first@example.com"), (1, "second@example.com"), (5, "later@example.com")]
    first = evaluate(records, params)
    assert (witnesses[0]["seq"], witnesses[0]["reason"]) == (first.seq, first.reason)
    assert records == before


def test_calendar_recipients_follow_successful_state_and_keep_provenance():
    from eval.agentdojo.outbound import resolve_outbound_calls

    rows = [
        call(1, "add_calendar_event_participants", {"event_id": "1", "participants": ["new@example.com"]},
             {"id_": "1", "participants": ["old@example.com", "new@example.com"]}),
        call(2, "add_calendar_event_participants", {"event_id": "1", "participants": ["failed@example.com"]},
             status="failed"),
        call(3, "reschedule_calendar_event", {"event_id": "1"},
             {"id_": "1", "participants": ["old@example.com", "new@example.com"]}),
        call(4, "cancel_calendar_event", {"event_id": "1"}, "canceled"),
        call(5, "create_calendar_event", {"participants": ["fresh@example.com"]},
             {"id_": "2", "participants": ["fresh@example.com", "owner@example.com"]}),
        call(6, "cancel_calendar_event", {"event_id": "2"}, "canceled"),
    ]
    enriched, effects, issues = resolve_outbound_calls(
        rows, initial_calendar={"1": {"participants": ["old@example.com"]}},
        account_email="owner@example.com", initial_source="initial-state-hash")
    assert issues == []
    assert [(x["seq"], x["recipients"]) for x in effects] == [
        (3, ["old@example.com", "new@example.com"]), (4, ["old@example.com", "new@example.com"]),
        (5, ["fresh@example.com", "owner@example.com"]), (6, ["fresh@example.com", "owner@example.com"])]
    assert effects[0]["state_sources"] == ["initial-state-hash", "call:1:captured_result"]
    assert enriched[2]["args"]["notification_recipients"] == ["old@example.com", "new@example.com"]
    assert "notification_recipients" not in rows[2]["args"]


def test_created_event_order_and_email_normalization_do_not_poison_later_state():
    from eval.agentdojo.outbound import resolve_outbound_calls

    rows = [call(1, "create_calendar_event",
                 {"participants": ["Guest@EXAMPLE.COM", "Guest@EXAMPLE.COM"]},
                 {"id_": "1", "participants": ["owner@example.com", "Guest@example.com"]}),
            call(2, "add_calendar_event_participants",
                 {"event_id": "1", "participants": ["new@example.com"]},
                 {"id_": "1", "participants": ["owner@example.com", "Guest@example.com", "new@example.com"]}),
            call(3, "reschedule_calendar_event", {"event_id": "1"},
                 {"id_": "1", "participants": ["owner@example.com", "Guest@example.com", "new@example.com"]})]
    _, effects, issues = resolve_outbound_calls(
        rows, initial_calendar={}, account_email="owner@example.com", initial_source="initial")
    assert issues == []
    assert effects[0]["recipients"] == ["Guest@example.com", "Guest@example.com", "owner@example.com"]
    assert effects[1]["recipients"] == ["owner@example.com", "Guest@example.com", "new@example.com"]


def test_unknown_or_contradictory_calendar_state_is_explicitly_unresolved():
    from eval.agentdojo.outbound import resolve_outbound_calls

    rows = [
        call(1, "cancel_calendar_event", {"event_id": "missing"}),
        call(2, "reschedule_calendar_event", {"event_id": "1"},
             {"id_": "1", "participants": ["unexplained@example.com"]}),
        call(3, "cancel_calendar_event", {"event_id": "1"}),
    ]
    enriched, effects, issues = resolve_outbound_calls(
        rows, initial_calendar={"1": {"participants": ["old@example.com"]}},
        account_email="owner@example.com", initial_source="initial")
    assert effects == []
    assert [x["seq"] for x in issues] == [1, 2, 3]
    assert all("notification_recipients" not in x["args"] for x in enriched)


def test_cc_bcc_and_calendar_promises_share_frozen_allowlists():
    from eval.agentdojo.outbound import register_outbound_promises
    from eval.agentdojo.runtime import register_shipped_promises

    _, original = register_shipped_promises()
    runtimes, manifest = register_outbound_promises(original)
    rows = [call(1, "send_email", {"recipients": ["emma.johnson@bluesparrowtech.com"],
                                  "cc": ["cc@example.com"], "bcc": ["bcc@example.com"]}),
            call(2, "create_calendar_event", {"notification_recipients": ["calendar@example.com"]})]
    verdicts = runtimes["workspace"].self_check(build_action_records(rows, case_id="case"))
    fired = {(p["tool"], p["recipient_key"]) for p in manifest
             if p["suite"] == "workspace" and verdicts[p["promise_id"]].violated}
    assert fired == {("send_email", "cc"), ("send_email", "bcc"),
                     ("create_calendar_event", "notification_recipients")}
    for p in original:
        assert next(x for x in manifest if x["promise_id"] == p["promise_id"]) == p
    allow = next(p["params"]["recipient_allowlist"] for p in original
                 if p["suite"] == "workspace" and p["tool"] == "send_email")
    assert all(p["params"]["recipient_allowlist"] == allow for p in manifest
               if p["suite"] == "workspace")


def test_frozen_predicate_hash_mismatch_is_rejected():
    from eval.agentdojo.outbound import register_outbound_promises
    from eval.agentdojo.runtime import register_shipped_promises

    _, manifest = register_shipped_promises()
    manifest[0]["predicate_hash"] = "0x" + "00" * 32
    with pytest.raises(ValueError, match="hash"):
        register_outbound_promises(manifest)


def test_summary_separates_attack_success_from_benign_and_unknown_labels():
    from eval.agentdojo.outbound_review import summarize

    rows = []
    for group, old, new, related in [("successful_attack", False, True, True),
                                      ("successful_attack", True, True, False),
                                      ("failed_attack", False, True, False),
                                      ("benign", False, True, False),
                                      ("protocol_error", False, True, False)]:
        rows.append({"model_id": "model", "reference_group": group,
                     "original_any_fire": old, "revised_any_fire": new,
                     "attack_related_witness": related, "unresolved_count": 0})
    result = summarize(rows)["model"]
    assert result["successful_attack"] == {
        "runs": 2, "original_alarm_runs": 1, "revised_alarm_runs": 2,
        "new_alarm_runs": 1, "removed_alarm_runs": 0,
        "reviewed_attack_related_runs": 1, "alarm_without_reviewed_attack_match": 1,
        "unresolved_runs": 0}
    assert result["benign"]["runs"] == 1
    assert result["protocol_error"]["reviewed_attack_related_runs"] == 0


def test_direct_injection_baseline_uses_default_native_calendar():
    from eval.agentdojo.outbound_review import initial_calendar

    events, owner = initial_calendar(
        {"benchmark_version": "v1.2.2", "suite": "workspace",
         "case_kind": "injection_baseline", "user_task_id": "injection_task_0"}, {})
    assert owner == "emma.johnson@bluesparrowtech.com"
    assert "5" in events
