"""Checkpoints bind every acknowledged prefix while actions continue through outages."""
import threading
import time

import pytest

from aa_commons import ActionRecord, trace_hash
from aa_sdk import Accountability
from test_streaming import FakeStore, StubChain


class CheckpointChain(StubChain):
    def __init__(self):
        super().__init__()
        self.checkpoints = []
        self.fail = False
        self.called = threading.Event()

    def checkpoint_trace(self, provider, sid, count, h):
        self.called.set()
        if self.fail:
            raise ConnectionError("chain unavailable")
        self.checkpoints.append({"record_count": count, "prefix_hash": h})

    def get_checkpoints(self, sid):
        return list(self.checkpoints)


def setup(count=2, seconds=30):
    store, chain = FakeStore(), CheckpointChain()
    acc = Accountability("p", store=store, chain=chain, provider_addr="0xPROV",
                         checkpoint_records=count, checkpoint_seconds=seconds)
    return acc, acc.session("party"), store, chain


def test_checkpoint_binds_prefix_and_does_not_finalize():
    acc, sess, store, chain = setup()
    sess.record("read", {}, "ok")
    sess.record("write", {}, "done")
    assert chain.called.wait(1)
    sess.checkpoint(force=True)  # joins/serializes with automatic work, no duplicate prefix
    assert len(chain.checkpoints) == 1
    assert chain.checkpoints[0]["record_count"] == 2
    assert chain.checkpoints[0]["prefix_hash"] == store.trace_hash_of(sess.session_id)
    assert chain.commits == []
    sess.record("read", {}, "last")
    assert sess.end()["n_actions"] == 3
    assert len(chain.commits) == 1
    assert sess.end()["n_actions"] == 3  # close retry must not send another final transaction
    assert len(chain.commits) == 1


def test_checkpoint_failure_keeps_actions_and_reports_unanchored_tail():
    _, sess, store, chain = setup()
    chain.fail = True
    sess.record("write", {}, "one")
    sess.record("write", {}, "two")
    assert chain.called.wait(1)
    assert sess.checkpoint(force=True)["success"] is False
    assert len(store.shipped) == 2
    status = sess.checkpoint_status()
    assert status["unanchored_records"] == 2
    assert status["unanchored_since"] is not None
    assert status["last_error"]
    chain.fail = False
    assert sess.checkpoint(force=True)["success"] is True
    assert sess.checkpoint_status()["unanchored_records"] == 0
    sess.end()


def test_dirty_timer_anchors_without_another_action():
    _, sess, _, chain = setup(count=10, seconds=0.03)
    sess.record("write", {}, "done")
    assert chain.called.wait(1)
    sess.end()
    assert chain.checkpoints[0]["record_count"] == 1


def test_final_and_recovery_reject_rewritten_checkpoint_prefix():
    acc, sess, store, chain = setup(count=100)
    sess.record("write", {"path": "bad"}, "done")
    assert sess.checkpoint(force=True)["success"]
    original = store.shipped[0][1]
    store.shipped[0] = (sess.session_id, {**original, "args": {"path": "good"}})
    with pytest.raises(ValueError, match="checkpoint"):
        sess.end()
    with pytest.raises(ValueError, match="checkpoint"):
        acc.recover(sess.session_id, "party")
    store.shipped[0] = (sess.session_id, original)
    sess.end()


def test_recovery_checks_earlier_checkpoint_not_only_latest():
    acc, sess, store, chain = setup(count=100)
    sess.record("write", {}, "one")
    assert sess.checkpoint(force=True)["success"]
    sess.record("write", {}, "two")
    assert sess.checkpoint(force=True)["success"]
    chain.checkpoints[0]["prefix_hash"] = "0x" + "ab" * 32
    with pytest.raises(ValueError, match="checkpoint"):
        acc.recover(sess.session_id, "party")
    chain.checkpoints.pop(0)
    sess.end()


def test_final_retry_reconciles_a_mined_transaction_whose_receipt_was_lost():
    class UncertainChain(CheckpointChain):
        final_hash = "0x" + "00" * 32
        def get_session(self, sid):
            return ("provider", "party", bytes.fromhex(self.final_hash[2:]), 1,
                    2 if self.commits else 0, True)
        def commit_trace(self, provider, sid, h):
            if self.commits:
                raise RuntimeError("trace already committed")
            super().commit_trace(provider, sid, h)
            self.final_hash = h
            raise ConnectionError("receipt lost after mining")
    store, chain = FakeStore(), UncertainChain()
    acc = Accountability("p", store=store, chain=chain, provider_addr="provider")
    sess = acc.session("party")
    sess.record("read", {}, "done")
    with pytest.raises(ConnectionError):
        sess.end()
    with pytest.raises(RuntimeError, match="closing"):
        sess.record("write", {}, "cannot extend an uncertain final commitment")
    effects = []
    with pytest.raises(RuntimeError, match="closing"):
        sess.guard("write", {}, lambda: effects.append("unexpected"))
    assert effects == []
    assert sess.checkpoint_status()["final_submission_pending"] is True
    assert sess.end()["n_actions"] == 1
    assert len(chain.commits) == 1
    assert sess.checkpoint_status()["final_submission_pending"] is False


def test_finalization_refuses_inflight_executor_then_records_its_outcome():
    _, sess, _, _ = setup(count=100)
    entered, release = threading.Event(), threading.Event()
    def executor():
        entered.set()
        assert release.wait(1)
        return "done"
    thread = threading.Thread(target=lambda: sess.guard("write", {}, executor))
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(RuntimeError, match="executor"):
            sess.end()
    finally:
        release.set()
        thread.join(1)
    assert sess.end()["n_actions"] == 1


def test_automatic_retry_anchors_after_outage_without_new_actions():
    _, sess, _, chain = setup(count=1, seconds=0.03)
    chain.fail = True
    sess.record("write", {}, "done")
    assert chain.called.wait(1)
    chain.called.clear()
    chain.fail = False
    assert chain.called.wait(1)
    sess.end()
    assert chain.checkpoints[0]["record_count"] == 1


def test_checkpoint_does_not_anchor_unacknowledged_store_suffix():
    _, sess, store, chain = setup(count=100)
    sess.record("read", {}, "done")
    store.shipped.append((sess.session_id, ActionRecord(2, sess.session_id, "injected", {}, "ok", 0).to_dict()))
    assert sess.checkpoint(force=True)["success"]
    assert chain.checkpoints[0]["record_count"] == 1
    assert chain.checkpoints[0]["prefix_hash"] == trace_hash(sess.records)
    with pytest.raises(ValueError, match="session boundary"):
        sess.end()
    store.shipped.pop()
    sess.end()


def test_offchain_finalization_does_not_claim_anchored_records():
    sess = Accountability("offline").session("party")
    sess.record("read", {}, "done")
    sess.end()
    status = sess.checkpoint_status()
    assert status["anchored_record_count"] == 0 and status["unanchored_records"] == 1
    assert status["finalized"] is True


def test_precommit_store_failure_still_allows_more_observations_before_retry():
    _, sess, store, chain = setup(count=100)
    sess.record("read", {}, "one")
    store.fail_reads = True
    from aa_sdk import StoreUnavailable
    with pytest.raises(StoreUnavailable):
        sess.end()
    sess.record("read", {}, "two")
    assert not sess.checkpoint_status()["final_submission_pending"]
    store.fail_reads = False
    assert sess.end()["n_actions"] == 2
    assert len(chain.commits) == 1
