"""Registration and replay must bind both the generic checker and domain observations."""
import importlib
import inspect
import json

import pytest

from aa_commons import ActionRecord, registry, validate_params
from aa_sdk import Accountability


def _api():
    import aa_commons
    assert hasattr(aa_commons, 'policy_profiles'), 'shared observation profiles are not available'
    return aa_commons.policy_profiles


def _observe(records, config):
    from aa_commons.constraints import expr, fact
    return {'trace': [{'seq': r.seq, 'tool': r.tool, 'args': r.args, 'result': r.result} for r in records],
            'inputs': {'scope_checks': {r.seq: [{'arm': 'limit',
                       'condition': expr('le', fact('amount', r.args['amount']), config['cap']),
                       'target': r.args['amount'], 'allowed': [], 'detail': 'amount exceeds limit'}]
                       for r in records}}, 'sequence_map': {r.seq: r.seq for r in records}}


def _profile(cap=10):
    api = _api()
    return api.register(api.ObservationProfile('test_amount', 1, _observe, {'cap': cap},
                        {'limit': 'scope'}, inspect.getsource(_observe)))


def test_profile_is_bound_in_params_and_operand_changes_verdict():
    profile = _profile()
    params = {'observation_profile': profile.profile_hash, 'rule': 'limit'}
    acc = Accountability('profile-test')
    pid = acc.register_promise('action_within_declared_scope', params, payout_wei=1)
    good = [ActionRecord(1, 's', 'purchase', {'amount': 10}, 'ok', 0)]
    bad = [ActionRecord(1, 's', 'purchase', {'amount': 11}, 'ok', 0)]
    assert not acc.self_check(good)[pid].violated
    verdict = acc.self_check(bad)[pid]
    assert verdict.violated and verdict.seq == 1
    spec = registry.resolve_hash(registry.predicate_hash_for('action_within_declared_scope'))
    restored = [ActionRecord.from_dict(r.to_dict()) for r in bad]
    assert spec.evaluate(restored, params) == verdict
    assert len([s for s in registry.catalog() if s['number'].startswith('AAP-')]) == 5


def test_profile_policy_change_changes_identity_and_cannot_upgrade_old_promise():
    old, new = _profile(10), _profile(20)
    assert old.profile_hash != new.profile_hash
    spec = registry.get('action_within_declared_scope')
    record = [ActionRecord(1, 's', 'purchase', {'amount': 11}, 'ok', 0)]
    assert spec.evaluate(record, {'observation_profile': old.profile_hash, 'rule': 'limit'}).violated
    assert not spec.evaluate(record, {'observation_profile': new.profile_hash, 'rule': 'limit'}).violated


def test_unknown_profile_wrong_family_and_extra_params_reject():
    profile = _profile()
    with pytest.raises(ValueError, match='profile'):
        validate_params('action_within_declared_scope', {'observation_profile': '0x' + '0'*64, 'rule': 'limit'})
    with pytest.raises(ValueError, match='family'):
        validate_params('payment_within_mandate', {'observation_profile': profile.profile_hash, 'rule': 'limit'})
    with pytest.raises(ValueError, match='param'):
        validate_params('action_within_declared_scope', {'observation_profile': profile.profile_hash, 'rule': 'limit', 'cap': 100})


def test_legacy_scope_hash_retains_original_behavior():
    _api()
    from aa_commons.ids import predicate_hash
    from aa_commons.predicates import legacy_scope_v3
    original = predicate_hash(inspect.getsource(legacy_scope_v3.evaluate))
    old = registry.resolve_hash(original)
    assert old is not None
    params = {'scoped_tools': ['write'], 'allow_prefixes': ['workspace']}
    records = [ActionRecord(1, 's', 'write', {'target': 'workspace/../outside'}, 'ok', 0)]
    assert old.evaluate(records, params).violated
    assert old.evaluate(records, params) == registry.get('action_within_declared_scope').evaluate(records, params)
