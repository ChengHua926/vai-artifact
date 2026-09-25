"""Local demonstration helper: exercise the same signed HTTP handoff as the services.

The provider delivers the claim's evidence to a temporary verifier receiver (no transaction),
then the verifier settles the claim from its inbox.
"""
from pathlib import Path
import socket
import tempfile
import threading
import time

import uvicorn
from aa_sdk.evidence import EvidenceClient, UnlockedSigner
from aa_verifier import process_challenge
from aa_verifier.inbox import EvidenceInbox, create_app


def adjudicate_local(escrow, store, challenge_id, provider_addr, signer, verifier_addr):
    if int(escrow.w3.eth.chain_id) != 31337:
        raise ValueError('local demonstration receiver is restricted to Anvil')
    with tempfile.TemporaryDirectory(prefix='vai-evidence-demo-') as directory:
        inbox = EvidenceInbox(Path(directory)/'inbox.sqlite3', 31337, escrow.contract.address)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(escrow, inbox), log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            if not server.started:
                raise RuntimeError('local evidence receiver failed to start')
            EvidenceClient(f'http://127.0.0.1:{port}').deliver(
                escrow, provider_addr, signer or UnlockedSigner(escrow.w3, provider_addr), store, challenge_id)
            return process_challenge(escrow, inbox, challenge_id, verifier_addr)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
