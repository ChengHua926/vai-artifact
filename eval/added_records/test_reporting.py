"""Reporting must not turn partial fixtures into proven historical coverage."""
import pytest

from eval.added_records.report import partition


def link(case, rule, categories, obligation=None):
    return {"benchmark": "tau", "model_id": "glm47", "case_id": case,
            "task": case, "rule_id": rule, "prior_scope": categories,
            "obligation_id": obligation}


def catalog_entry(identifier="approval"):
    return {"obligation_id": identifier, "check_ids": [identifier],
            "required_inputs": ["bound user approval"], "authority": "user controller",
            "applicability": "successful update", "completion": "at update",
            "limitations": ["conditional on complete recording"]}


def scenario(identifier, name, expected, actual, passed=True):
    return {"check_id": identifier, "scenario_id": name, "expected": expected,
            "actual": actual, "passed": passed, "recording_route": "SDK Session",
            "trace_hash": "recorded-trace-hash"}


def controls(identifier="approval"):
    return [scenario(identifier, "allow", "satisfied", "satisfied"),
            scenario(identifier, "deny", "violated", "violated"),
            scenario(identifier, "incomplete", "unresolved", "unresolved")]


def test_mixed_run_counts_once_and_failed_check_does_not_become_outside_scope():
    links = [link("mixed", "a", ["could_catch"], "approval"),
             link("mixed", "b", ["out_of_scope"]),
             link("unsupported", "c", ["could_catch"], "missing-check"),
             link("subjective", "d", ["out_of_scope"])]
    result = partition(links, [catalog_entry()], controls())
    assert result["counts"]["tau/glm47"] == {
        "tasks": 3, "conditional_supported": 1, "unresolved": 1,
        "outside_reviewed_scope": 1, "prior_could_catch": 2}
    assert {r["case_id"]: r["classification"] for r in result["tasks"]} == {
        "mixed": "conditional_supported", "unsupported": "unresolved",
        "subjective": "outside_reviewed_scope"}


@pytest.mark.parametrize("scenarios", [controls()[:1], controls()[:2],
    controls() + [scenario("approval", "wrong_action", "violated", "satisfied", False)]])
def test_missing_or_failed_controls_cannot_support_a_conditional_count(scenarios):
    result = partition([link("one", "a", ["could_catch"], "approval")],
                       [catalog_entry()], scenarios)
    assert result["tasks"][0]["classification"] == "unresolved"


def test_recorded_boolean_cannot_hide_wrong_actual_outcome():
    tests = controls()
    tests[1] = scenario("approval", "deny", "violated", "satisfied", True)
    result = partition([link("one", "a", ["could_catch"], "approval")],
                       [catalog_entry()], tests)
    assert result["tasks"][0]["classification"] == "unresolved"


def test_duplicate_task_rule_link_is_rejected():
    row = link("one", "a", ["could_catch"], "approval")
    with pytest.raises(ValueError, match="duplicate"):
        partition([row, row], [catalog_entry()], controls())


def test_a_test_does_not_promote_an_outside_scope_citation():
    result = partition([link("one", "a", ["out_of_scope"], "approval")],
                       [catalog_entry()], controls())
    assert result["tasks"][0]["classification"] == "outside_reviewed_scope"


def test_missing_input_authority_keeps_claim_unresolved():
    entry = catalog_entry()
    del entry["authority"]
    result = partition([link("one", "a", ["could_catch"], "approval")], [entry], controls())
    assert result["tasks"][0]["classification"] == "unresolved"
def test_saved_trace_is_rehashed_and_replayed_before_counting():
    from aa_commons import trace_hash
    from aa_commons.structured_workflow import WorkflowRecorder, register_workflow
    from aa_sdk import Accountability
    from .report import validate_scenarios
    import copy
    import pytest

    config = {'rules': {'example': {'tools': ['write'], 'condition': {'lit': False}}}}
    profile = register_workflow(config)
    s = Accountability('report-verification').session('user')
    recorder = WorkflowRecorder(s)
    recorder.begin(['actions'])
    s.guard('write', {'path': 'example'}, lambda: {'ok': True})
    recorder.complete()
    row = {'check_id': 'example', 'scenario_id': 'bad', 'profile_hash': profile.profile_hash,
           'records': [r.to_dict() for r in s.records], 'trace_hash': trace_hash(s.records),
           'actual': 'violated', 'registered_verdict': {'violated': True}}
    assert validate_scenarios([row]) == 1
    tampered = copy.deepcopy(row)
    tampered['records'][1]['args']['path'] = 'changed'
    with pytest.raises(ValueError, match='trace hash'):
        validate_scenarios([tampered])
    lying = {**row, 'actual': 'satisfied'}
    with pytest.raises(ValueError, match='replayed status'):
        validate_scenarios([lying])


def test_omitted_or_extra_task_rule_links_do_not_shrink_or_expand_denominator():
    from .report import validate_link_universe
    import pytest
    appendix = {'task_rules': [{'suite': 'Tau GLM', 'task_id': 't1', 'rule_id': 'r1'}]}
    cases = [{'reference_group': 'successful_attack', 'attack_related_witness': False,
              'model_id': 'glm', 'case_id': 'a1', 'suite': 'slack', 'injection_task_id': 'inj1'}]
    contracts = [{'suite': 'slack', 'injection_task_id': 'inj1', 'condition_id': 'visit'}]
    links = [{'benchmark': 'tau', 'suite': 'Tau GLM', 'task': 't1', 'rule_id': 'r1'},
             {'benchmark': 'agentdojo', 'model_id': 'glm', 'case_id': 'a1', 'condition_id': 'visit'}]
    assert validate_link_universe(links, appendix, cases, contracts) == 2
    with pytest.raises(ValueError, match='task-rule universe'):
        validate_link_universe(links[1:], appendix, cases, contracts)
    with pytest.raises(ValueError, match='task-rule universe'):
        validate_link_universe(links + [{**links[0], 'task': 'invented'}], appendix, cases, contracts)
