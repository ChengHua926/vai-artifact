"""Provider listener: answer accepted challenges from its private trace store.

Set STORE_TOKEN and EVIDENCE_URL for this process. Only the provider signing key is loaded.
The answer to a claim is signed evidence delivered to the verifier; nothing is sent on chain.
A claim stays pending until the verifier returns a durable receipt, the claim closes, or its
evidence deadline passes. Claims on sessions without a final commitment wait for it.
"""
import json
import logging
from pathlib import Path
import time

import _config as C
from aa_sdk import HttpStore
from aa_sdk.evidence import EvidenceClient, EvidenceNotReady, EvidenceRefused, UnlockedSigner

log = logging.getLogger('provider')
POLL_SECONDS = 6


def _load_state(path, default):
    try:
        data = json.loads(Path(path).read_text())
        return int(data['block']), {int(cid) for cid in data['pending']}
    except FileNotFoundError:
        return default, set()


def _save_state(path, block, pending):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'block': block, 'pending': sorted(pending)})+'\n')
    temporary.replace(path)


def poll_once(escrow, store, client, provider_addr, signer, last, pending, *, confirmations=0):
    latest = int(escrow.w3.eth.block_number)
    start, end = last + 1, min(latest - confirmations, last + 2000)
    work = set(pending)
    if start <= end:
        work.update(int(ev['args']['challengeId']) for ev in
            escrow.contract.events.Challenged().get_logs(from_block=start, to_block=end))
    remaining = set()
    for cid in sorted(work):
        try:
            claim = escrow.get_challenge(cid)
            if escrow.get_promise(int(claim[1]))[0].lower() != provider_addr.lower():
                continue
            receipt = client.deliver(escrow, provider_addr, signer, store, cid)
            log.info('delivered evidence for claim %s (receipt %s)', cid, receipt['evidence_hash'])
        except EvidenceRefused as error:
            log.warning('claim %s no longer accepts evidence: %s', cid, error)
        except EvidenceNotReady:
            remaining.add(cid)
        except Exception as error:
            remaining.add(cid)
            log.warning('claim %s delivery failed; will retry: %s', cid, str(error)[:160])
    return max(last, end), remaining


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    w3 = C.w3()
    deployment = C.load_deployment()
    actors = C.provider_actors(w3, deployment)
    escrow = C.escrow_client(w3, actors)
    provider_addr, account = actors['provider']
    signer = account or UnlockedSigner(w3, provider_addr)
    store, client = HttpStore(C.STORE_URL), EvidenceClient(C.EVIDENCE_URL)
    state_path = C.provider_cursor_path(deployment)
    last, pending = _load_state(state_path, deployment['deploy_block'] - 1)
    while True:
        try:
            next_block, pending = poll_once(escrow, store, client, provider_addr, signer, last,
                pending, confirmations=0 if C.LOCAL else 2)
            _save_state(state_path, next_block, pending)
            last = next_block
        except Exception as error:
            log.warning('provider infrastructure unavailable; retrying: %s', str(error)[:160])
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
