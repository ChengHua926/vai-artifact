"""Trusted verifier: settle every accepted claim from provider-submitted evidence.

The verifier has only its own claim inbox, never access to the provider's private store. The
provider's response to a claim is its signed evidence; nothing else counts. Evidence must reach
the inbox by the claim's on-chain filing time plus EVIDENCE_WINDOW. Missing evidence, evidence
that does not match the on-chain commitments, an unknown predicate, or a predicate that fails
to evaluate rules against the provider. RPC and inbox faults propagate for retry; they never
produce a verdict.
"""
from __future__ import annotations

from aa_commons import ActionRecord, params_hash, registry, trace_hash
from aa_sdk.evidence import EVIDENCE_WINDOW
from .prefixes import check_prefixes_streaming as check_prefixes
from web3 import Web3

ZERO32 = "0x" + "00" * 32


class InboxIntegrityError(RuntimeError):
    """A stored submission does not match its claim. The receiver checked these bindings before
    saving, so this is a verifier fault: it is raised for retry, never charged to the provider."""


def process_challenge(escrow, inbox, challenge_id: int, verifier_addr: str) -> dict:
    session_id_b, promise_id, _challenger, _bond, status, filed_at = escrow.get_challenge(challenge_id)
    if int(status) == 0:   # an RPC answering from behind the filing block: keep the claim
        return _waiting(challenge_id, 'claim not visible yet')
    if int(status) != 1:
        return _waiting(challenge_id, 'claim is no longer open', pending=False)
    submission = inbox.get(challenge_id)
    if submission is None:
        now = int(escrow.w3.eth.get_block('latest')['timestamp'])
        if now <= int(filed_at) + EVIDENCE_WINDOW:
            return _waiting(challenge_id, 'awaiting provider evidence')
        if inbox.close_without_evidence(challenge_id, now):
            return _violation(escrow, verifier_addr, challenge_id, None,
                              'no evidence received by the deadline')
        submission = inbox.get(challenge_id)   # a delivery committed just before the close

    # Reads remain outside the evidence checks: infrastructure failure must not produce a verdict.
    session_id = Web3.to_hex(session_id_b)
    provider, pred_hash_b, params_hash_b, _payout, _reserve, _registered, _retired = escrow.get_promise(promise_id)
    _provider, _party, trace_hash_b, _opened, _committed, _exists = escrow.get_session(session_id)
    checkpoints = escrow.get_checkpoints(session_id)
    # The receiver authenticated this claim-bound payload before saving it. A mismatch here means
    # the verifier's own inbox is wrong, so it stops this claim for retry instead of ruling on it.
    chain_id = int(escrow.w3.eth.chain_id)
    payload = submission['payload']
    if (payload.get('chain_id') != chain_id
            or str(payload.get('escrow_address', '')).lower() != escrow.contract.address.lower()
            or payload.get('challenge_id') != challenge_id or payload.get('session_id') != session_id
            or payload.get('promise_id') != promise_id
            or str(submission['provider']).lower() != provider.lower()):
        raise InboxIntegrityError(f'inbox entry for claim {challenge_id} does not match the claim')
    try:
        records, params = _committed_data(payload, Web3.to_hex(trace_hash_b), Web3.to_hex(params_hash_b))
        check_prefixes(records, checkpoints, session_id)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        return _violation(escrow, verifier_addr, challenge_id, None,
                          f'evidence does not match the commitments ({error})')

    spec = registry.resolve_hash(Web3.to_hex(pred_hash_b))
    if spec is None:
        return _violation(escrow, verifier_addr, challenge_id, None,
                          'on-chain predicate hash matches no catalog predicate')
    try:
        verdict = spec.evaluate(records, params)
    except Exception as error:
        return _violation(escrow, verifier_addr, challenge_id, spec.spec_id,
                          f'predicate failed to evaluate: {error!r}')
    paid = escrow.submit_verdict(verifier_addr, challenge_id, verdict.violated)
    return {'challenge_id': challenge_id, 'predicate': spec.spec_id, 'violated': verdict.violated,
            'seq': verdict.seq, 'reason': verdict.reason,
            'paid_to_challenger': int(paid['paidToChallenger'])}


def resolve_predicate(onchain_pred_hash: str) -> 'str | None':
    spec = registry.resolve_hash(onchain_pred_hash)
    return spec.spec_id if spec else None


def _committed_data(payload, onchain_trace_hash, onchain_params_hash):
    if onchain_trace_hash == ZERO32:
        raise ValueError('trace never committed on chain')
    if not isinstance(payload['records'], list) or not isinstance(payload['params'], dict):
        raise ValueError('trace must be a record list and parameters must be an object')
    records = [ActionRecord.from_dict(item) for item in payload['records']]
    params = payload['params']
    if trace_hash(records) != onchain_trace_hash:
        raise ValueError('no records in the submission' if not records else 'records hash != on-chain traceHash')
    if params_hash(params) != onchain_params_hash:
        raise ValueError('params hash != on-chain paramsHash')
    return records, params


def _waiting(challenge_id, reason, pending=True):
    return {'challenge_id': challenge_id, 'predicate': None, 'violated': None,
            'reason': reason, 'paid_to_challenger': 0, 'pending': pending}


def _violation(escrow, verifier_addr, challenge_id, predicate_id, reason):
    paid = escrow.submit_verdict(verifier_addr, challenge_id, True)
    return {'challenge_id': challenge_id, 'predicate': predicate_id, 'violated': True,
            'reason': reason, 'paid_to_challenger': int(paid['paidToChallenger'])}
