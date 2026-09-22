"""Streaming custody: each record leaves the process at action time, is buffered on store
failure, and the store's copy is what gets committed. The tamper test is the METR scenario in
miniature: edit the in-process buffer after shipping and the committed hash is still the hash
of the store's records. A store that never acknowledged a record means no commit at all.
"""
from __future__ import annotations

import logging

import pytest

from aa_commons import ActionRecord, trace_hash
from aa_sdk import Accountability, StoreUnavailable


class FakeStore:
    """In-memory stand-in for HttpStore; `fail` simulates an outage on writes, `fail_reads` on
    the read-back."""

    def __init__(self):
        self.shipped: list[tuple[str, dict]] = []   # (session_id, record) in arrival order
        self.fail = False
        self.fail_reads = False

    def append_record(self, session_id, record):
        if self.fail:
            raise ConnectionError("store unreachable")
        self.shipped.append((session_id, dict(record)))

    def get_records(self, session_id):
        if self.fail or self.fail_reads:
            raise ConnectionError("store unreachable")
        return [r for sid, r in self.shipped if sid == session_id]

    def put_promise(self, promise_id, record):
        pass

    def trace_hash_of(self, session_id) -> str:
        """What the store holds for the session, hashed the way the verifier will hash it."""
        return trace_hash([ActionRecord.from_dict(r) for sid, r in self.shipped if sid == session_id])


class StubChain:
    """Just enough chain for session()/end()/recover(): records open_session + commit_trace calls."""

    def __init__(self):
        self.opened = []
        self.commits = []
        self.contract = type("Contract", (), {"address": "0xESCROW"})()

    def open_session(self, provider_addr, session_id, party):
        self.opened.append((provider_addr, session_id, party))
        return type("Receipt", (), {"transactionHash": b"\x00" * 32})()

    def commit_trace(self, provider_addr, session_id, h):
        self.commits.append((provider_addr, session_id, h))


def _acc(store, chain=None):
    return Accountability("test-provider", store=store, chain=chain, provider_addr="0xPROV")


def test_guard_ships_each_record_at_action_time():
    store = FakeStore()
    sess = _acc(store).session(party="0xUSER")
    sess.guard("read", {"path": "a.txt"}, lambda: "contents")
    assert len(store.shipped) == 1                       # shipped before the next action ran
    sess.guard("write", {"path": "b.txt"}, lambda: "ok")
    assert [r["seq"] for _, r in store.shipped] == [1, 2]
    assert all(sid == sess.session_id for sid, _ in store.shipped)
    for rec, shipped in zip(sess.records, [r for _, r in store.shipped]):
        assert shipped == rec.to_dict()


def test_record_grant_revoke_and_violating_actions_ship_too():
    store = FakeStore()
    acc = _acc(store)
    acc.register_promise("egress_within_allowlist",
                         {"egress_tools": ["send"], "recipient_allowlist": ["alice"]},
                         payout_wei=1)
    sess = acc.session(party="0xUSER")
    sess.record("remote_tool", {"x": 1}, result="done")
    sess.grant("recipient_grant", {"target": "bob"})
    sess.revoke("recipient_revoke", {"target": "bob"})
    assert sess.guard("send", {"target": "eve"}, lambda: "sent") == "sent"
    assert [r["seq"] for _, r in store.shipped] == [1, 2, 3, 4]
    assert store.shipped[-1][1]["result"] == "sent"
    assert next(iter(acc.self_check(sess.records).values())).violated


def test_no_store_hashes_the_buffer():
    """Offline self-check mode: no store, so the buffer is the only copy and its hash is what
    gets committed — no custody claim holds here."""
    chain = StubChain()
    sess = _acc(None, chain).session(party="0xUSER")
    sess.guard("read", {"path": "a"}, lambda: "x")
    summary = sess.end()
    assert summary["n_actions"] == 1
    assert summary["trace_hash"] == trace_hash(sess.records)
    assert chain.commits == [("0xPROV", sess.session_id, trace_hash(sess.records))]


def test_store_outage_buffers_and_flushes_later():
    store = FakeStore()
    sess = _acc(store).session(party="0xUSER")
    sess.guard("a", {}, lambda: 1)
    store.fail = True
    sess.guard("b", {}, lambda: 2)                       # outage: action still executes
    assert [r["seq"] for _, r in store.shipped] == [1]
    assert sess.unshipped_seqs() == [2]
    store.fail = False
    sess.guard("c", {}, lambda: 3)                       # next action flushes the backlog
    assert [r["seq"] for _, r in store.shipped] == [1, 2, 3]

    store.fail = True
    sess.guard("d", {}, lambda: 4)
    store.fail = False
    sess.end()                                           # end() flushes the remainder, then commits
    assert [r["seq"] for _, r in store.shipped] == [1, 2, 3, 4]


def test_end_commits_the_hash_of_the_stores_records():
    store, chain = FakeStore(), StubChain()
    sess = _acc(store, chain).session(party="0xUSER")
    sess.guard("read", {"path": "a"}, lambda: "x")
    sess.guard("send", {"target": "alice"}, lambda: "ok")
    summary = sess.end()
    committed = store.trace_hash_of(sess.session_id)
    assert chain.commits == [("0xPROV", sess.session_id, committed)]
    assert summary == {"session_id": sess.session_id, "trace_hash": committed, "n_actions": 2}
    assert committed == trace_hash(sess.records)         # untouched buffer: the same bytes


def test_buffer_tamper_after_shipping_commits_the_store_copy(caplog):
    """METR in miniature: the agent edits the in-process buffer after the record left. The
    committed hash is the hash of the store's records, not the buffer's, and the differing
    seq is logged."""
    store, chain = FakeStore(), StubChain()
    sess = _acc(store, chain).session(party="0xUSER")
    sess.guard("delete", {"path": "/etc/passwd"}, lambda: "deleted")
    sess.guard("read", {"path": "notes.txt"}, lambda: "x")
    original = store.shipped[0][1]
    sess.records[0] = ActionRecord(1, sess.session_id, "read",
                                   {"path": "innocent.txt"}, result="x", ts=original["ts"])
    with caplog.at_level(logging.WARNING, logger="aa_sdk"):
        summary = sess.end()
    committed = store.trace_hash_of(sess.session_id)
    assert summary["trace_hash"] == committed
    assert chain.commits == [("0xPROV", sess.session_id, committed)]
    assert committed != trace_hash(sess.records)         # the edited buffer is NOT what was committed
    assert store.shipped[0][1] == original               # streamed copy untouched by end()
    assert any("seqs [1]" in m for m in caplog.messages)


def test_buffer_truncation_after_shipping_is_also_overridden():
    store, chain = FakeStore(), StubChain()
    sess = _acc(store, chain).session(party="0xUSER")
    sess.guard("delete", {"path": "/etc/passwd"}, lambda: "deleted")
    sess.guard("read", {"path": "a"}, lambda: "x")
    del sess.records[0]                                  # the agent drops the incriminating record
    summary = sess.end()
    assert summary["n_actions"] == 2                     # the store still has it, so it is committed
    assert chain.commits == [("0xPROV", sess.session_id, store.trace_hash_of(sess.session_id))]


def test_store_down_at_end_raises_and_commits_nothing():
    store, chain = FakeStore(), StubChain()
    sess = _acc(store, chain).session(party="0xUSER")
    sess.guard("a", {}, lambda: 1)
    store.fail = True
    sess.guard("b", {}, lambda: 2)
    with pytest.raises(StoreUnavailable) as ei:
        sess.end()
    assert ei.value.unshipped == [2] and ei.value.session_id == sess.session_id
    assert chain.commits == []                           # no commit without the store's copy
    store.fail = False
    summary = sess.end()                                 # the provider retries once the store is back
    assert summary["n_actions"] == 2
    assert chain.commits == [("0xPROV", sess.session_id, store.trace_hash_of(sess.session_id))]


def test_store_unreadable_at_end_raises_and_commits_nothing():
    store, chain = FakeStore(), StubChain()
    sess = _acc(store, chain).session(party="0xUSER")
    sess.guard("a", {}, lambda: 1)
    store.fail_reads = True                              # shipped fine, but the read-back fails
    with pytest.raises(StoreUnavailable) as ei:
        sess.end()
    assert ei.value.unshipped == []
    assert chain.commits == []
    store.fail_reads = False
    assert sess.end()["n_actions"] == 1


def test_recover_finalizes_from_shipped_records():
    store = FakeStore()
    sess = _acc(store).session(party="0xUSER")
    sess.guard("a", {}, lambda: 1)
    sess.guard("b", {}, lambda: 2)
    sid = sess.session_id
    del sess                                             # crash: end() never runs

    chain = StubChain()
    acc2 = Accountability("test-provider", store=store, chain=chain, provider_addr="0xPROV")
    summary = acc2.recover(sid, party="0xUSER")
    assert summary["n_actions"] == 2
    assert summary["trace_hash"] == store.trace_hash_of(sid)
    assert chain.commits == [("0xPROV", sid, store.trace_hash_of(sid))]


def test_recover_without_records_raises():
    store = FakeStore()
    acc = Accountability("test-provider", store=store)
    with pytest.raises(RuntimeError):
        acc.recover("0x" + "ab" * 32, party="0xUSER")
