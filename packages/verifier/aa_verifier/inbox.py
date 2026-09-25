"""Authenticated, claim-specific evidence receiver with an immutable durable inbox.

No route reads or grants access to the provider's store. A bundle is authenticated before
it can occupy a claim's inbox slot; its evidence content is checked only during adjudication.
Each claim ends with exactly one inbox outcome: the first accepted submission, or a record that
the deadline passed without one. One database transaction decides between them, so a delivery
at the deadline cannot race the verifier's missing-evidence verdict.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from web3 import Web3

from aa_commons import canonical_bytes
from aa_sdk.evidence import EVIDENCE_WINDOW, PAYLOAD_KEYS, VERSION, ZERO32, evidence_hash, recover_provider


class EvidenceConflict(ValueError):
    pass


class ClaimClosedWithoutEvidence(ValueError):
    pass


class EvidenceInbox:
    def __init__(self, path, chain_id, escrow_address):
        self.path = Path(path)
        self.chain_id = int(chain_id)
        self.escrow_address = Web3.to_checksum_address(escrow_address).lower()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS deployment (identity TEXT PRIMARY KEY)')
            identity = f'{self.chain_id}:{self.escrow_address}'
            db.execute('INSERT OR IGNORE INTO deployment VALUES (?)', (identity,))
            if db.execute('SELECT identity FROM deployment').fetchall() != [(identity,)]:
                raise ValueError('evidence inbox belongs to another deployment')
            db.execute('''CREATE TABLE IF NOT EXISTS evidence (
                challenge_id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                signature TEXT NOT NULL, provider TEXT NOT NULL,
                evidence_hash TEXT NOT NULL, received_at INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS closed_without_evidence (
                challenge_id INTEGER PRIMARY KEY, closed_at INTEGER NOT NULL)''')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def _exclusive(self, decide):
        """Run ``decide(db)`` in one write transaction that holds the database lock throughout."""
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                result = decide(db)
            except BaseException:
                db.execute('ROLLBACK')
                raise
            db.execute('COMMIT')
            return result
        finally:
            db.close()

    def get(self, challenge_id):
        with self._db() as db:
            row = db.execute('SELECT payload, signature, provider, evidence_hash, received_at '
                             'FROM evidence WHERE challenge_id=?', (int(challenge_id),)).fetchone()
        if row is None:
            return None
        payload, signature, provider, digest, received_at = row
        return {'payload': json.loads(payload), 'signature': signature, 'provider': provider,
                'evidence_hash': digest, 'received_at': received_at, 'challenge_id': int(challenge_id)}

    def save(self, challenge_id, envelope, provider, received_at):
        """Internal receiver operation; commit before returning any acknowledgment."""
        cid = int(challenge_id)
        payload = canonical_bytes(envelope['payload']).decode('utf-8')
        digest = evidence_hash(envelope['payload'])

        def decide(db):
            if db.execute('SELECT 1 FROM closed_without_evidence WHERE challenge_id=?', (cid,)).fetchone():
                raise ClaimClosedWithoutEvidence('the deadline passed without evidence for this claim')
            db.execute('INSERT OR IGNORE INTO evidence VALUES (?, ?, ?, ?, ?, ?)',
                       (cid, payload, envelope['signature'], provider, digest, int(received_at)))
            return db.execute('SELECT evidence_hash, received_at FROM evidence WHERE challenge_id=?',
                              (cid,)).fetchone()

        row = self._exclusive(decide)
        if row[0] != digest:
            raise EvidenceConflict('a different evidence submission already exists for this claim')
        return {'challenge_id': cid, 'evidence_hash': row[0], 'received_at': row[1]}

    def close_without_evidence(self, challenge_id, closed_at):
        """Record that the deadline passed with no submission. False if evidence was saved first."""
        cid = int(challenge_id)

        def decide(db):
            if db.execute('SELECT 1 FROM evidence WHERE challenge_id=?', (cid,)).fetchone():
                return False
            db.execute('INSERT OR IGNORE INTO closed_without_evidence VALUES (?, ?)', (cid, int(closed_at)))
            return True

        return self._exclusive(decide)


def create_app(escrow, inbox: EvidenceInbox, *, max_body_bytes=64 * 1024 * 1024):
    if int(escrow.w3.eth.chain_id) != inbox.chain_id or escrow.contract.address.lower() != inbox.escrow_address:
        raise ValueError('receiver and inbox deployment differ')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/claims/{challenge_id}/evidence')
    async def receive(challenge_id: int, request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_body_bytes:
                raise HTTPException(413, 'evidence exceeds receiver size limit')
        try:
            envelope = json.loads(body)
            payload = envelope['payload']
            if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
                raise ValueError('payload must be an object with exactly the v2 fields')
            if (type(payload['version']) is not int or payload['version'] != VERSION
                    or type(payload['chain_id']) is not int or payload['chain_id'] != inbox.chain_id
                    or payload['escrow_address'].lower() != inbox.escrow_address
                    or type(payload['challenge_id']) is not int or payload['challenge_id'] != challenge_id
                    or type(payload['promise_id']) is not int
                    or not isinstance(payload['session_id'], str)):
                raise ValueError('evidence domain or claim binding is wrong')
            provider = recover_provider(envelope)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            raise HTTPException(400, 'invalid signed evidence envelope') from error
        # RPC and DB failures produce an error response; they never become a provider verdict.
        claim = escrow.get_challenge(challenge_id)
        session_id, promise_id = Web3.to_hex(claim[0]), int(claim[1])
        if payload['session_id'] != session_id or payload['promise_id'] != promise_id:
            raise HTTPException(400, 'evidence does not bind the challenged session and promise')
        expected_provider = escrow.get_promise(promise_id)[0]
        if provider.lower() != expected_provider.lower():
            raise HTTPException(403, 'submission was not signed by the challenged provider')
        existing = inbox.get(challenge_id)
        if existing is not None:
            if existing['evidence_hash'] != evidence_hash(payload):
                raise HTTPException(409, 'a different evidence submission already exists for this claim')
            return {key: existing[key] for key in ('challenge_id', 'evidence_hash', 'received_at')}
        if int(claim[4]) == 0:
            raise HTTPException(409, 'claim not visible yet')
        if int(claim[4]) != 1:
            raise HTTPException(409, 'claim is not open')
        now = int(escrow.w3.eth.get_block('latest')['timestamp'])
        if now > int(claim[5]) + EVIDENCE_WINDOW:
            raise HTTPException(409, 'evidence deadline passed')
        # Evidence must match the final commitment, so it cannot be accepted before it exists.
        session = escrow.get_session(session_id)
        if bytes(session[2]) == ZERO32:
            raise HTTPException(409, 'session has no final trace commitment yet')
        try:
            return inbox.save(challenge_id, envelope, provider, now)
        except (EvidenceConflict, ClaimClosedWithoutEvidence) as error:
            raise HTTPException(409, str(error)) from error

    return app
