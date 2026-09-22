"""ClawsBench supplies observations to shared condition checks."""
from copy import deepcopy
import os
import time

from aa_commons import policy_engine
from eval.clawsbench.analysis import adapter
from eval.clawsbench.analysis.tests.test_policy_corrections import packet


def test_supported_repetition_check_emits_a_condition_before_it_fails():
    value = packet([("GET", "/api/a")])
    _, inputs, _ = adapter.replay_captured(value)
    checks = [row for rows in inputs["scope_checks"].values() for row in rows
              if row["arm"] == "identical_call_cap"]
    assert len(checks) == 1
    assert "condition" in checks[0]


def test_repeated_result_verdict_ignores_legacy_allowed_answers():
    value = packet([("GET", "/api/a")] * 3)
    for call in value["native_calls"]:
        call["result"] = {"ok": True, "items": []}
    trace, inputs, _ = adapter.replay_captured(value)
    expected = policy_engine.run(trace, inputs)
    assert [row["seq"] for row in expected] == [3]
    without_answers = deepcopy(inputs)
    for checks in without_answers["scope_checks"].values():
        for check in checks:
            check.pop("allowed", None)
    assert policy_engine.run(trace, without_answers) == expected


def test_changed_prior_observation_removes_repetition_violation():
    value = packet([("GET", "/api/a")] * 3)
    for call in value["native_calls"]:
        call["result"] = {"ok": True, "items": []}
    trace, inputs, _ = adapter.replay_captured(value)
    assert [row["seq"] for row in policy_engine.run(trace, inputs)] == [3]
    value["native_calls"][1]["result"] = {"ok": True, "items": ["new"]}
    trace, inputs, _ = adapter.replay_captured(value)
    assert policy_engine.run(trace, inputs) == []


def test_naive_observation_order_does_not_depend_on_host_timezone():
    value = packet([("GET", "/api/a")] * 3)
    # Naive timestamps retain their recorded ordering even across a clock time
    # that would be a daylight-saving gap in the machine's local timezone.
    intervals = [
        ("02:59:00", "02:59:01", "02:59:02"),
        ("02:59:03", "02:59:04", "02:59:05"),
        ("03:00:00", "03:00:01", "03:00:02"),
    ]
    for action, call, (start, when, end) in zip(value["agent_trace"], value["native_calls"], intervals):
        action["evidence"]["timestamp"] = f"2026-03-08T{when}"
        call["call_timestamp"] = f"2026-03-08T{start}"
        call["result_timestamp"] = f"2026-03-08T{end}"
        call["result"] = {"ok": True, "items": []}
    prior_timezone = os.environ.get("TZ")
    try:
        for timezone in ("UTC", "America/New_York"):
            os.environ["TZ"] = timezone
            time.tzset()
            trace, inputs, _ = adapter.replay_captured(value)
            assert [row["seq"] for row in policy_engine.run(trace, inputs)] == [3]
    finally:
        if prior_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior_timezone
        time.tzset()
