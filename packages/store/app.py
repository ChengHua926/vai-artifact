"""Private provider trace store (FastAPI + SQLite prototype).

Holds DATA only; never funds. The SDK ships each action record here as it happens; at session
end it reads them back and commits their hash on chain, so the per-action records held here ARE
the trace — there is no second copy. The provider retrieves records to submit claim-specific
evidence to the verifier. The verifier is not given this store's token. Records are append-only and
first-write-wins, so once a record lands neither the agent nor a later request can rewrite it
here; the on-chain hash stays the only integrity root (this store narrows how long evidence sits
inside the agent's process; it never adjudicates). Swap SQLite -> Postgres/Neon later by
changing one connection string; nothing else moves.

Set a private STORE_TOKEN for this service and the provider SDK. Every data endpoint requires
Authorization: Bearer <STORE_TOKEN>; /health is public. Use HTTPS for a remote deployment.
Run:  uvicorn app:app --port 8000        (from packages/store/)
  POST /sessions/{id}/records  <- SDK ships one ActionRecord dict per action, at action time
  GET  /sessions/{id}/records  -> seq-ordered list for provider commitment, recovery, and claims
  PUT  /promises/{id}          <- SDK ships {predicate, params} at registration
  GET  /promises/{id}          -> provider retrieves parameters for a claim response
"""
from __future__ import annotations

import json
import os
import sqlite3
import hmac
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

DB = os.environ.get("STORE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trace_store.db"))


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS promises (promise_id TEXT PRIMARY KEY, payload TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS records (session_id TEXT, seq INTEGER, payload TEXT, "
                 "PRIMARY KEY (session_id, seq))")
    return conn


app = FastAPI(title="aa-trace-store")
_bearer = HTTPBearer(auto_error=False)


def require_provider(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """Keep all provider data private, including when deployment configuration is missing."""
    token = os.environ.get("STORE_TOKEN")
    if not token or not token.strip():
        raise HTTPException(status_code=503, detail="store authentication is not configured")
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="provider authentication required",
                            headers={"WWW-Authenticate": "Bearer"})


class Record(BaseModel):
    seq: int
    session_id: str
    tool: str
    args: dict
    result: Any = None
    ts: int
    metadata: dict | None = None


@app.post("/sessions/{session_id}/records", dependencies=[Depends(require_provider)])
def append_record(session_id: str, record: Record):
    """Append-only, first-write-wins: a retry of the same seq is a no-op, and a different
    payload for an existing seq is ignored rather than applied — nothing rewrites a landed
    record through this API."""
    payload = record.model_dump()
    if not payload.get("metadata"):
        payload.pop("metadata", None)  # preserve legacy hashes, including explicit null result
    conn = _db()
    conn.execute("INSERT OR IGNORE INTO records VALUES (?, ?, ?)",
                 (session_id, record.seq, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
    conn.commit()
    conn.close()
    return {"ok": True, "session_id": session_id, "seq": record.seq}


@app.get("/sessions/{session_id}/records", dependencies=[Depends(require_provider)])
def get_records(session_id: str):
    """The session's trace, in seq order (an empty list for an unknown session — whether that
    is "nothing shipped" or "no actions" is decided by the on-chain hash, not here)."""
    conn = _db()
    rows = conn.execute("SELECT payload FROM records WHERE session_id = ? ORDER BY seq",
                        (session_id,)).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


class PromiseRecord(BaseModel):
    predicate: str   # spec_id in aa_commons's registry (a hint; the chain's predicateHash decides)
    params: dict     # the promise parameters (verifier re-hashes these to paramsHash)


@app.put("/promises/{promise_id}", dependencies=[Depends(require_provider)])
def put_promise(promise_id: str, record: PromiseRecord):
    conn = _db()
    conn.execute("INSERT OR REPLACE INTO promises VALUES (?, ?)", (promise_id, record.model_dump_json()))
    conn.commit()
    conn.close()
    return {"ok": True, "promise_id": promise_id}


@app.get("/promises/{promise_id}", dependencies=[Depends(require_provider)])
def get_promise(promise_id: str):
    conn = _db()
    row = conn.execute("SELECT payload FROM promises WHERE promise_id = ?", (promise_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="unknown promise")
    return json.loads(row[0])


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/inventory", dependencies=[Depends(require_provider)])
def inventory():
    """Counts for a deployment preflight; no trace contents are returned."""
    conn = _db()
    try:
        return {"promise_count": conn.execute("SELECT COUNT(*) FROM promises").fetchone()[0],
                "record_count": conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]}
    finally:
        conn.close()
