"""Exercise the real Python sidecar; no model, chain, or remote service."""
import importlib.util
import os
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ['AA_NO_STORE'] = '1'
spec = importlib.util.spec_from_file_location('openclaw_helper', HERE / 'aa_helper/app.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

@pytest.fixture
def client():
    helper.aa_openclaw._sessions.clear()
    return TestClient(helper.app)

def start(client, sid='s1'):
    return client.post('/session', json={'party':'0xalice', 'native_session_id':sid})

def emit(client, phase, sid='s1', aid='a1', **fields):
    return client.post('/observation', json=dict(native_session_id=sid, action_id=aid,
        event_id=f'{aid}:{phase}', phase=phase, tool='exec', args={'command':'printf ok'}, **fields))

def test_session_routing_and_replayed_open(client):
    a = start(client).json(); assert start(client).json()['session_id'] == a['session_id']
    b = start(client, 's2').json(); assert b['session_id'] != a['session_id']
    emit(client, 'execution_started'); emit(client, 'execution_completed', result={'ok':True})
    assert len(helper.aa_openclaw.current('s1')[1].records) == 1
    assert helper.aa_openclaw.current('s2')[1].records == []

def test_pending_is_not_execution_and_prevents_close(client):
    start(client); emit(client, 'execution_started')
    emit(client, 'approval_requested', request_id='q1')
    assert client.post('/end', json={'native_session_id':'s1'}).status_code == 409
    assert all(r.tool != 'exec' for r in helper.aa_openclaw.current('s1')[1].records)
    emit(client, 'approval_resolved', request_id='q1', decision='allow-once', scope='once', authority='human')
    assert client.post('/end', json={'native_session_id':'s1'}).status_code == 409
    emit(client, 'execution_completed', result={'ok':True})
    assert client.post('/end', json={'native_session_id':'s1'}).status_code == 200
    assert client.post('/end', json={'native_session_id':'s1'}).status_code == 200

def test_allow_once_is_bound_to_exact_invocation_and_replays_do_not_duplicate(client):
    start(client); emit(client, 'execution_started')
    grant = dict(request_id='q1', decision='allow-once', scope='once', authority='human')
    assert emit(client, 'approval_resolved', **grant).status_code == 200
    assert emit(client, 'approval_resolved', **grant).status_code == 200
    emit(client, 'execution_completed', result={'ok':True})
    sess = helper.aa_openclaw.current('s1')[1]
    assert len(sess.records) == 2
    acc, _ = helper.aa_openclaw.current('s1')
    consent = next(v for pid, v in acc.self_check(sess.records).items()
        if next(p.predicate for p in acc.promises if p.promise_id == pid) == 'no_destructive_without_consent')
    assert not consent.violated
    emit(client, 'execution_started', aid='a2'); emit(client, 'execution_completed', aid='a2', result={'ok':True})
    assert any(v.violated for v in acc.self_check(sess.records).values())

def test_conflicting_event_replay_is_rejected(client):
    start(client); emit(client, 'execution_started')
    assert emit(client, 'approval_resolved', request_id='q', decision='deny', scope='once', authority='human').status_code == 200
    assert emit(client, 'approval_resolved', request_id='q', decision='allow-once', scope='once', authority='human').status_code == 409
    emit(client, 'execution_completed', result={'blocked':'denied'})
    assert not any(v.violated for v in helper.aa_openclaw.current('s1')[0].self_check(helper.aa_openclaw.current('s1')[1].records).values())

@pytest.mark.parametrize('decision', ['deny', 'timeout', 'cancelled'])
def test_nonapproval_never_fabricates_execution(client, decision):
    start(client); emit(client, 'execution_started')
    emit(client, 'approval_resolved', request_id='q', decision=decision, scope='once', authority='human')
    emit(client, 'execution_completed', result={'blocked':decision})
    records = helper.aa_openclaw.current('s1')[1].records
    assert records[0].tool == 'user_authorization'
    assert not any(v.violated for v in helper.aa_openclaw.current('s1')[0].self_check(records).values())


def test_resolution_requires_explicit_authority_and_scope(client):
    start(client); emit(client, 'execution_started')
    assert emit(client, 'approval_resolved', request_id='q', decision='allow-once').status_code == 422
    assert helper.aa_openclaw.current('s1')[1].records == []


def test_configured_consent_tools_and_payout_are_registered(client, monkeypatch):
    monkeypatch.setenv('AA_CONSENT_TOOLS', 'exec,write')
    monkeypatch.setenv('AA_PAYOUT_WEI', '123')
    assert start(client).status_code == 200
    promises = helper.aa_openclaw.current('s1')[0].promises
    assert promises[1].params['destructive_tools'] == ['exec','write']
    assert all(p.payout_wei == 123 for p in promises)


def test_native_session_cannot_change_its_party(client):
    start(client)
    assert client.post('/session', json={'party':'0xbob', 'native_session_id':'s1'}).status_code == 409


def test_store_outage_keeps_finalization_retryable(client, monkeypatch):
    class Store:
        def __init__(self): self.records = []; self.fail = False
        def append_record(self, sid, record):
            if self.fail: raise ConnectionError('offline')
            self.records.append((sid, record))
        def get_records(self, sid): return [r for s, r in self.records if s == sid]
        def put_promise(self, *args): pass
    store = Store()
    monkeypatch.setattr(helper, '_store', lambda:store)
    start(client); emit(client,'execution_started')
    store.fail = True
    emit(client,'execution_completed',result={'ok':True})
    assert client.post('/end', json={'native_session_id':'s1'}).status_code == 503
    store.fail = False
    assert client.post('/end', json={'native_session_id':'s1'}).json()['n_actions'] == 1
    assert len(store.records) == 1
