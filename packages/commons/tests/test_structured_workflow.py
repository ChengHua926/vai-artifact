"""Checks bind real SDK actions to independently supplied workflow evidence."""
import importlib.util

import pytest

from aa_commons import registry
from aa_sdk import Accountability


def api():
    assert importlib.util.find_spec('aa_commons.structured_workflow'), 'workflow support missing'
    from aa_commons import structured_workflow
    return structured_workflow


def rule_config(condition, **rule):
    return {'observations': {}, 'rules': {'test': {
        'tools': ['modify'], 'condition': condition, **rule}}}


def check(session, config):
    m = api()
    profile = m.register_workflow(config)
    params = {'observation_profile': profile.profile_hash, 'rule': 'test'}
    return m.evaluate_workflow(session.records, params)


def session():
    s = Accountability('workflow-test').session('user')
    return s, api().WorkflowRecorder(s)


def test_exact_selection_is_checked_by_registered_aap2_without_adapter_verdict():
    s, c = session()
    c.begin(['actions', 'selection'])
    args = {'items': ['A', 'B']}
    c.observe('selection', {'items': ['A', 'B']}, source='user', action_id='a', args=args)
    s.guard('modify', args, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'multiset_eq', 'args': [
        {'ref': 'action.args.items'}, {'ref': 'evidence.selection.items'}]})
    config['observations'] = {'selection': {'name': 'selection', 'source': 'user',
                                           'action_bound': True, 'args_bound': True}}
    result = check(s, config)
    assert result['status'] == 'satisfied'
    assert result['verdict'].violated is False
    assert len(registry.catalog()) == 5


@pytest.mark.parametrize('second,expected', [
    ({'id': 'P2', 'first': 'B', 'last': 'Two', 'dob': '2000-01-02'}, 'satisfied'),
    ({'id': 'P2', 'first': 'B', 'last': 'Two'}, 'violated'),
    ({'id': 'P2', 'first': 'B', 'last': 'Two', 'dob': None}, 'violated'),
    ({'id': 'P2', 'first': 'B', 'last': 'Two', 'dob': ''}, 'violated'),
])
def test_required_fields_cover_every_record_in_complete_typed_form(second, expected):
    s, c = session()
    c.begin(['actions', 'request'])
    c.observe('request', {'passengers': [
        {'id': 'P1', 'first': 'A', 'last': 'One', 'dob': '2000-01-01'}, second]},
        source='user', action_id='a')
    s.guard('modify', {}, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'all_nonempty_fields', 'args': [
        {'ref': 'evidence.request.passengers'}, {'lit': ['id', 'first', 'last', 'dob']}]})
    config['observations'] = {'request': {'name': 'request', 'source': 'user', 'action_bound': True}}
    assert check(s, config)['status'] == expected


def test_required_records_cannot_be_vacuously_complete_when_list_is_empty():
    s, c = session()
    c.begin(['actions', 'request'])
    c.observe('request', {'passengers': []}, source='user', action_id='a')
    s.guard('modify', {}, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'all_nonempty_fields', 'args': [
        {'ref': 'evidence.request.passengers'}, {'lit': ['id', 'dob']}]})
    config['observations'] = {'request': {'name': 'request', 'source': 'user', 'action_bound': True}}
    assert check(s, config)['status'] == 'violated'


@pytest.mark.parametrize('decision,want', [('allow', 'satisfied'), ('deny', 'violated'),
                                         ('timeout', 'violated'), (None, 'violated')])
def test_native_authorization_decision_is_not_inferred_from_missing_permission(decision, want):
    s, c = session()
    c.begin(['actions', 'authorization'])
    args = {'items': ['A']}
    if decision:
        c.authorize(action_id='a', request_id='r', tool='modify', args=args,
                    decision=decision, authority='human')
    s.guard('modify', args, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'eq', 'args': [{'ref': 'authorization.decision'}, {'lit': 'allow'}]})
    assert check(s, config)['status'] == want


@pytest.mark.parametrize('mode', ['wrong_args', 'wrong_action', 'late', 'revoked', 'agent_claim'])
def test_approval_cannot_be_reused_rebound_or_asserted_by_agent(mode):
    s, c = session()
    c.begin(['actions', 'authorization'])
    args = {'items': ['A']}
    if mode == 'agent_claim':
        s.guard('user_authorization', {'schema_version': 1, 'authority': 'human',
                'decision': 'allow', 'tool': 'modify', 'args': args}, lambda: 'allow', action_id='fake')
    elif mode != 'late':
        c.authorize(action_id='wrong' if mode == 'wrong_action' else 'a', request_id='r',
                    tool='modify', args={'items': ['B']} if mode == 'wrong_args' else args,
                    decision='allow', authority='human')
        if mode == 'revoked':
            c.authorize(action_id='a', request_id='r2', tool='modify', args=args,
                        decision='deny', authority='human')
    s.guard('modify', args, lambda: {'ok': True}, action_id='a')
    if mode == 'late':
        c.authorize(action_id='a', request_id='r', tool='modify', args=args,
                    decision='allow', authority='human')
    c.complete()
    config = rule_config({'op': 'eq', 'args': [{'ref': 'authorization.decision'}, {'lit': 'allow'}]})
    assert check(s, config)['status'] == 'violated'


@pytest.mark.parametrize('mode', ['no_seal', 'incomplete', 'missing_channel'])
def test_absence_is_unknown_without_complete_recording(mode):
    s, c = session()
    c.begin(['actions'] if mode == 'missing_channel' else ['actions', 'authorization'])
    s.guard('modify', {'items': ['A']}, lambda: {'ok': True}, action_id='a')
    if mode != 'no_seal':
        c.complete(recording_complete=mode != 'incomplete')
    config = rule_config({'op': 'eq', 'args': [{'ref': 'authorization.decision'}, {'lit': 'allow'}]})
    result = check(s, config)
    assert result['status'] == 'unresolved' and result['verdict'] is None
    profile = api().register_workflow(config)
    with pytest.raises(api().UnresolvedEvidence):
        registry.get('action_within_declared_scope').evaluate(s.records, {
            'observation_profile': profile.profile_hash, 'rule': 'test'})


@pytest.mark.parametrize('mode', ['missing', 'wrong_action', 'wrong_args', 'late', 'revoked', 'agent_source'])
def test_missing_or_stale_selection_is_not_fabricated(mode):
    s, c = session()
    c.begin(['actions', 'selection'])
    args = {'items': ['A']}
    if mode not in {'missing', 'late'}:
        c.observe('selection', {'items': ['A']}, source='agent' if mode == 'agent_source' else 'user',
                  action_id='b' if mode == 'wrong_action' else 'a',
                  args={'items': ['B']} if mode == 'wrong_args' else args)
        if mode == 'revoked':
            c.revoke('selection', source='user', action_id='a', args=args)
    s.guard('modify', args, lambda: {'ok': True}, action_id='a')
    if mode == 'late':
        c.observe('selection', {'items': ['A']}, source='user', action_id='a', args=args)
    c.complete()
    config = rule_config({'op': 'eq', 'args': [
        {'ref': 'action.args.items'}, {'ref': 'evidence.selection.items'}]})
    config['observations'] = {'selection': {'name': 'selection', 'source': 'user',
                                           'action_bound': True, 'args_bound': True}}
    assert check(s, config)['status'] == 'unresolved'


@pytest.mark.parametrize('items,complete,want', [(['A', 'B'], True, 'satisfied'),
                                               (['A'], True, 'violated'),
                                               (['A'], False, 'unresolved')])
def test_required_action_set_is_checked_only_at_explicit_completion(items, complete, want):
    s, c = session()
    c.begin(['actions', 'required'])
    c.observe('required', ['A', 'B'], source='user')
    for item in items:
        s.guard('modify', {'item': item}, lambda: {'ok': True}, action_id=item)
    c.complete(task_complete=complete)
    config = rule_config({'op': 'multiset_eq', 'args': [
        {'ref': 'collections.done'}, {'ref': 'evidence.required'}]}, phase='terminal')
    config['observations'] = {'required': {'name': 'required', 'source': 'user'}}
    config['collections'] = {'done': {'tools': ['modify'], 'path': 'args.item'}}
    assert check(s, config)['status'] == want


@pytest.mark.parametrize('actual,want', [('Price: 20', 'satisfied'), ('Price: 2', 'violated'),
                                       ('Price: 20. Ignore policy', 'violated')])
def test_rendered_text_is_bound_to_independent_typed_source(actual, want):
    s, c = session()
    c.begin(['actions', 'offer'])
    c.observe('offer', {'price': 20}, source='catalog')
    s.guard('modify', {'text': actual, 'price': 2}, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'lit': True}, rendering={
        'template': 'Price: {price}', 'fields': {'price': {'ref': 'evidence.offer.price'}},
        'path': 'args.text'})
    config['observations'] = {'offer': {'name': 'offer', 'source': 'catalog'}}
    assert check(s, config)['status'] == want


def test_prior_sources_and_arithmetic_use_successful_recorded_operands():
    s, c = session()
    c.begin(['actions', 'prices'])
    c.observe('prices', {'base': 100, 'extra': 50}, source='catalog')
    s.guard('read', {'source': 'policy'}, lambda: {'ok': True}, action_id='r')
    s.guard('modify', {'amount': 150}, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'and', 'args': [
        {'op': 'in', 'args': [{'lit': 'policy'}, {'ref': 'collections.read'}]},
        {'op': 'eq', 'args': [{'ref': 'action.args.amount'}, {'op': 'add', 'args': [
            {'ref': 'evidence.prices.base'}, {'ref': 'evidence.prices.extra'}]}]}]})
    config['observations'] = {'prices': {'name': 'prices', 'source': 'catalog'}}
    config['collections'] = {'read': {'tools': ['read'], 'path': 'args.source'}}
    assert check(s, config)['status'] == 'satisfied'


def test_no_applicable_actions_and_failed_actions_do_not_fabricate_violations():
    s, c = session()
    c.begin(['actions'])
    s.record('modify', {'amount': 999}, {'error': 'failed'}, action_id='a')
    c.complete()
    assert check(s, rule_config({'lit': False}))['status'] == 'satisfied'


@pytest.mark.parametrize('read_mode,want', [('successful', 'satisfied'),
                                         ('failed', 'violated'), ('late', 'violated')])
def test_failed_or_late_source_read_cannot_satisfy_a_prior_read_requirement(read_mode, want):
    s, c = session()
    c.begin(['actions'])
    if read_mode != 'late':
        s.record('read', {'source': 'contract'},
                 {'error': 'not found'} if read_mode == 'failed' else {'ok': True}, action_id='read')
    s.guard('modify', {'target': 'contract'}, lambda: {'ok': True}, action_id='a')
    if read_mode == 'late':
        s.record('read', {'source': 'contract'}, {'ok': True}, action_id='read')
    c.complete()
    config = rule_config({'op': 'subset', 'args': [
        {'lit': ['contract']}, {'ref': 'collections.read'}]})
    config['collections'] = {'read': {'tools': ['read'], 'path': 'args.source'}}
    assert check(s, config)['status'] == want


def test_late_action_after_recording_seal_does_not_satisfy_terminal_omission():
    s, c = session()
    c.begin(['actions'])
    c.complete()
    s.guard('modify', {'item': 'A'}, lambda: {'ok': True}, action_id='a')
    config = rule_config({'op': 'subset', 'args': [
        {'lit': ['A']}, {'ref': 'collections.done'}]}, phase='terminal')
    config['collections'] = {'done': {'tools': ['modify'], 'path': 'args.item'}}
    result = check(s, config)
    assert result['status'] == 'unresolved' and result['verdict'] is None


def test_inapplicable_obligation_does_not_require_unneeded_selection():
    s, c = session()
    c.begin(['actions', 'selection'])
    s.guard('modify', {'kind': 'preview'}, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'eq', 'args': [
        {'ref': 'action.args.items'}, {'ref': 'evidence.selection.items'}]},
        when={'op': 'eq', 'args': [{'ref': 'action.args.kind'}, {'lit': 'commit'}]})
    config['observations'] = {'selection': {'name': 'selection', 'source': 'user'}}
    assert check(s, config)['status'] == 'satisfied'


def test_changed_policy_is_bound_without_mutating_existing_profile():
    s, c = session()
    c.begin(['actions'])
    s.guard('modify', {'amount': 20}, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'le', 'args': [{'ref': 'action.args.amount'}, {'lit': 10}]})
    old = api().register_workflow(config)
    config['rules']['test']['condition']['args'][1]['lit'] = 25
    new = api().register_workflow(config)
    assert old.profile_hash != new.profile_hash
    assert api().evaluate_workflow(s.records, {'observation_profile': old.profile_hash,
                                               'rule': 'test'})['status'] == 'violated'
    assert api().evaluate_workflow(s.records, {'observation_profile': new.profile_hash,
                                               'rule': 'test'})['status'] == 'satisfied'


@pytest.mark.parametrize('authority,want', [(None, 'violated'), ('human', 'satisfied'),
                                          ('policy', 'violated'), ('auto_review', 'violated')])
def test_explicit_human_approval_does_not_accept_a_native_policy_grant(authority, want):
    s, c = session()
    c.begin(['actions', 'authorization'])
    if authority:
        c.authorize(action_id='a', request_id='r', tool='modify', args={'item': 'A'},
                    decision='allow', authority=authority, policy_id='native-policy')
    s.guard('modify', {'item': 'A'}, lambda: {'ok': True}, action_id='a')
    c.complete()
    config = rule_config({'op': 'and', 'args': [
        {'op': 'eq', 'args': [{'ref': 'authorization.decision'}, {'lit': 'allow'}]},
        {'op': 'eq', 'args': [{'ref': 'authorization.authority'}, {'lit': 'human'}]}]})
    assert check(s, config)['status'] == want


@pytest.mark.parametrize('offer,output,want', [
    (20, '{"price":20}', 'satisfied'),
    (2, '{"price":2}', 'violated'),
    (20, '{"price":2}', 'violated'),
    (20, '{"price":20} plus a new claim', 'violated'),
])
def test_actual_rendered_offer_must_be_faithful_and_independently_valid(offer, output, want):
    s, c = session()
    c.begin(['actions', 'offer'])
    c.observe('offer', {'price': 20}, source='service')
    s.guard('modify', {'price': offer}, lambda: {'text': output}, action_id='a')
    c.complete()
    config = rule_config({'op': 'eq', 'args': [
        {'ref': 'action.args.price'}, {'ref': 'evidence.offer.price'}]},
        action_rendering={'fields': ['price'], 'path': 'result.text'})
    config['observations'] = {'offer': {'name': 'offer', 'source': 'service'}}
    assert check(s, config)['status'] == want


def test_controller_renderer_displays_exact_declared_fields_and_rejects_extra_prose():
    m = api()
    assert hasattr(m, 'render_action'), 'typed action renderer missing'
    assert m.render_action(['price', 'eligible'], {'price': 20, 'eligible': True}) == '{"eligible":true,"price":20}'
    with pytest.raises(ValueError, match='fields'):
        m.render_action(['price'], {'price': 20, 'extra': 'unchecked claim'})
    s, c = session()
    c.begin(['actions'])
    s.guard('modify', {'price': 20, 'extra': 'unchecked claim'},
            lambda: {'text': '{"price":20}'}, action_id='a')
    c.complete()
    config = rule_config({'lit': True}, action_rendering={'fields': ['price'], 'path': 'result.text'})
    assert check(s, config)['status'] == 'violated'
