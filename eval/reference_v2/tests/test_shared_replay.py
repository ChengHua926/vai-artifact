"""Full replay comparison must detect changes hidden by aggregate counts."""
import copy
from importlib import import_module

import pytest


def replay():
    return import_module("eval.reference_v2.replay_shared")


def behavior(violated=True):
    return {"verdicts": {"rule": {"violated": violated, "record_seq": 3 if violated else None,
                                   "reason": "original reason" if violated else ""}},
            "fires": [{"seq": 2, "arm": "rule"}] if violated else [],
            "diagnostics": [], "unsupported": {}, "trace_hash": "fixed",
            "sdk_verifier_parity": True}


def test_comparison_detects_reason_change_with_unchanged_verdict():
    before = {"task-a": behavior()}
    after = copy.deepcopy(before)
    after["task-a"]["verdicts"]["rule"]["reason"] = "different reason"
    assert replay().compare_cases(before, after) == [{"task": "task-a", "fields": ["verdicts"]}]


def test_comparison_detects_different_offending_record():
    before = {"task-a": behavior()}
    after = copy.deepcopy(before)
    after["task-a"]["verdicts"]["rule"]["record_seq"] = 4
    assert replay().compare_cases(before, after) == [{"task": "task-a", "fields": ["verdicts"]}]


def test_comparison_detects_swapped_tasks_despite_identical_totals():
    before = {"task-a": behavior(True), "task-b": behavior(False)}
    after = {"task-a": behavior(False), "task-b": behavior(True)}
    assert sum(x["verdicts"]["rule"]["violated"] for x in before.values()) == sum(
        x["verdicts"]["rule"]["violated"] for x in after.values())
    assert {item["task"] for item in replay().compare_cases(before, after)} == {"task-a", "task-b"}


def test_legacy_answer_removal_does_not_mutate_observations():
    original = {"scope_checks": {1: [{"condition": {"lit": True}, "allowed": ["x"]}]},
                "mandate_checks": {2: [{"amount_condition": {"lit": True}, "amount": 9,
                                        "allowed_amounts": [9], "max_amount": 9}]}}
    stripped, count = replay().remove_legacy_answers(original)
    assert count == 2
    assert "allowed" not in stripped["scope_checks"][1][0]
    assert "allowed_amounts" not in stripped["mandate_checks"][2][0]
    assert "max_amount" not in stripped["mandate_checks"][2][0]
    assert original["scope_checks"][1][0]["allowed"] == ["x"]


def test_cases_gzip_is_deterministic_and_has_no_timestamp():
    import gzip
    encoded = replay().encode_cases([{"task": "a", "behavior": behavior()}])
    assert encoded == replay().encode_cases([{"task": "a", "behavior": behavior()}])
    assert encoded[4:8] == b"\x00" * 4
    assert b'"task":"a"' in gzip.decompress(encoded)


def test_check_mode_detects_changed_saved_bytes_without_rewriting(tmp_path):
    module = replay()
    artifacts = {"summary.json": b'{"passed":true}\n'}
    module.write_artifacts(tmp_path, artifacts)
    module.write_artifacts(tmp_path, artifacts, check=True)
    changed = b'{"passed":false}\n'
    (tmp_path / "summary.json").write_bytes(changed)
    with pytest.raises(ValueError, match="summary.json"):
        module.write_artifacts(tmp_path, artifacts, check=True)
    assert (tmp_path / "summary.json").read_bytes() == changed


def test_check_comparison_normalizes_json_object_keys_without_sort_order_artifacts():
    before = {"task": {"unsupported": {9: ["a"], 10: ["b"]}}}
    after = {"task": {"unsupported": {"10": ["b"], "9": ["a"]}}}
    assert replay().compare_cases(before, after) == []
