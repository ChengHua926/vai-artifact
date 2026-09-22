"""aa_verifier — the neutral adjudicator.

Imports aa_commons directly, so it runs the *same* predicate the SDK does (invariant I3 — one
implementation, no drift). On a challenge it reads the commitments from CHAIN (never the store's
self-reported fields), fetches the session's records and the promise params from the store, and
recomputes the hashes against those commitments (I1) — which is what makes a *trusted* store
sound: it can hold data it cannot forge.

Everything the commitments do not vouch for is ONE case, "committed data not produced": the
store cannot be reached, its payload is malformed, the records hash to something other than the
on-chain traceHash (including no records for a session whose commitment is not the empty trace),
or the params hash to something other than the on-chain paramsHash. Availability is attributed,
not assumed (the store rule): if the provider has NOT responded on chain the verifier stands
aside — the challenger's remedy is ``claimDefault`` after the response window; if the provider
HAS responded ("the data is available") and it still is not produced, that is withholding after
an on-chain claim of availability, and the trusted verifier settles it as a violation.

Which predicate runs is decided by the ON-CHAIN predicateHash: the catalog entry whose evaluate
source hashes to it. The store's predicate id is only a hint. A commitment that matches no
catalog predicate is an unevaluatable promise, which rules against the provider who registered
it (``_malformed``), as does a predicate that crashes on the committed data.
"""
from __future__ import annotations

from aa_commons import ActionRecord, params_hash, registry, trace_hash
from .prefixes import check_prefixes_streaming as check_prefixes
from web3 import Web3

ZERO32 = "0x" + "00" * 32


def process_challenge(escrow, store, challenge_id: int, verifier_addr: str) -> dict:
    # 1) read challenge + the promise/session it binds, from chain
    session_id_b, promise_id, _challenger, _bond, _status, _challenged_at, responded_at = \
        escrow.get_challenge(challenge_id)
    session_id = Web3.to_hex(session_id_b)                       # bytes32 -> store key
    _p_provider, pred_hash_b, params_hash_b, _payout, _reserve, _registered_at, _retired_at = \
        escrow.get_promise(promise_id)
    _s_provider, _party, trace_hash_b, _opened_at, _committed_at, _exists = escrow.get_session(session_id)
    onchain_trace_hash = Web3.to_hex(trace_hash_b)
    onchain_pred_hash = Web3.to_hex(pred_hash_b)
    onchain_params_hash = Web3.to_hex(params_hash_b)
    # An RPC failure is verifier infrastructure failure, not provider withholding. Let the
    # service retry; never turn inability to read chain checkpoints into a slashing verdict.
    checkpoints = escrow.get_checkpoints(session_id)

    # 2) the committed data, from the store — or the one reason it is not produced
    records, params, hint, why_not = _committed_data(store, session_id, promise_id,
                                                     onchain_trace_hash, onchain_params_hash)
    if why_not is not None:
        return _not_produced(escrow, verifier_addr, challenge_id, responded_at, why_not)
    try:
        check_prefixes(records, checkpoints, session_id)
    except (ValueError, KeyError, TypeError) as e:
        return _not_produced(escrow, verifier_addr, challenge_id, responded_at, str(e))

    # 3) resolve the predicate by the ON-CHAIN hash (the store's id is a hint, nothing more)
    spec = registry.resolve_hash(onchain_pred_hash)
    if spec is None:
        return _malformed(escrow, verifier_addr, challenge_id, hint,
                          "on-chain predicate hash matches no catalog predicate")
    predicate_id = spec.spec_id

    # 4) run the SAME predicate the SDK uses, then settle on-chain. A predicate that cannot be
    #    evaluated is the provider's breach (they registered promise machinery that doesn't run),
    #    so a crash rules against them — this is what makes malformed-on-purpose self-slashing.
    try:
        verdict = spec.evaluate(records, params)
    except Exception as e:
        return _malformed(escrow, verifier_addr, challenge_id, predicate_id,
                          f"predicate failed to evaluate: {e!r}")
    paid = escrow.submit_verdict(verifier_addr, challenge_id, verdict.violated)
    return {
        "challenge_id": challenge_id,
        "predicate": predicate_id,
        "violated": verdict.violated,
        "seq": verdict.seq,                       # the offending action; on chain only the bit travels
        "reason": verdict.reason,
        "paid_to_challenger": int(paid["paidToChallenger"]),
    }


def resolve_predicate(onchain_pred_hash: str) -> "str | None":
    """The current or historical catalog predicate identified by the committed source hash."""
    spec = registry.resolve_hash(onchain_pred_hash)
    return spec.spec_id if spec else None


def _committed_data(store, session_id: str, promise_id: int,
                    onchain_trace_hash: str, onchain_params_hash: str):
    """Fetch the records and params and check them against the chain. Returns
    ``(records, params, predicate_hint, None)`` when the committed data is produced, else
    ``(None, None, None, why_not)``. Nothing here raises: a malformed or unreachable store is
    the same "not produced" outcome as a mismatching one, so it can never wedge a caller."""
    if onchain_trace_hash == ZERO32:                             # zero hash = never committed
        return None, None, None, "trace never committed on chain"
    try:
        records = [ActionRecord.from_dict(d) for d in store.get_records(session_id)]
        promise_rec = store.get_promise(promise_id)
        params = promise_rec["params"]
        hint = promise_rec.get("predicate")
    except Exception as e:
        return None, None, None, f"store unfetchable or malformed: {e!r}"
    try:
        if trace_hash(records) != onchain_trace_hash:
            return None, None, None, ("no records in the store" if not records
                                      else "records hash != on-chain traceHash")
        if params_hash(params) != onchain_params_hash:
            return None, None, None, "params hash != on-chain paramsHash"
    except (ValueError, TypeError, AttributeError) as e:
        return None, None, None, f"store data is not canonical: {e!r}"
    return records, params, hint, None


def _not_produced(escrow, verifier_addr, challenge_id: int, responded_at: int, detail: str) -> dict:
    if responded_at != 0:
        # the provider claimed availability on-chain and still cannot produce -> violated
        paid = escrow.submit_verdict(verifier_addr, challenge_id, True)
        return {
            "challenge_id": challenge_id,
            "predicate": None,
            "violated": True,
            "reason": f"committed data not produced after on-chain response ({detail})",
            "paid_to_challenger": int(paid["paidToChallenger"]),
        }
    # silence: nothing to adjudicate — the challenger's remedy is claimDefault after the window
    return {
        "challenge_id": challenge_id,
        "predicate": None,
        "violated": None,
        "reason": f"committed data not produced ({detail}); awaiting provider respond or default claim",
        "paid_to_challenger": 0,
    }


def _malformed(escrow, verifier_addr, challenge_id: int, predicate_id, detail: str) -> dict:
    paid = escrow.submit_verdict(verifier_addr, challenge_id, True)
    return {
        "challenge_id": challenge_id,
        "predicate": predicate_id,
        "violated": True,
        "reason": f"malformed promise rules against provider ({detail})",
        "paid_to_challenger": int(paid["paidToChallenger"]),
    }
