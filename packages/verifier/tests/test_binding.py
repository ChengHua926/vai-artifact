"""Adjudication checks received claim evidence against on-chain commitments, never provider storage."""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from aa_commons import ActionRecord, params_hash, trace_hash
from aa_commons.ids import keccak_hex
from aa_commons.registry import predicate_hash_for
from aa_sdk.evidence import EVIDENCE_WINDOW
from aa_verifier import InboxIntegrityError, ZERO32, process_challenge

PARAMS = {"egress_tools": ["send"], "recipient_allowlist": ["alice"]}
PRED = "egress_within_allowlist"
SID = "0x" + "aa" * 32
ESCROW = "0x" + "11" * 20
FILED = 100                            # the claim's on-chain challengedAt
DEADLINE = FILED + EVIDENCE_WINDOW     # last chain second at which evidence is accepted


def _records(target="alice", sid=SID):
    return [ActionRecord(1, sid, "read", {"path": "a"}, result="x", ts=1),
            ActionRecord(2, sid, "send", {"target": target}, result="ok", ts=2)]


def _dicts(records):
    return [r.to_dict() for r in records]


class StubEscrow:
    """Claim 1 is open on promise 1 over session SID, filed at FILED; chain time is ``now``."""
    def __init__(self, trace_hash_hex, pred_hash_hex=None, params_hash_hex=None, now=FILED + 50):
        self.trace_hash = trace_hash_hex
        self.pred_hash = pred_hash_hex or predicate_hash_for(PRED)
        self.params_hash = params_hash_hex or params_hash(PARAMS)
        self.now = now
        self.verdicts = []
        self.checkpoints = []
        self.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337,
            get_block=lambda _: {"timestamp": self.now}))
        self.contract = SimpleNamespace(address=ESCROW)

    def get_checkpoints(self, sid):
        return self.checkpoints

    def get_challenge(self, cid):
        return (bytes.fromhex(SID[2:]), 1, "0xCHAL", 10, 1, FILED)

    def get_promise(self, pid):
        return ("0xPROV", bytes.fromhex(self.pred_hash[2:]), bytes.fromhex(self.params_hash[2:]), 5, 5, 50, 0)

    def get_session(self, sid):
        return ("0xPROV", "0xUSER", bytes.fromhex(self.trace_hash[2:]), 60, 90, True)

    def submit_verdict(self, verifier_addr, cid, violated):
        self.verdicts.append((cid, violated))
        return {"paidToChallenger": 5 if violated else 0}


class StubInbox:
    """The verifier's own inbox: one stored v2 submission for the claim, or none (records=None).

    ``promise`` supplies the submitted parameters; any other key in it (such as an old
    predicate name) is not part of the v2 payload and is dropped.
    """
    def __init__(self, records, promise=None, provider="0xPROV", **payload_overrides):
        self.records = records
        self.promise = promise if promise is not None else {"params": PARAMS}
        self.provider = provider
        self.overrides = payload_overrides
        self.closed = []

    def get(self, challenge_id):
        if self.records is None:
            return None
        promise = self.promise if isinstance(self.promise, dict) else {}
        payload = {"version": 2, "chain_id": 31337, "escrow_address": ESCROW,
                   "challenge_id": challenge_id, "session_id": SID, "promise_id": 1,
                   "records": self.records, "params": promise.get("params")}
        payload.update(self.overrides)
        return {"received_at": FILED + 10, "provider": self.provider, "payload": payload}

    def close_without_evidence(self, challenge_id, closed_at):
        if self.records is not None:
            return False
        self.closed.append((challenge_id, closed_at))
        return True


def _run(escrow, inbox):
    return process_challenge(escrow, inbox, challenge_id=1, verifier_addr="0xVER")


# ── stored evidence that matches the commitments is judged on its merits ─────────

def test_delivered_data_is_evaluated_by_the_onchain_predicate():
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubInbox(_dicts(records)))
    assert out["violated"] is False and out["predicate"] == PRED   # alice is allow-listed
    assert escrow.verdicts == [(1, False)]


def test_violation_in_delivered_records_pays_the_challenger():
    records = _records(target="eve")
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubInbox(_dicts(records)))
    assert out["violated"] is True and out["seq"] == 2 and out["paid_to_challenger"] == 5
    assert escrow.verdicts == [(1, True)]


def test_stored_evidence_is_evaluated_without_waiting_for_the_deadline():
    """The first accepted submission is final, so there is nothing left to wait for."""
    records = _records()
    escrow = StubEscrow(trace_hash(records), now=FILED)
    assert _run(escrow, StubInbox(_dicts(records)))["violated"] is False
    assert escrow.verdicts == [(1, False)]


def test_evidence_stored_in_time_is_evaluated_after_the_deadline():
    records = _records(target="eve")
    escrow = StubEscrow(trace_hash(records), now=DEADLINE + 86400)
    inbox = StubInbox(_dicts(records))
    out = _run(escrow, inbox)
    assert out["violated"] is True and out["predicate"] == PRED and out["seq"] == 2
    assert inbox.closed == []


def test_empty_trace_committed_and_delivered_is_evaluated():
    """No records with a commitment to the empty trace is matching evidence (every predicate is
    satisfied on it); only evidence that cannot match the commitment rules against the provider."""
    escrow = StubEscrow(trace_hash([]))
    out = _run(escrow, StubInbox([]))
    assert out["violated"] is False
    assert escrow.verdicts == [(1, False)]


def test_a_leftover_predicate_name_changes_nothing():
    """The v2 payload has no predicate field; the on-chain hash alone selects the predicate."""
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubInbox(_dicts(records), predicate="garbage"))
    assert out["violated"] is False and out["predicate"] == PRED


# ── stored evidence that does not match the commitments: against the provider ────

def test_trace_from_another_session_cannot_be_committed_as_this_session_without_checkpoints():
    records = _records(sid="0x" + "bb" * 32)
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubInbox(_dicts(records)))
    assert out["violated"] is True and "another session" in out["reason"]
    assert escrow.verdicts == [(1, True)]


def test_records_that_do_not_hash_to_the_commitment_pay_the_challenger():
    escrow = StubEscrow(trace_hash(_records()))
    tampered = [ActionRecord(1, SID, "read", {"path": "innocent"}, result="x", ts=1)]
    out = _run(escrow, StubInbox(_dicts(tampered)))
    assert out["violated"] is True and out["paid_to_challenger"] == 5 and out["predicate"] is None
    assert "records hash != on-chain traceHash" in out["reason"]
    assert escrow.verdicts == [(1, True)]


def test_no_records_for_a_committed_trace_is_a_violation():
    escrow = StubEscrow(trace_hash(_records()))
    out = _run(escrow, StubInbox([]))
    assert out["violated"] is True and "no records" in out["reason"]


def test_params_mismatch_is_a_violation():
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    other = {"egress_tools": ["send"], "recipient_allowlist": ["alice", "eve"]}   # widened off-chain
    out = _run(escrow, StubInbox(_dicts(records), promise={"params": other}))
    assert out["violated"] is True and "params hash" in out["reason"]


def test_final_trace_must_match_every_historical_checkpoint():
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    escrow.checkpoints = [
        {"record_count": 1, "prefix_hash": trace_hash(_records("eve")[1:])},
        {"record_count": 2, "prefix_hash": trace_hash(records)},
    ]
    out = _run(escrow, StubInbox(_dicts(records)))
    assert out["violated"] is True and "checkpoint prefix mismatch at record 1" in out["reason"]


def test_checkpointed_trace_must_be_numbered_contiguously():
    records = [ActionRecord(1, SID, "read", {"path": "a"}, "x", 1),
               ActionRecord(3, SID, "send", {"target": "alice"}, "ok", 2)]
    escrow = StubEscrow(trace_hash(records))
    escrow.checkpoints = [{"record_count": 1, "prefix_hash": trace_hash(records[:1])}]
    out = _run(escrow, StubInbox(_dicts(records)))
    assert out["violated"] is True and "contiguous" in out["reason"]


def test_matching_checkpoints_leave_normal_adjudication_intact():
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    escrow.checkpoints = [{"record_count": 1, "prefix_hash": trace_hash(records[:1])}]
    assert _run(escrow, StubInbox(_dicts(records)))["violated"] is False


def test_malformed_signed_payload_rules_against_provider():
    """Malformed evidence is attributable to the provider who signed it and cannot wedge the service."""
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    assert _run(escrow, StubInbox([{"garbage": 1}]))["violated"] is True
    assert _run(escrow, StubInbox({"not": "records"}))["violated"] is True
    assert _run(escrow, StubInbox(_dicts(records), promise={"nope": 1}))["violated"] is True
    assert _run(escrow, StubInbox(_dicts(records), promise={"params": ["not", "an", "object"]}))["violated"] is True
    assert escrow.verdicts == [(1, True)] * 4


@pytest.mark.parametrize("field,value", [("chain_id", 1), ("escrow_address", "0x" + "33" * 20),
    ("challenge_id", 2), ("session_id", "0x" + "bb" * 32), ("promise_id", 2)])
def test_inbox_entry_bound_to_another_claim_stops_for_retry(field, value):
    """The receiver checked these bindings before saving, so a mismatch is a verifier fault."""
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    with pytest.raises(InboxIntegrityError):
        _run(escrow, StubInbox(_dicts(records), **{field: value}))
    assert escrow.verdicts == []


def test_inbox_entry_from_another_signer_stops_for_retry():
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    with pytest.raises(InboxIntegrityError):
        _run(escrow, StubInbox(_dicts(records), provider="0xOTHER"))
    assert escrow.verdicts == []


def test_stored_evidence_without_a_final_commitment_is_a_violation():
    """Defensive: the receiver refuses evidence before the final commitment, and a satisfied
    verdict needs it on chain."""
    escrow = StubEscrow(ZERO32)
    out = _run(escrow, StubInbox(_dicts(_records())))
    assert out["violated"] is True and "never committed" in out["reason"]
    assert escrow.verdicts == [(1, True)]


# ── an unevaluatable promise: against the provider ───────────────────────────────

def test_onchain_hash_matching_no_predicate_rules_violated():
    records = _records()
    escrow = StubEscrow(trace_hash(records), pred_hash_hex=keccak_hex(b"not a catalog predicate"))
    out = _run(escrow, StubInbox(_dicts(records)))
    assert out["violated"] is True and out["predicate"] is None
    assert "matches no catalog predicate" in out["reason"]
    assert escrow.verdicts == [(1, True)]


def test_predicate_that_fails_to_evaluate_rules_violated(monkeypatch):
    import aa_commons.registry as registry

    class Failing:
        spec_id = "failing_predicate"

        def evaluate(self, records, params):
            raise RuntimeError("cannot evaluate")

    monkeypatch.setattr(registry, "resolve_hash", lambda _hash: Failing())
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    out = _run(escrow, StubInbox(_dicts(records)))
    assert out["violated"] is True and out["predicate"] == "failing_predicate"
    assert "predicate failed to evaluate" in out["reason"] and "cannot evaluate" in out["reason"]
    assert escrow.verdicts == [(1, True)]


# ── no evidence: wait through the deadline, then settle against the provider ─────

def test_missing_evidence_waits_through_the_deadline_then_is_a_violation():
    escrow = StubEscrow(trace_hash(_records()), now=DEADLINE)
    inbox = StubInbox(None)
    out = _run(escrow, inbox)
    assert out["violated"] is None and out["pending"] is True and "awaiting" in out["reason"]
    assert escrow.verdicts == [] and inbox.closed == []
    escrow.now = DEADLINE + 1
    out = _run(escrow, inbox)
    assert out["violated"] is True and out["predicate"] is None and out["paid_to_challenger"] == 5
    assert "no evidence" in out["reason"]
    assert inbox.closed == [(1, DEADLINE + 1)] and escrow.verdicts == [(1, True)]


def test_missing_evidence_on_an_unfinished_session_is_a_violation_after_the_deadline():
    escrow = StubEscrow(ZERO32, now=DEADLINE + 1)
    assert _run(escrow, StubInbox(None))["violated"] is True
    assert escrow.verdicts == [(1, True)]


# ── infrastructure failure is never a provider verdict ───────────────────────────

@pytest.mark.parametrize("method", ["get_promise", "get_session", "get_checkpoints"])
def test_chain_read_failure_is_not_a_provider_verdict(method):
    records = _records()
    escrow = StubEscrow(trace_hash(records))
    def unavailable(*_args):
        raise ConnectionError("RPC unavailable")
    setattr(escrow, method, unavailable)
    with pytest.raises(ConnectionError):
        _run(escrow, StubInbox(_dicts(records)))
    assert escrow.verdicts == []


def test_inbox_read_failure_is_not_a_provider_verdict():
    escrow = StubEscrow(trace_hash(_records()), now=DEADLINE + 1)
    inbox = StubInbox(None)
    def unavailable(_cid):
        raise OSError("inbox unavailable")
    inbox.get = unavailable
    with pytest.raises(OSError):
        _run(escrow, inbox)
    assert escrow.verdicts == [] and inbox.closed == []
