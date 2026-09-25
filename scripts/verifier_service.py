"""Receive provider evidence and settle accepted challenges without store access.

Run behind an HTTPS reverse proxy for remote providers; the receiver binds loopback by default.
The verifier holds only its own signing key and durable evidence inbox. Every accepted claim
stays pending until it is settled: pending claims are revisited every poll, including when no
new blocks arrived, so a claim without evidence is ruled a violation once its deadline passes.
"""
import json
import logging
import os
from pathlib import Path
import threading
import time

import uvicorn

import _config as C
from aa_verifier import process_challenge
from aa_verifier.inbox import EvidenceInbox, create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s")
log = logging.getLogger('verifier')
POLL_SECONDS = 6
CONFIRMATIONS = 0 if C.LOCAL else 2
CHECKPOINT = None


def _load_state(default):
    try:
        state = json.loads(Path(CHECKPOINT).read_text())
    except FileNotFoundError:
        return default, set()
    return int(state['block']), {int(cid) for cid in state['retry_challenges']}


def _save_state(block, retry):
    path = Path(CHECKPOINT)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'block': block, 'retry_challenges': sorted(retry)}) + '\n')
    temporary.replace(path)


def poll_once(escrow, inbox, verifier_addr, last, pending, *, confirmations=CONFIRMATIONS):
    """Scan newly accepted claims, then service all unresolved claims once."""
    latest = int(escrow.w3.eth.block_number)
    start = last + 1
    end = min(latest - confirmations, start + 1999)
    work = set(pending)
    if start <= end:
        work.update(int(ev['args']['challengeId']) for ev in
                    escrow.contract.events.Challenged().get_logs(from_block=start, to_block=end))
    remaining = set()
    for cid in sorted(work):
        try:
            result = process_challenge(escrow, inbox, cid, verifier_addr)
            if result['violated'] is None and result.get('pending', True):
                remaining.add(cid)
            elif result['violated'] is not None:
                log.info('claim %s violated=%s paid=%s wei (%s)', cid, result['violated'],
                         result['paid_to_challenger'], result['reason'])
        except Exception as error:
            # Infrastructure failure, a depleted reserve, or a claim closed by a racing withdrawal
            # all leave the claim pending; the next poll drops it once it is no longer open.
            # Saving this set before advancing the cursor makes process restarts safe.
            remaining.add(cid)
            log.warning('claim %s remains pending: %s', cid, str(error)[:160])
    return max(last, end), remaining


def main():
    global CHECKPOINT
    w3 = C.w3()
    deployment = C.load_deployment()
    actors = C.verifier_actors(w3, deployment)
    escrow = C.escrow_client(w3, actors)
    verifier_addr = actors['verifier'][0]
    inbox = EvidenceInbox(C.evidence_inbox_path(deployment), C.CHAIN_ID, escrow.contract.address)
    CHECKPOINT = C.verifier_cursor_path(deployment)
    last, pending = _load_state(deployment['deploy_block'] - 1)
    server = uvicorn.Server(uvicorn.Config(create_app(escrow, inbox),
        host='127.0.0.1', port=int(os.environ.get('EVIDENCE_PORT', '8001')), log_level='warning'))
    receiver = threading.Thread(target=server.run, name='evidence-receiver', daemon=True)
    receiver.start()
    log.info('verifier %s watching %s; evidence receiver on loopback port %s',
             verifier_addr, escrow.contract.address, server.config.port)
    try:
        while receiver.is_alive():
            try:
                next_block, pending = poll_once(escrow, inbox, verifier_addr, last, pending)
                _save_state(next_block, pending)
                last = next_block
            except Exception as error:
                log.warning('verifier infrastructure unavailable; retrying: %s', str(error)[:160])
            time.sleep(POLL_SECONDS)
        raise RuntimeError('evidence receiving service stopped')
    finally:
        server.should_exit = True
        receiver.join(timeout=5)


if __name__ == '__main__':
    main()
