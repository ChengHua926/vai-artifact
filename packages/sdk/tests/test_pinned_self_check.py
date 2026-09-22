import pytest

from aa_commons import ActionRecord, PredicateSpec, Verdict, registry
from aa_sdk import Accountability


def wrong_upgrade(records, params):
    return Verdict.satisfied()


def test_self_check_resolves_committed_source_not_current_catalog_alias(monkeypatch):
    sid = 'action_within_declared_scope'
    original = registry.get(sid)
    acc = Accountability('pinned-self-check')
    pid = acc.register_promise(sid, {'scoped_tools': ['write'], 'allow_prefixes': ['workspace']}, payout_wei=1)
    registry.register_historical(original)
    try:
        monkeypatch.setitem(registry._REGISTRY, sid, PredicateSpec(sid, 999, wrong_upgrade))
        records = [ActionRecord(1, 's', 'write', {'target': '/outside'}, 'ok', 0)]
        assert acc.self_check(records)[pid].violated
    finally:
        registry._HISTORICAL.remove(original)


def test_self_check_unknown_committed_hash_raises_instead_of_using_current_alias(monkeypatch):
    acc = Accountability('unknown-pinned-self-check')
    acc.register_promise('action_within_declared_scope',
                         {'scoped_tools': ['write'], 'allow_prefixes': ['workspace']}, payout_wei=1)
    # The same inconsistency can occur when recovering a commitment without its installed version.
    monkeypatch.setattr(registry, 'resolve_hash', lambda committed: None)
    with pytest.raises(ValueError, match='committed predicate'):
        acc.self_check([])


def test_registration_copies_parameters_and_rejects_later_mutation():
    original = {'scoped_tools': ['write'], 'allow_prefixes': ['workspace']}
    acc = Accountability('pinned-params')
    pid = acc.register_promise('action_within_declared_scope', original, payout_wei=1)
    original['allow_prefixes'].append('/outside')
    records = [ActionRecord(1, 's', 'write', {'target': '/outside/file'}, 'ok', 0)]
    assert acc.self_check(records)[pid].violated
    acc.promises[0].params['allow_prefixes'].append('/outside')
    with pytest.raises(ValueError, match='committed parameters'):
        acc.self_check(records)
