"""Loopback HTTP transport for native-session keyed OpenClaw observations.

POST /session binds a user before dispatch. POST /observation forwards native starts,
approvals and terminal outcomes. POST /end refuses unresolved invocations or unshipped
records. Python alone encodes and hashes evidence; the TypeScript plugin retries the
same event IDs. AA_NO_STORE=1 selects in-memory tests; AA_CHAIN=1 enables escrow.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # repo root
sys.path.insert(0, os.path.dirname(HERE))   # integrations/openclaw -> import aa_openclaw
sys.path.insert(0, os.path.join(PROTO, "scripts"))   # _config (chain actors), only used with AA_CHAIN=1

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import aa_openclaw
from aa_sdk import StoreUnavailable

app = FastAPI(title="aa-openclaw-helper")


class StartReq(BaseModel):
    party: str
    native_session_id: str


class EndReq(BaseModel):
    native_session_id: str


class ObservationReq(BaseModel):
    native_session_id: str
    event_id: str
    action_id: str
    phase: str
    tool: str
    args: dict
    request_id: str | None = None
    decision: str | None = None
    scope: str | None = None
    authority: str | None = None
    policy_id: str | None = None
    origin: object | None = None
    result: object | None = None


def _store():
    if os.environ.get("AA_NO_STORE") == "1":
        return None
    from aa_sdk import HttpStore
    return HttpStore(os.environ.get("STORE_URL", "http://127.0.0.1:8000"))


def _chain():
    if os.environ.get("AA_CHAIN") != "1":
        return None, None
    import _config as C
    w3 = C.w3()
    ws = C.actors(w3)
    return C.escrow_client(w3, ws), ws["provider"][0]


@app.post("/session")
def start(req: StartReq):
    if not req.party.strip() or not req.native_session_id.strip():
        raise HTTPException(422, "party and native session identity are required")
    reused = aa_openclaw.current(req.native_session_id)[1] is not None
    cap = os.environ.get("AA_AGG_MAX_COUNT")
    escrow, provider = _chain()
    if escrow is not None:
        import _config as C
        default_payout = C.PAYOUT
    else:
        default_payout = 10**16
    try:
        payout = int(os.environ.get("AA_PAYOUT_WEI", str(default_payout)))
        if payout <= 0:
            raise ValueError("AA_PAYOUT_WEI must be positive")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    consent_tools = [tool.strip() for tool in os.environ.get("AA_CONSENT_TOOLS", "exec").split(",") if tool.strip()]
    try:
        _, sess = aa_openclaw.begin_session(party=req.party, native_session_id=req.native_session_id,
            store=_store(), chain=escrow, provider_addr=provider, payout_wei=payout, consent_tools=consent_tools,
            aggregate_max_count=int(cap) if cap else None)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"session_id":sess.session_id, "receipt":sess.receipt, "reused":reused}


@app.post("/observation")
def observation(req: ObservationReq):
    if not all([req.native_session_id, req.action_id, req.event_id]):
        raise HTTPException(422, "native session, action and event identities are required")
    if req.phase.startswith("approval_") and not req.request_id:
        raise HTTPException(422, "approval requires native request identity")
    if req.phase == "approval_resolved" and (not req.scope or not req.authority):
        raise HTTPException(422, "resolution requires explicit native scope and authority")
    try:
        return aa_openclaw.observe(req.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/end")
def end(req: EndReq):
    try:
        summary = aa_openclaw.end_session(req.native_session_id)
    except StoreUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if summary is None:
        raise HTTPException(409, "no active session")
    return summary


@app.get("/verdicts")
def verdicts(native_session_id: str):
    acc, sess = aa_openclaw.current(native_session_id)
    if sess is None:
        raise HTTPException(409, "no active session")
    return {str(pid): {"violated":v.violated, "seq":v.seq, "reason":v.reason}
            for pid, v in acc.self_check(sess.records).items()}


@app.get("/health")
def health():
    return {"ok":True}
