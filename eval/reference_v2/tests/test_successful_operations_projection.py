"""The projection must recompute counts, preserve evidence, and reject bad joins."""
import copy
import importlib
import json
from pathlib import Path

import pytest


def projection():
    return importlib.import_module('eval.reference_v2.project_successful_operations')


def fixture():
    cap_fire={'arm':'once_per_order','seq':2,'targets':['A'],'original_fire_index':7}
    other={'arm':'auth_first','seq':2,'targets':['access'],'original_fire_index':9}
    task={'bench':'tau','case_id':'case','model_id':'glm47','task':'example',
          'quality_group':'primary','label':'violation','fires':[cap_fire,other],
          'fired':True,'same_reason':True,'matching_original_fire_indexes':[9],
          'citations':[{'rule_id':'r','matching_fire_indexes':[9],'scope':'unresolved'}],
          'rules':[{'rule_id':'r','matched':True,'matching_original_fire_indexes':[9],
                    'scope_categories':['could_catch','should_catch']} ]}
    calls=[{'case_id':'case','seq':i,'tool':'modify_pending_order_items','args':{'order_id':'A'},
            'accepted':accepted,'captured_accepted':accepted,'result_match':True,
            'captured_result':'ok' if accepted else 'Error: unavailable','changes':[]}
           for i,accepted in [(1,False),(2,True)]]
    cases=[{'case_id':'case','domain':'retail','monitor_rejected_seqs':[1]}]
    return [task],calls,cases


def test_recomputes_successful_trigger_after_failed_request_and_preserves_other_match():
    tasks,calls,cases=fixture(); originals=copy.deepcopy((tasks,calls,cases))
    rows,audit=projection().project(tasks,calls,cases)
    assert [f['arm'] for f in rows[0]['fires']]==['auth_first']
    assert rows[0]['matching_original_fire_indexes']==[9]
    assert rows[0]['same_reason'] is True
    assert rows[0]['citations']==tasks[0]['citations']
    assert rows[0]['rules']==tasks[0]['rules']
    assert audit[0]['removed_fire_indexes']==[7]
    assert (tasks,calls,cases)==originals


def test_two_successful_operations_keep_original_fire_and_reference_link():
    tasks,calls,cases=fixture()
    calls[0].update(accepted=True,captured_accepted=True,captured_result='ok')
    cases[0]['monitor_rejected_seqs']=[]
    tasks[0]['matching_original_fire_indexes']=[7,9]
    rows,_=projection().project(tasks,calls,cases)
    assert rows[0]['fires']==tasks[0]['fires']
    assert rows[0]['matching_original_fire_indexes']==[7,9]


def test_inconsistent_rejection_evidence_is_not_silently_treated_as_success():
    tasks,calls,cases=fixture(); cases[0]['monitor_rejected_seqs']=[]
    with pytest.raises(ValueError,match='acceptance'):
        projection().project(tasks,calls,cases)


def test_missing_call_cannot_silently_remove_a_frozen_fire():
    tasks,calls,cases=fixture(); calls.pop()
    with pytest.raises(ValueError,match='attempt'):
        projection().project(tasks,calls,cases)


def test_frozen_cohort_counts_and_same_reason_matches():
    root=Path(__file__).resolve().parents[3]
    output=projection().build_outputs(root)
    summary=json.loads(output['summary.json'])
    glm=summary['panels']['Tau GLM47']; qwen=summary['panels']['Tau Qwen30B']
    assert [glm[k] for k in ('violation_fire','violation_no_fire','clean_fire','clean_no_fire')]==[15,96,0,53]
    assert [qwen[k] for k in ('violation_fire','violation_no_fire','clean_fire','clean_no_fire')]==[29,125,0,10]
    assert (glm['same_reason_tasks'],qwen['same_reason_tasks'])==(15,28)
    assert qwen['unmatched_positive_firing_task_ids']==['ext-162']
    assert summary['tau_once_per_order']=={'before_fires':30,'after_fires':0,'removed_fires':30,'affected_tasks':27}
    assert len(output['tasks.jsonl'].splitlines())==388
