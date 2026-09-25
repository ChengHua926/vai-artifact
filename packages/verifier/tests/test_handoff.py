"""Provider evidence is claim-bound, deadline-bound, private until submitted, and durably acknowledged.

The provider's whole answer to a claim is signed evidence in the verifier's inbox by the claim's
filing time plus EVIDENCE_WINDOW. Nothing is sent on chain for it. The verifier settles every
claim: from the stored evidence, or as a violation once the deadline has passed without any.
"""
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from web3 import Web3

from aa_commons import ActionRecord, params_hash, trace_hash
from aa_commons.registry import predicate_hash_for
from aa_sdk.evidence import EVIDENCE_WINDOW

SID = '0x' + 'aa' * 32
ADDRESS = '0x' + '11' * 20
FILED = 100                            # claim 1's on-chain challengedAt
DEADLINE = FILED + EVIDENCE_WINDOW     # last chain second at which evidence is accepted
PARAMS = {'egress_tools': ['send'], 'recipient_allowlist': ['alice']}
RECORDS = [ActionRecord(1, SID, 'send', {'target': 'alice'}, 'ok', 1).to_dict()]
SCRIPTS = Path(__file__).resolve().parents[3] / 'scripts'


class OnlyChallenged:
    """An events namespace that has nothing but Challenged: scanning any other event fails."""
    def __init__(self, logs=()):
        self.logs = list(logs)
        self.scans = []

    def Challenged(self):
        def get_logs(**query):
            self.scans.append(query)
            return self.logs
        return SimpleNamespace(get_logs=get_logs)


class Chain:
    """Public escrow reads for claim 1 (promise 1 over session SID, filed at FILED) and the
    verifier's verdict call. It has no response or default-claim path."""
    def __init__(self, provider, logs=()):
        self.provider = provider
        self.now = 1000
        self.status = 1
        self.final = True
        self.verdicts = []
        self.failed_verdicts = 0
        self.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, block_number=50,
            get_block=lambda _: {'timestamp': self.now}))
        self.contract = SimpleNamespace(address=ADDRESS, events=OnlyChallenged(logs))

    def get_challenge(self, cid):
        return (bytes.fromhex(SID[2:]), 1, '0x' + '22' * 20, 10, self.status, FILED)

    def get_promise(self, pid):
        return (self.provider, bytes.fromhex(predicate_hash_for('egress_within_allowlist')[2:]),
                bytes.fromhex(params_hash(PARAMS)[2:]), 5, 5, 50, 0)

    def get_session(self, sid):
        committed = trace_hash([ActionRecord.from_dict(d) for d in RECORDS]) if self.final else '0x' + '00' * 32
        return (self.provider, '0x' + '22' * 20, bytes.fromhex(committed[2:]), 60, 90 if self.final else 0, True)

    def get_checkpoints(self, sid):
        return []

    def submit_verdict(self, sender, cid, violated):
        if self.status != 1:
            raise RuntimeError('execution reverted: not open')
        if self.failed_verdicts:
            self.failed_verdicts -= 1
            raise ConnectionError('verdict receipt unavailable')
        self.verdicts.append(violated)
        self.status = 2 if violated else 3
        return {'paidToChallenger': 5 if violated else 0}


class ProviderView:
    """What provider code may use: public reads and the Challenged scan. Anything else fails."""
    READS = {'get_challenge', 'get_promise', 'get_session', 'get_checkpoints'}

    def __init__(self, chain):
        self._chain = chain
        self.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, block_number=50,
            get_block=chain.w3.eth.get_block))
        self.contract = SimpleNamespace(address=chain.contract.address, events=chain.contract.events)

    def __getattr__(self, name):
        if name in self.READS:
            return getattr(self._chain, name)
        raise AssertionError(f'provider code used {name}; evidence delivery sends no transaction')


class Store:
    def get_records(self, _): return RECORDS
    def get_promise(self, _): return {'predicate': 'egress_within_allowlist', 'params': PARAMS}


class SealedStore:
    """A provider store that must not be read: the claim cannot take evidence (yet or any more)."""
    def get_records(self, _): raise AssertionError('store read for a claim that cannot take evidence')
    def get_promise(self, _): raise AssertionError('store read for a claim that cannot take evidence')


class BrokenStore:
    def get_records(self, _): raise ConnectionError('provider store unavailable')
    def get_promise(self, _): raise ConnectionError('provider store unavailable')


@pytest.fixture
def handoff(tmp_path):
    from aa_sdk.evidence import create_envelope
    from aa_verifier.inbox import EvidenceInbox, create_app
    provider = Account.create()
    chain = Chain(provider.address, logs=[{'args': {'challengeId': 1}}])
    inbox = EvidenceInbox(tmp_path / 'inbox.sqlite3', 31337, ADDRESS)
    client = TestClient(create_app(chain, inbox))
    envelope = create_envelope(chain, 1, RECORDS, PARAMS, provider)
    return provider, chain, inbox, client, envelope


def post(client, envelope):
    return client.post('/claims/1/evidence', json=envelope)


def closed_rows(inbox):
    with closing(sqlite3.connect(inbox.path)) as db:
        return db.execute('SELECT challenge_id, closed_at FROM closed_without_evidence').fetchall()


def sender(client):
    from aa_sdk.evidence import EvidenceClient
    return EvidenceClient('http://127.0.0.1:8001', session=client)


def load_script(name):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f'{name}_under_test', SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── receiver: authentication, claim binding, first submission is final ─────────────

def test_provider_signed_submission_is_durable_and_idempotent(handoff):
    from aa_verifier.inbox import EvidenceInbox
    _, chain, inbox, client, envelope = handoff
    first = post(client, envelope)
    assert first.status_code == 200
    assert first.json()['received_at'] == chain.now
    restarted = EvidenceInbox(inbox.path, 31337, ADDRESS)
    assert restarted.get(1)['payload']['records'] == RECORDS
    chain.now += 99
    assert post(client, envelope).json() == first.json()


def test_identical_retry_returns_the_receipt_after_the_deadline_and_settlement(handoff):
    _, chain, _, client, envelope = handoff
    first = post(client, envelope).json()
    chain.now, chain.status = DEADLINE + 10, 3
    again = post(client, envelope)
    assert again.status_code == 200 and again.json() == first


def test_first_submission_cannot_be_replaced(handoff):
    from aa_sdk.evidence import create_envelope
    provider, chain, inbox, client, envelope = handoff
    assert post(client, envelope).status_code == 200
    other = create_envelope(chain, 1, [], PARAMS, provider)
    assert post(client, other).status_code == 409
    assert inbox.get(1)['payload']['records'] == RECORDS


def test_outsider_cannot_submit_or_read_evidence(handoff):
    from aa_sdk.evidence import create_envelope
    _, chain, inbox, client, _ = handoff
    envelope = create_envelope(chain, 1, RECORDS, PARAMS, Account.create())
    assert post(client, envelope).status_code == 403
    assert inbox.get(1) is None
    assert client.get('/claims/1/evidence').status_code == 405
    assert client.get('/sessions/' + SID + '/records').status_code == 404


def test_tampered_signature_rejected_without_claim_attribution(handoff):
    _, _, inbox, client, envelope = handoff
    envelope['payload']['params'] = {'recipient_allowlist': ['eve']}
    assert post(client, envelope).status_code == 403
    assert inbox.get(1) is None


@pytest.mark.parametrize('field,value', [('chain_id', 1), ('escrow_address', '0x' + '33' * 20),
    ('challenge_id', 2), ('session_id', '0x' + 'bb' * 32), ('promise_id', 2), ('version', 1)])
def test_signature_cannot_be_replayed_in_another_claim_or_domain(handoff, field, value):
    from aa_sdk.evidence import sign_payload
    provider, _, inbox, client, envelope = handoff
    envelope['payload'][field] = value
    assert post(client, sign_payload(envelope['payload'], provider)).status_code == 400
    assert inbox.get(1) is None


def test_missing_session_binding_rejected_as_invalid_request(handoff):
    from aa_sdk.evidence import sign_payload
    provider, _, inbox, client, envelope = handoff
    del envelope['payload']['session_id']
    assert post(client, sign_payload(envelope['payload'], provider)).status_code == 400
    assert inbox.get(1) is None


@pytest.mark.parametrize('extra', ['predicate', 'note'])
def test_payload_with_fields_outside_v2_rejected(handoff, extra):
    """Every signed field must decide something; unused fields are refused, not ignored."""
    from aa_sdk.evidence import sign_payload
    provider, _, inbox, client, envelope = handoff
    envelope['payload'][extra] = 'AAP-1'
    assert post(client, sign_payload(envelope['payload'], provider)).status_code == 400
    assert inbox.get(1) is None


def test_envelope_has_no_predicate_field(handoff):
    _, _, _, _, envelope = handoff
    assert set(envelope['payload']) == {'version', 'chain_id', 'escrow_address', 'challenge_id',
                                        'session_id', 'promise_id', 'records', 'params'}
    assert envelope['payload']['version'] == 2


# ── receiver: when evidence can be accepted ────────────────────────────────────────

def test_delivery_before_final_commitment_is_refused_and_keeps_the_slot_free(handoff):
    from aa_sdk.evidence import create_envelope
    provider, chain, inbox, client, envelope = handoff
    chain.final = False
    early = post(client, create_envelope(chain, 1, [], PARAMS, provider))
    assert early.status_code == 409 and 'final trace commitment' in early.json()['detail']
    assert inbox.get(1) is None
    chain.final = True
    assert post(client, envelope).status_code == 200      # a different submission is still accepted
    assert inbox.get(1)['payload']['records'] == RECORDS


def test_delivery_is_accepted_through_the_deadline_second(handoff):
    _, chain, inbox, client, envelope = handoff
    chain.now = DEADLINE
    assert post(client, envelope).status_code == 200
    assert inbox.get(1)['received_at'] == DEADLINE


def test_late_delivery_is_refused(handoff):
    _, chain, inbox, client, envelope = handoff
    chain.now = DEADLINE + 1
    late = post(client, envelope)
    assert late.status_code == 409 and 'deadline' in late.json()['detail']
    assert inbox.get(1) is None


def test_delivery_to_a_settled_claim_is_refused(handoff):
    _, chain, inbox, client, envelope = handoff
    chain.status = 2
    assert post(client, envelope).status_code == 409
    assert inbox.get(1) is None


def test_receiver_infrastructure_failure_is_an_error_not_a_refusal(handoff):
    _, chain, inbox, client, envelope = handoff
    def unavailable(_cid):
        raise ConnectionError('RPC unavailable')
    chain.get_challenge = unavailable
    with pytest.raises(ConnectionError):
        post(client, envelope)
    assert inbox.get(1) is None


# ── verifier: every claim is settled from the inbox ────────────────────────────────

def test_evidence_delivered_before_the_deadline_is_evaluated_without_waiting(handoff):
    from aa_verifier import process_challenge
    _, chain, inbox, client, envelope = handoff
    assert post(client, envelope).status_code == 200
    result = process_challenge(chain, inbox, 1, 'verifier')
    assert result['violated'] is False and result['predicate'] == 'egress_within_allowlist'
    assert chain.verdicts == [False] and closed_rows(inbox) == []


def test_signed_bad_evidence_is_stored_then_rules_against_provider(handoff):
    from aa_sdk.evidence import create_envelope
    from aa_verifier import process_challenge
    provider, chain, inbox, client, _ = handoff
    bad = create_envelope(chain, 1, {'not': 'records'}, {'wrong': 'params'}, provider)
    assert post(client, bad).status_code == 200
    result = process_challenge(chain, inbox, 1, 'verifier')
    assert result['violated'] is True and 'does not match the commitments' in result['reason']
    assert chain.verdicts == [True]


def test_missing_evidence_is_pending_through_the_deadline(handoff):
    from aa_verifier import process_challenge
    _, chain, inbox, _, _ = handoff
    for now in (FILED, 1000, DEADLINE):
        chain.now = now
        result = process_challenge(chain, inbox, 1, 'verifier')
        assert result['violated'] is None and result['pending'] is True
    assert chain.verdicts == [] and closed_rows(inbox) == []


def test_missing_evidence_after_the_deadline_closes_the_claim_as_violated(handoff):
    from aa_verifier import process_challenge
    from aa_verifier.inbox import ClaimClosedWithoutEvidence
    provider, chain, inbox, client, envelope = handoff
    chain.now = DEADLINE + 1
    result = process_challenge(chain, inbox, 1, 'verifier')
    assert result['violated'] is True and result['paid_to_challenger'] == 5
    assert result['predicate'] is None and 'no evidence' in result['reason']
    assert closed_rows(inbox) == [(1, DEADLINE + 1)] and chain.verdicts == [True]
    assert post(client, envelope).status_code == 409
    with pytest.raises(ClaimClosedWithoutEvidence):
        inbox.save(1, envelope, provider.address, DEADLINE)
    assert inbox.get(1) is None
    assert process_challenge(chain, inbox, 1, 'verifier')['pending'] is False


def test_failed_verdict_submission_is_resubmitted_after_close(handoff):
    from aa_verifier import process_challenge
    from aa_verifier.inbox import ClaimClosedWithoutEvidence
    provider, chain, inbox, _, envelope = handoff
    chain.now, chain.failed_verdicts = DEADLINE + 1, 1
    with pytest.raises(ConnectionError):
        process_challenge(chain, inbox, 1, 'verifier')
    assert chain.verdicts == [] and closed_rows(inbox) == [(1, DEADLINE + 1)]
    with pytest.raises(ClaimClosedWithoutEvidence):          # the close already decided the claim
        inbox.save(1, envelope, provider.address, DEADLINE)
    chain.now = DEADLINE + 60
    assert process_challenge(chain, inbox, 1, 'verifier')['violated'] is True
    assert chain.verdicts == [True] and closed_rows(inbox) == [(1, DEADLINE + 1)]


def test_save_after_close_is_refused(handoff):
    from aa_verifier.inbox import ClaimClosedWithoutEvidence
    provider, _, inbox, _, envelope = handoff
    assert inbox.close_without_evidence(1, DEADLINE + 1) is True
    assert inbox.close_without_evidence(1, DEADLINE + 2) is True           # idempotent
    with pytest.raises(ClaimClosedWithoutEvidence):
        inbox.save(1, envelope, provider.address, DEADLINE)
    assert inbox.get(1) is None and closed_rows(inbox) == [(1, DEADLINE + 1)]


def test_close_after_save_returns_false_and_evidence_is_evaluated(handoff):
    from aa_verifier import process_challenge
    provider, chain, inbox, _, envelope = handoff
    inbox.save(1, envelope, provider.address, DEADLINE)
    assert inbox.close_without_evidence(1, DEADLINE + 1) is False
    assert closed_rows(inbox) == []
    chain.now = DEADLINE + 1
    result = process_challenge(chain, inbox, 1, 'verifier')
    assert result['violated'] is False and chain.verdicts == [False]


def test_delivery_whose_write_follows_the_close_is_refused(handoff):
    """Race, close first: the receiver passed its deadline check at D, then the chain moved past
    D and the verifier closed the claim before the receiver's write. The write loses."""
    from aa_verifier import process_challenge
    _, chain, inbox, client, envelope = handoff
    reached, release = threading.Event(), threading.Event()
    original, calls = chain.get_session, []
    def held_after_deadline_check(sid):
        calls.append(sid)
        if len(calls) == 1:
            reached.set()
            assert release.wait(10)
        return original(sid)
    chain.get_session = held_after_deadline_check
    chain.now = DEADLINE
    response = {}
    delivery = threading.Thread(target=lambda: response.update(http=post(client, envelope)))
    delivery.start()
    try:
        assert reached.wait(10)
        chain.now = DEADLINE + 1
        result = process_challenge(chain, inbox, 1, 'verifier')
        assert result['violated'] is True and 'no evidence' in result['reason']
    finally:
        release.set()
        delivery.join(10)
    assert response['http'].status_code == 409
    assert 'without evidence' in response['http'].json()['detail']
    assert inbox.get(1) is None and chain.verdicts == [True]


def test_delivery_that_commits_between_the_read_and_the_close_is_evaluated(handoff, monkeypatch):
    """Race, delivery first: the receiver passed its deadline check at D; the verifier then read
    an empty slot at D+1, but the receiver's write committed before the verifier's close. The
    close returns False and the verifier evaluates the evidence."""
    from aa_verifier import process_challenge
    _, chain, inbox, client, envelope = handoff
    reached, release = threading.Event(), threading.Event()
    original, calls = chain.get_session, []
    def held_after_deadline_check(sid):
        calls.append(sid)
        if len(calls) == 1:
            reached.set()
            assert release.wait(10)
        return original(sid)
    chain.get_session = held_after_deadline_check
    chain.now = DEADLINE
    response = {}
    delivery = threading.Thread(target=lambda: response.update(http=post(client, envelope)))
    delivery.start()
    try:
        assert reached.wait(10)
        chain.now = DEADLINE + 1
        close = inbox.close_without_evidence
        closes = []
        def close_after_the_write(cid, closed_at):
            release.set()
            delivery.join(10)                    # the receiver's durable write commits first
            closes.append(close(cid, closed_at))
            return closes[-1]
        monkeypatch.setattr(inbox, 'close_without_evidence', close_after_the_write)
        result = process_challenge(chain, inbox, 1, 'verifier')
    finally:
        release.set()
        delivery.join(10)
    assert response['http'].status_code == 200 and closes == [False]
    assert result['violated'] is False and result['predicate'] == 'egress_within_allowlist'
    assert chain.verdicts == [False] and closed_rows(inbox) == []


def test_close_after_a_committed_delivery_evaluates_the_evidence(handoff):
    """Delivery at the deadline second: evidence written at D is evaluated after D."""
    from aa_verifier import process_challenge
    _, chain, inbox, client, envelope = handoff
    chain.now = DEADLINE
    assert post(client, envelope).status_code == 200
    chain.now = DEADLINE + 1
    result = process_challenge(chain, inbox, 1, 'verifier')
    assert result['violated'] is False and chain.verdicts == [False]
    assert closed_rows(inbox) == []


def test_concurrent_save_and_close_have_exactly_one_outcome(handoff):
    from aa_verifier.inbox import ClaimClosedWithoutEvidence
    provider, _, inbox, _, envelope = handoff
    outcomes = {}
    def save(cid, barrier):
        barrier.wait()
        try:
            inbox.save(cid, envelope, provider.address, DEADLINE)
            outcomes[cid, 'saved'] = True
        except ClaimClosedWithoutEvidence:
            outcomes[cid, 'saved'] = False
    def close(cid, barrier):
        barrier.wait()
        outcomes[cid, 'closed'] = inbox.close_without_evidence(cid, DEADLINE + 1)
    for cid in range(10, 40):
        barrier = threading.Barrier(2)
        threads = [threading.Thread(target=save, args=(cid, barrier)),
                   threading.Thread(target=close, args=(cid, barrier))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert outcomes[cid, 'saved'] != outcomes[cid, 'closed']
        assert (inbox.get(cid) is not None) == outcomes[cid, 'saved']
    closed = {cid for cid, _ in closed_rows(inbox)}
    assert closed == {cid for cid in range(10, 40) if outcomes[cid, 'closed']}


# ── verifier: infrastructure failure never produces a verdict ──────────────────────

def test_inbox_failure_retries_without_verdict(handoff, monkeypatch):
    from aa_verifier import process_challenge
    _, chain, inbox, _, _ = handoff
    def broken(_):
        raise OSError('disk unavailable')
    monkeypatch.setattr(inbox, 'get', broken)
    for now in (1000, DEADLINE + 1):
        chain.now = now
        with pytest.raises(OSError):
            process_challenge(chain, inbox, 1, 'verifier')
    assert chain.verdicts == []


def test_close_failure_retries_without_verdict(handoff, monkeypatch):
    from aa_verifier import process_challenge
    _, chain, inbox, _, _ = handoff
    def locked(*_):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(inbox, 'close_without_evidence', locked)
    chain.now = DEADLINE + 1
    with pytest.raises(sqlite3.OperationalError):
        process_challenge(chain, inbox, 1, 'verifier')
    assert chain.verdicts == []


def test_chain_time_failure_is_infrastructure_error(handoff):
    from aa_verifier import process_challenge
    _, chain, inbox, _, _ = handoff
    def unavailable(_):
        raise ConnectionError('RPC unavailable')
    chain.w3.eth.get_block = unavailable
    with pytest.raises(ConnectionError):
        process_challenge(chain, inbox, 1, 'verifier')
    assert chain.verdicts == [] and closed_rows(inbox) == []


def test_chain_identity_failure_is_infrastructure_error(handoff):
    from aa_verifier import process_challenge
    _, chain, inbox, client, envelope = handoff
    post(client, envelope)
    class BrokenEth:
        @property
        def chain_id(self): raise ValueError('RPC rejected chain identity request')
    chain.w3.eth = BrokenEth()
    with pytest.raises(ValueError, match='RPC'):
        process_challenge(chain, inbox, 1, 'verifier')
    assert chain.verdicts == []


def test_inbox_cannot_be_reused_for_another_contract(handoff):
    from aa_verifier.inbox import EvidenceInbox
    _, _, inbox, _, _ = handoff
    with pytest.raises(ValueError, match='deployment'):
        EvidenceInbox(inbox.path, 31337, '0x' + 'ff' * 20)


# ── provider client: delivery is the whole response ────────────────────────────────

def test_delivery_is_the_whole_response_and_sends_no_transaction(handoff):
    provider, chain, inbox, client, _ = handoff
    view = ProviderView(chain)
    receipt = sender(client).deliver(view, provider.address, provider, Store(), 1)
    assert receipt['evidence_hash'] == inbox.get(1)['evidence_hash']
    assert sender(client).deliver(view, provider.address, provider, Store(), 1) == receipt
    assert chain.verdicts == []


def test_deliver_waits_for_final_commitment_without_reading_the_store(handoff):
    from aa_sdk.evidence import EvidenceNotReady
    provider, chain, inbox, client, _ = handoff
    chain.final = False
    with pytest.raises(EvidenceNotReady):
        sender(client).deliver(ProviderView(chain), provider.address, provider, SealedStore(), 1)
    assert inbox.get(1) is None


@pytest.mark.parametrize('status,now,reason', [(2, 1000, 'no longer open'), (1, DEADLINE + 1, 'deadline')])
def test_deliver_refuses_closed_or_expired_claims(handoff, status, now, reason):
    from aa_sdk.evidence import EvidenceRefused
    provider, chain, inbox, client, _ = handoff
    chain.status, chain.now = status, now
    with pytest.raises(EvidenceRefused, match=reason):
        sender(client).deliver(ProviderView(chain), provider.address, provider, SealedStore(), 1)
    assert inbox.get(1) is None


def test_deliver_rejects_another_signer_or_provider_before_sending(handoff):
    provider, chain, inbox, client, _ = handoff
    with pytest.raises(ValueError, match='signer'):
        sender(client).deliver(ProviderView(chain), provider.address, Account.create(), SealedStore(), 1)
    with pytest.raises(ValueError, match='challenged provider'):
        sender(client).deliver(ProviderView(chain), Account.create().address, provider, SealedStore(), 1)
    assert inbox.get(1) is None


def test_bad_or_absent_receipt_is_not_a_delivery(handoff):
    from aa_sdk.evidence import EvidenceClient
    provider, chain, _, _, _ = handoff
    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {'challenge_id': 1, 'evidence_hash': 'incorrect', 'received_at': 1000}
    class Session:
        def post(self, *args, **kwargs): return Response()
    client = EvidenceClient('http://127.0.0.1:8001', session=Session())
    with pytest.raises(ValueError, match='receipt'):
        client.deliver(ProviderView(chain), provider.address, provider, Store(), 1)


def test_evidence_redirect_is_not_a_delivery_acknowledgment(handoff):
    import requests
    from aa_sdk.evidence import EvidenceClient, evidence_hash
    _, _, _, _, envelope = handoff
    class Response:
        status_code = 307
        def raise_for_status(self): pass
        def json(self):
            return {'challenge_id': 1, 'evidence_hash': evidence_hash(envelope['payload']), 'received_at': 1000}
    class Session:
        def post(self, *args, **kwargs): return Response()
    with pytest.raises(requests.HTTPError, match='redirect'):
        EvidenceClient('https://verifier.example.com', session=Session()).submit(envelope)


def test_remote_plaintext_delivery_is_refused():
    from aa_sdk.evidence import EvidenceClient
    with pytest.raises(ValueError, match='HTTPS'):
        EvidenceClient('http://verifier.example.com')


def test_evidence_deadline_is_filing_time_plus_the_window(handoff):
    from aa_sdk.evidence import evidence_deadline
    _, chain, _, _, _ = handoff
    assert EVIDENCE_WINDOW == 3 * 24 * 60 * 60
    assert evidence_deadline(chain, 1) == DEADLINE


def test_real_http_signed_delivery_and_verdict(handoff):
    import socket
    import time
    import uvicorn
    from aa_sdk.evidence import EvidenceClient
    from aa_verifier import process_challenge
    from aa_verifier.inbox import create_app
    provider, chain, inbox, _, _ = handoff
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(chain, inbox), log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        client = EvidenceClient(f'http://127.0.0.1:{port}')
        receipt = client.deliver(ProviderView(chain), provider.address, provider, Store(), 1)
        assert receipt['evidence_hash'] == inbox.get(1)['evidence_hash']
        assert process_challenge(chain, inbox, 1, 'verifier')['violated'] is False
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_local_unlocked_signer_produces_the_same_authenticated_format():
    import shutil
    import socket
    import subprocess
    import time
    from aa_sdk.evidence import UnlockedSigner, sign_payload, recover_provider
    binary = shutil.which('anvil')
    if not binary:
        pytest.skip('Anvil is required for the local signer integration test')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    child = subprocess.Popen([binary, '--host', '127.0.0.1', '--port', str(port), '--silent'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        w3 = Web3(Web3.HTTPProvider(f'http://127.0.0.1:{port}', request_kwargs={'timeout': 1}))
        deadline = time.monotonic() + 5
        while not w3.is_connected() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert w3.is_connected()
        provider = w3.eth.accounts[1]
        assert recover_provider(sign_payload({'test': 'private evidence'}, UnlockedSigner(w3, provider))) == provider
    finally:
        child.terminate()
        child.wait(timeout=5)


# ── provider service: deliver only ─────────────────────────────────────────────────

def test_provider_listener_waits_for_finalization_then_delivers(handoff):
    service = load_script('provider_service')
    provider, chain, inbox, http, _ = handoff
    view, client = ProviderView(chain), sender(http)
    chain.final = False
    assert service.poll_once(view, Store(), client, provider.address, provider, 40, set()) == (50, {1})
    assert inbox.get(1) is None
    chain.final = True
    assert service.poll_once(view, Store(), client, provider.address, provider, 50, {1}) == (50, set())
    assert inbox.get(1)['payload']['records'] == RECORDS
    assert chain.contract.events.scans == [{'from_block': 41, 'to_block': 50}]   # Challenged only
    assert chain.verdicts == []


@pytest.mark.parametrize('status,now', [(2, 1000), (3, 1000), (1, DEADLINE + 1)])
def test_provider_listener_drops_claims_that_refuse_evidence(handoff, status, now):
    service = load_script('provider_service')
    provider, chain, inbox, http, _ = handoff
    chain.status, chain.now = status, now
    assert service.poll_once(ProviderView(chain), SealedStore(), sender(http), provider.address,
                             provider, 50, {1}) == (50, set())
    assert inbox.get(1) is None


def test_provider_listener_retries_other_failures(handoff):
    from aa_sdk.evidence import create_envelope
    service = load_script('provider_service')
    provider, chain, inbox, http, _ = handoff
    view, client = ProviderView(chain), sender(http)
    assert service.poll_once(view, BrokenStore(), client, provider.address, provider, 50, {1}) == (50, {1})
    assert inbox.get(1) is None
    inbox.save(1, create_envelope(chain, 1, [], PARAMS, provider), provider.address, 1000)
    # The receiver answers 409 to a different submission; the listener keeps the claim until it closes.
    assert service.poll_once(view, Store(), client, provider.address, provider, 50, {1}) == (50, {1})
    chain.status = 2
    assert service.poll_once(view, Store(), client, provider.address, provider, 50, {1}) == (50, set())


def test_provider_listener_ignores_other_providers_claims(handoff):
    service = load_script('provider_service')
    provider, chain, inbox, http, _ = handoff
    other = Account.create()
    assert service.poll_once(ProviderView(chain), SealedStore(), sender(http), other.address, other,
                             50, {1}) == (50, set())
    assert inbox.get(1) is None


# ── verifier service with the real adjudicator ─────────────────────────────────────

def test_verifier_service_keeps_a_silent_claim_across_restart_then_settles_it(handoff, tmp_path):
    service = load_script('verifier_service')
    service.CHECKPOINT = str(tmp_path / 'verifier.cursor')
    _, chain, inbox, _, _ = handoff
    last, pending = service.poll_once(chain, inbox, 'verifier', 40, set(), confirmations=0)
    assert (last, pending) == (50, {1}) and chain.verdicts == []
    service._save_state(last, pending)
    restarted = load_script('verifier_service')
    restarted.CHECKPOINT = service.CHECKPOINT
    last, pending = restarted._load_state(0)
    assert (last, pending) == (50, {1})
    chain.now = DEADLINE
    assert restarted.poll_once(chain, inbox, 'verifier', last, pending, confirmations=0) == (50, {1})
    chain.now = DEADLINE + 1
    assert restarted.poll_once(chain, inbox, 'verifier', last, pending, confirmations=0) == (50, set())
    assert chain.verdicts == [True] and closed_rows(inbox) == [(1, DEADLINE + 1)]
    assert chain.contract.events.scans == [{'from_block': 41, 'to_block': 50}]


def test_claim_not_yet_visible_stays_pending_and_delivery_retries(handoff):
    """A lagging RPC can return an unknown claim (status 0); nothing may drop it."""
    from aa_sdk.evidence import EvidenceClient, EvidenceNotReady
    from aa_verifier import process_challenge
    provider, chain, inbox, client, _ = handoff
    real = chain.get_challenge
    chain.get_challenge = lambda cid: (b'\x00' * 32, 0, '0x' + '00' * 20, 0, 0, 0)
    out = process_challenge(chain, inbox, 1, '0xverifier')
    assert out['violated'] is None and out['pending'] is True
    with pytest.raises(EvidenceNotReady):
        EvidenceClient('http://127.0.0.1:8001').deliver(chain, provider.address, provider, None, 1)
    chain.get_challenge = real
