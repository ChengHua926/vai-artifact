"""Store service: append-only record custody — the streamed records ARE the trace (there is no
finalize copy to diverge from) — plus the promise endpoints and health."""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as store_app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store_app, "DB", str(tmp_path / "test_store.db"))
    return TestClient(store_app.app)


def _rec(seq, tool="read", args=None, result="ok", sid="0xS"):
    return {"seq": seq, "session_id": sid, "tool": tool,
            "args": args if args is not None else {}, "result": result, "ts": 1000 + seq}


def test_append_and_ordered_get(client):
    # out-of-order arrival is fine; GET returns seq order
    assert client.post("/sessions/0xS/records", json=_rec(2)).status_code == 200
    assert client.post("/sessions/0xS/records", json=_rec(1)).status_code == 200
    got = client.get("/sessions/0xS/records").json()
    assert [r["seq"] for r in got] == [1, 2]
    assert got[0] == _rec(1)


def test_append_is_first_write_wins(client):
    client.post("/sessions/0xS/records", json=_rec(1, tool="delete"))
    client.post("/sessions/0xS/records", json=_rec(1, tool="delete"))          # idempotent retry
    client.post("/sessions/0xS/records", json=_rec(1, tool="read"))            # rewrite attempt
    got = client.get("/sessions/0xS/records").json()
    assert len(got) == 1
    assert got[0]["tool"] == "delete"                                          # original stands


def test_records_are_per_session(client):
    client.post("/sessions/0xA/records", json=_rec(1, sid="0xA"))
    client.post("/sessions/0xB/records", json=_rec(1, sid="0xB"))
    assert len(client.get("/sessions/0xA/records").json()) == 1


def test_unknown_session_is_an_empty_list(client):
    # "nothing shipped" vs "no actions" is the on-chain hash's call, not the store's
    assert client.get("/sessions/0xNOPE/records").json() == []


def test_malformed_record_is_rejected(client):
    assert client.post("/sessions/0xS/records", json={"seq": 1, "tool": "x"}).status_code == 422
    assert client.get("/sessions/0xS/records").json() == []


def test_there_is_no_second_copy_of_the_trace(client):
    """The finalize row (PUT/GET /sessions/{id}) is gone: the records are the only copy."""
    client.post("/sessions/0xS/records", json=_rec(1))
    r = client.put("/sessions/0xS", json={"session_id": "0xS", "provider_id": "p", "party": "0xU",
                                          "trace_hash": "0x" + "11" * 32, "trace": "[]"})
    assert r.status_code in (404, 405)
    assert client.get("/sessions/0xS").status_code in (404, 405)
    assert [x["seq"] for x in client.get("/sessions/0xS/records").json()] == [1]


def test_promises_roundtrip_and_health(client):
    client.put("/promises/1", json={"predicate": "egress_within_allowlist", "params": {"a": 1}})
    assert client.get("/promises/1").json() == {"predicate": "egress_within_allowlist", "params": {"a": 1}}
    assert client.get("/promises/9").status_code == 404
    assert client.get("/health").json() == {"ok": True}


def test_optional_metadata_is_preserved_without_changing_legacy_records(client):
    old = _rec(1, result=None)
    new = {**_rec(2), "metadata": {"action_id": "invocation", "event_id": "completion"}}
    client.post("/sessions/0xS/records", json=old)
    client.post("/sessions/0xS/records", json=new)
    assert client.get("/sessions/0xS/records").json() == [old, new]


def test_inventory_detects_either_kind_of_existing_data(client):
    assert client.get("/inventory").json() == {"promise_count": 0, "record_count": 0}
    client.post("/sessions/0xS/records", json=_rec(1))
    assert client.get("/inventory").json() == {"promise_count": 0, "record_count": 1}
    client.put("/promises/1", json={"predicate": "x", "params": {}})
    assert client.get("/inventory").json() == {"promise_count": 1, "record_count": 1}
