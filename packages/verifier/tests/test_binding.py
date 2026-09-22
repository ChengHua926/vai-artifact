"""Locks the verifier's adjudication rules (stub chain + stub store, no network):

- committed data produced — the store's records and the params hash to the on-chain
  commitments — adjudicates normally, with the predicate chosen by the on-chain predicateHash;
- everything else is ONE case, "committed data not produced": no verdict while the provider is
  silent (claimDefault is the challenger's remedy), a violation once the provider has responded
  on chain. Store unreachable, payload malformed, records or params off: all the same case, and
  none of them raises;
- an on-chain predicateHash that matches no catalog predicate rules against the provider.
"""
from __future__ import annotations

import pytest

from aa_commons import ActionRecord, params_hash, trace_hash
from aa_commons.ids import keccak_hex
from aa_commons.registry import predicate_hash_for
from aa_verifier import ZERO32, process_challenge

PARAMS = {"egress_tools": ["send"], "recipient_allowlist": ["alice"]}
PRED = "egress_within_allowlist"
SID = "0x" + "aa" * 32
RESPONDED = 200      # a non-zero respondedAt: the provider said "available" on chain


def _records(target="alice"):
    return [ActionRecord(1, SID, "read", {"path": "a"}, result="x", ts=1),
            ActionRecord(2, SID, "send", {"target": target}, result="ok", ts=2)]


def _dicts(records):
    return [r.to_dict() for r in records]


class StubEscrow:
    def __init__(self, trace_hash_hex, pred_hash_hex=None, params_hash_hex=None, responded_at=0):
        self.trace_hash = trace_hash_hex
        self.pred_hash = pred_hash_hex or predicate_hash_for(PRED)
        self.params_hash = params_hash_hex or params_hash(PARAMS)
        self.responded_at = responded_at
        self.verdicts = []
        self.checkpoints = []

    def get_checkpoints(self, sid):
        return self.checkpoints

    def get_challenge(self, cid):
        return (bytes.fromhex(SID[2:]), 1, "0xCHAL", 10, 1, 100, self.responded_at)

    def get_promise(self, pid):
        return ("0xPROV", bytes.fromhex(self.pred_hash[2:]), bytes.fromhex(self.params_hash[2:]), 5, 5, 50, 0)

    def get_session(self, sid):
        return ("0xPROV", "0xUSER", bytes.fromhex(self.trace_hash[2:]), 60, 90, True)

    def submit_verdict(self, verifier_addr, cid, violated):
        self.verdicts.append((cid, violated))
        return {"paidToChallenger": 5 if violated else 0}


class StubStore:
    """`records` is what GET /sessions/{id}/records returns (a list of dicts) or an exception to
    raise; `promise` the promise row or an exception."""

    def __init__(self, records, promise=None):
        self.records = records
        self.promise = promise if promise is not None else {"predicate": PRED, "params": PARAMS}

    def get_records(self, sid):
        if isinstance(self.records, Exception):
            raise self.records
        return self.records

    def get_promise(self, pid):
        if isinstance(self.promise, Exception):
            raise self.promise
        return self.promise


def _run(escrow, store):
    return process_challenge(escrow, store, challenge_id=1, verifier_addr="0xVER")


# ── produced: adjudicate on the store's records ──────────────────────────────────

def test_produced_data_adjudicates_from_the_stores_records():
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubStore(_dicts(records)))
    assert out["violated"] is False and out["predicate"] == PRED   # alice is allow-listed
    assert escrow.verdicts == [(1, False)]


def test_violation_in_the_stores_records_pays_the_challenger():
    records = _records(target="eve")
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubStore(_dicts(records)))
    assert out["violated"] is True and out["seq"] == 2 and out["paid_to_challenger"] == 5
    assert escrow.verdicts == [(1, True)]


def test_store_predicate_id_is_only_a_hint():
    """A corrupted store id changes nothing: the on-chain hash names the catalog predicate."""
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubStore(_dicts(records), promise={"predicate": "garbage", "params": PARAMS}))
    assert out["violated"] is False and out["predicate"] == PRED
    assert escrow.verdicts == [(1, False)]


def test_empty_trace_committed_and_produced_adjudicates():
    """No records with a commitment to the empty trace is produced data (every predicate is
    satisfied on it); only a commitment the store cannot match is "not produced"."""
    escrow = StubEscrow(trace_hash([]))
    out = _run(escrow, StubStore([]))
    assert out["violated"] is False
    assert escrow.verdicts == [(1, False)]


@pytest.mark.parametrize("responded_at,expected", [(0, None), (RESPONDED, True)])
def test_trace_from_another_session_cannot_be_committed_as_this_session_without_checkpoints(responded_at, expected):
    records = [ActionRecord(1, "0x" + "bb" * 32, "send", {"target": "alice"}, "ok", 1)]
    escrow = StubEscrow(trace_hash(records), responded_at=responded_at)
    out = _run(escrow, StubStore(_dicts(records)))
    assert out["violated"] is expected
    assert "session" in out["reason"]
    assert escrow.verdicts == ([(1, True)] if expected else [])


# ── not produced: one case, two outcomes ─────────────────────────────────────────

def test_mismatch_with_a_silent_provider_stands_aside():
    records = _records()
    escrow = StubEscrow(trace_hash(records))                       # chain committed the real trace
    tampered = [ActionRecord(1, SID, "read", {"path": "innocent"}, result="x", ts=1)]
    out = _run(escrow, StubStore(_dicts(tampered)))
    assert out["violated"] is None
    assert out["reason"].startswith("committed data not produced")
    assert escrow.verdicts == []                                   # the remedy is claimDefault


def test_mismatch_after_an_onchain_response_pays_the_challenger():
    records = _records()
    escrow = StubEscrow(trace_hash(records), responded_at=RESPONDED)
    tampered = [ActionRecord(1, SID, "read", {"path": "innocent"}, result="x", ts=1)]
    out = _run(escrow, StubStore(_dicts(tampered)))
    assert out["violated"] is True and out["paid_to_challenger"] == 5
    assert out["reason"].startswith("committed data not produced after on-chain response")
    assert escrow.verdicts == [(1, True)]


@pytest.mark.parametrize("responded_at,expected", [(0, None), (RESPONDED, True)])
def test_unfetchable_store_is_the_same_case(responded_at, expected):
    records = _records()
    escrow = StubEscrow(trace_hash(records), responded_at=responded_at)
    out = _run(escrow, StubStore(ConnectionError("store down")))
    assert out["violated"] is expected
    assert escrow.verdicts == ([(1, True)] if expected else [])


@pytest.mark.parametrize("responded_at,expected", [(0, None), (RESPONDED, True)])
def test_no_records_for_a_committed_trace_is_not_produced(responded_at, expected):
    records = _records()
    escrow = StubEscrow(trace_hash(records), responded_at=responded_at)
    out = _run(escrow, StubStore([]))
    assert out["violated"] is expected
    assert "no records" in out["reason"]


def test_params_mismatch_is_not_produced():
    records = _records()
    escrow = StubEscrow(trace_hash(records), responded_at=RESPONDED)
    other = {"egress_tools": ["send"], "recipient_allowlist": ["alice", "eve"]}   # widened off-chain
    out = _run(escrow, StubStore(_dicts(records), promise={"predicate": PRED, "params": other}))
    assert out["violated"] is True and "params hash" in out["reason"]


def test_never_committed_session_stands_aside():
    escrow = StubEscrow(ZERO32)
    out = _run(escrow, StubStore(_dicts(_records())))
    assert out["violated"] is None and "never committed" in out["reason"]
    assert escrow.verdicts == []


def test_malformed_store_payload_never_raises():
    """Structurally broken store data is the same non-verdict, so it cannot wedge the service."""
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    assert _run(escrow, StubStore([{"garbage": 1}]))["violated"] is None
    assert _run(escrow, StubStore(_dicts(records), promise={"nope": 1}))["violated"] is None
    assert _run(escrow, StubStore(_dicts(records), promise=KeyError("unknown promise")))["violated"] is None
    assert escrow.verdicts == []


# ── unevaluatable promise: against the provider ──────────────────────────────────

def test_onchain_hash_matching_no_predicate_rules_violated():
    records = _records()
    escrow = StubEscrow(trace_hash(records), pred_hash_hex=keccak_hex(b"not a catalog predicate"))
    out = _run(escrow, StubStore(_dicts(records)))
    assert out["violated"] is True and "malformed promise" in out["reason"]
    assert out["predicate"] == PRED                                # the store's hint, reported as such
    assert escrow.verdicts == [(1, True)]


@pytest.mark.parametrize("responded_at,expected", [(0, None), (RESPONDED, True)])
def test_final_trace_must_match_every_historical_checkpoint(responded_at, expected):
    records = _records()
    escrow = StubEscrow(trace_hash(records), responded_at=responded_at)
    escrow.checkpoints = [
        {"record_count": 1, "prefix_hash": trace_hash(_records("eve")[1:])},
        {"record_count": 2, "prefix_hash": trace_hash(records)},
    ]
    out = _run(escrow, StubStore(_dicts(records)))
    assert out["violated"] is expected
    assert "checkpoint" in out["reason"]


def test_matching_checkpoints_leave_normal_adjudication_intact():
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    escrow.checkpoints = [{"record_count": 1, "prefix_hash": trace_hash(records[:1])}]
    assert _run(escrow, StubStore(_dicts(records)))["violated"] is False


def test_checkpoint_read_failure_is_not_a_provider_verdict():
    records = _records()
    escrow = StubEscrow(trace_hash(records), responded_at=RESPONDED)
    def unavailable(sid):
        raise ConnectionError("RPC unavailable")
    escrow.get_checkpoints = unavailable
    with pytest.raises(ConnectionError):
        _run(escrow, StubStore(_dicts(records)))
    assert escrow.verdicts == []
