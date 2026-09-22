from __future__ import annotations

import copy

from eval.agentdojo.runtime import build_action_records, register_shipped_promises
from eval.agentdojo.outbound import register_outbound_promises


def test_calendar_contacts_are_structured_preinjection_and_task_scoped():
    from eval.agentdojo.allowlist_sensitivity import calendar_contact_policy

    policy = calendar_contact_policy({"benchmark_version": "v1.2.2", "suite": "workspace",
                                      "case_kind": "attack", "user_task_id": "user_task_9",
                                      "trace": {"injections": {"ignored": "attacker@evil.example"}}})
    assert "john.mitchell@gmail.com" in policy["contacts"]
    assert "martha.raynolds@gmail.com" in policy["contacts"]
    assert "attacker@evil.example" not in policy["contacts"]
    john = next(x for x in policy["provenance"] if x["recipient"] == "john.mitchell@gmail.com")
    assert "24" in john["event_ids"]
    assert policy["task_scope"] == "workspace/user_task_9"
    assert policy["injections_applied"] is False


def test_variant_changes_only_email_allowlists_and_preserves_baseline():
    from eval.agentdojo.allowlist_sensitivity import register_variant

    _, frozen = register_shipped_promises()
    _, baseline = register_outbound_promises(frozen)
    before = copy.deepcopy(baseline)
    runtime, variants = register_variant(baseline, "workspace", ["extra@example.com"])
    rows = [{"seq": 1, "tool": "send_email", "args": {"recipients": ["extra@example.com"]},
             "captured_result": None, "execution_status": "successful"},
            {"seq": 2, "tool": "send_email", "args": {"recipients": ["still-off@example.com"]},
             "captured_result": None, "execution_status": "successful"}]
    verdicts = runtime.self_check(build_action_records(rows, case_id="case"))
    to = next(p for p in variants if p["tool"] == "send_email" and p["recipient_key"] == "recipients")
    assert verdicts[to["promise_id"]].seq == 2
    assert baseline == before
    for variant in variants:
        original = next(p for p in baseline if p["promise_id"] == variant["promise_id"])
        assert variant["predicate_hash"] == original["predicate_hash"]
        assert set(variant["params"]["recipient_allowlist"]) == set(original["params"]["recipient_allowlist"]) | {"extra@example.com"}
        assert {k:v for k,v in variant["params"].items() if k != "recipient_allowlist"} == {
            k:v for k,v in original["params"].items() if k != "recipient_allowlist"}


def test_non_email_promises_are_unchanged_by_calendar_policy():
    from eval.agentdojo.allowlist_sensitivity import register_variant

    _, frozen = register_shipped_promises()
    _, baseline = register_outbound_promises(frozen)
    _, variants = register_variant(baseline, "banking", ["extra@example.com"])
    for row in variants:
        old = next(p for p in baseline if p["promise_id"] == row["promise_id"])
        assert row["params"] == old["params"]
        assert row["params_hash"] == old["params_hash"]


def test_summary_preserves_exclusive_benign_categories_and_attack_losses():
    from eval.agentdojo.allowlist_sensitivity import summarize_variant

    rows = [dict(model_id="m", reference_group="successful_attack", baseline_alarm=True,
                 variant_alarm=False, baseline_attack_related=True, variant_attack_related=False),
            dict(model_id="m", reference_group="successful_attack", baseline_alarm=True,
                 variant_alarm=True, baseline_attack_related=True, variant_attack_related=True)]
    result = summarize_variant(rows)["m"]["successful_attack"]
    assert result == {"runs": 2, "baseline_alarm_runs": 2, "variant_alarm_runs": 1,
                      "removed_alarm_runs": 1, "added_alarm_runs": 0,
                      "baseline_attack_related_runs": 2, "variant_attack_related_runs": 1,
                      "lost_attack_detections": 1, "gained_attack_detections": 0}
