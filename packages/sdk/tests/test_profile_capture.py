"""A shared profile must evaluate actual SDK-captured results, not imported verdicts."""
import inspect
import json

from aa_commons import ActionRecord, policy_profiles, registry
from aa_commons.constraints import expr, fact
from aa_sdk import Accountability


def file_observations(records, config):
    return {'trace': [{'seq': r.seq, 'tool': r.tool, 'args': r.args, 'result': r.result} for r in records],
            'sequence_map': {r.seq: r.seq for r in records},
            'inputs': {'scope_checks': {r.seq: [{
                'arm': 'content_size', 'condition': expr('le', fact('written_size', r.result['size']), config['limit']),
                'target': r.args['target'], 'detail': 'written content exceeds configured size'}]
                for r in records if r.tool == 'write_local'}}}


def test_profile_checks_successful_local_capture_and_hash_resolved_replay(tmp_path):
    profile = policy_profiles.register(policy_profiles.ObservationProfile(
        'local_file_capture', 1, file_observations, {'limit': 3}, {'content_size': 'scope'},
        inspect.getsource(file_observations)))
    params = {'observation_profile': profile.profile_hash, 'rule': 'content_size'}
    acc = Accountability('local-profile-capture', chain=None, store=None)
    pid = acc.register_promise('action_within_declared_scope', params, payout_wei=1)
    session = acc.session('local-party')
    target = tmp_path / 'document.txt'

    def write(text):
        target.write_text(text)
        return {'size': len(target.read_text())}

    session.guard('write_local', {'target': str(target)}, lambda: write('abc'))
    assert not acc.self_check(session.records)[pid].violated
    session.guard('write_local', {'target': str(target)}, lambda: write('abcd'))
    assert target.read_text() == 'abcd'
    verdict = acc.self_check(session.records)[pid]
    assert verdict.violated and verdict.seq == 2
    restored = [ActionRecord.from_dict(r) for r in json.loads(json.dumps([r.to_dict() for r in session.records]))]
    spec = registry.resolve_hash(acc.promises[0].predicate_hash)
    assert spec.evaluate(restored, params) == verdict
    assert session.end()['n_actions'] == 2
