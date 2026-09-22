import pytest
from pathlib import Path


def test_destination_cannot_replace_frozen_results():
    from eval.reference_v2.run import validate_destination, ROOT
    with pytest.raises(ValueError,match='original'):
        validate_destination(ROOT/'eval/paper_main_v1/tau')
    with pytest.raises(ValueError,match='baseline'):
        validate_destination(ROOT/'eval/reference_v2/results')
    for snapshot in ('results_v3', 'presentation_no_format', 'presentation_successful_operations', 'results_v4', 'presentation_v4'):
        with pytest.raises(ValueError, match='baseline'):
            validate_destination(ROOT/'eval/reference_v2'/snapshot)
    validate_destination(ROOT/'eval/reference_v2/results_shared_full')


def test_destination_cannot_replace_baseline_source_snapshot():
    from eval.reference_v2.run import validate_destination, ROOT
    with pytest.raises(ValueError,match='baseline'):
        validate_destination(ROOT/'eval/reference_v2/baseline_source')


def test_removed_fire_has_specific_unsupported_explanation():
    from eval.reference_v2.run import changed_fires
    old=[{'seq':3,'arm':'payment_in_profile','targets':['p'],'detail':'old'}]
    result={'fires':[], 'inputs':{'unsupported_checks':{3:[{'arm':'payment_in_profile','reason':'profile not observed'}]}}}
    changes=changed_fires('tau','case',old,result)
    assert changes[0]['change']=='removed'
    assert changes[0]['explanation']=='profile not observed'
    assert changes[0]['old_fire']==old[0]


def test_output_child_symlink_cannot_redirect_into_frozen_results(tmp_path):
    from eval.reference_v2.run import validate_output_paths, ROOT
    (tmp_path/'tau').symlink_to(ROOT/'eval/paper_main_v1/tau',target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):
        validate_output_paths(tmp_path, ['tau/cases.jsonl'])


def test_modified_claws_reference_identity_is_rejected():
    from eval.reference_v2.run import validate_claws_seal
    baseline={'task':{'source':'run1','reward':1,'model':'model'}}
    with pytest.raises(ValueError,match='frozen baseline'):
        validate_claws_seal({'model':'model','tasks':{'task':{'source':'run1','reward':0}}},baseline)


def test_changed_witness_is_not_reported_as_a_fixed_violation():
    from eval.reference_v2.run import changed_fires
    old=[{'seq':3,'arm':'new_item_differs','targets':['item1']}]
    result={'fires':[{'seq':3,'arm':'new_item_differs','targets':['item1->item1']}],
            'inputs':{'scope_checks':{3:[{'arm':'new_item_differs','target':'item1->item1','allowed':[]}]}}}
    removed=next(c for c in changed_fires('tau','case',old,result) if c['change']=='removed')
    assert 'still fires' in removed['explanation']
    assert 'satisfies' not in removed['explanation']
