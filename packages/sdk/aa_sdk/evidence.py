"""Provider-controlled, signed evidence delivery for one accepted challenge.

The verifier receives this bundle, never credentials for the provider's trace store.
A successful HTTP receipt means the bytes have been durably saved, not that they are valid.
Delivery is the provider's entire response to a claim; nothing is sent on chain. The
deadline is the claim's on-chain filing time plus EVIDENCE_WINDOW, so the provider and the
verifier compute it from the same public data. The verifier enforces it.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from aa_commons import canonical_bytes, keccak_hex

DOMAIN = b"VAI claim evidence v2\n"
VERSION = 2
PAYLOAD_KEYS = frozenset({"version", "chain_id", "escrow_address", "challenge_id", "session_id",
                          "promise_id", "records", "params"})
EVIDENCE_WINDOW = 3 * 24 * 60 * 60   # seconds after filing
ZERO32 = b"\x00" * 32


class EvidenceNotReady(RuntimeError):
    """The session has no final trace commitment yet; deliver after finalization."""


class EvidenceRefused(ValueError):
    """The claim can no longer receive evidence (closed, or past its deadline)."""


def evidence_message(payload):
    return encode_defunct(primitive=DOMAIN + canonical_bytes(payload))


def evidence_hash(payload):
    return keccak_hex(DOMAIN + canonical_bytes(payload))


def sign_payload(payload, signer):
    """Sign arbitrary JSON evidence, including malformed evidence for protocol tests."""
    signature = signer.sign_message(evidence_message(payload)).signature
    return {"payload": payload, "signature": Web3.to_hex(signature)}


def recover_provider(envelope):
    return Account.recover_message(evidence_message(envelope["payload"]),
                                   signature=envelope["signature"])


def evidence_deadline(escrow, challenge_id):
    """Last chain timestamp at which the verifier accepts evidence for this claim."""
    return int(escrow.get_challenge(challenge_id)[5]) + EVIDENCE_WINDOW


class UnlockedSigner:
    """Development-only EIP-191 signer for an unlocked local Ethereum account."""
    def __init__(self, w3, address):
        self.w3, self.address = w3, address
        if int(w3.eth.chain_id) != 31337:
            raise ValueError("unlocked evidence signing is restricted to the local development chain")

    def sign_message(self, message):
        from types import SimpleNamespace
        # eth_sign applies the EIP-191 prefix; encode_defunct.body is the unsigned message.
        return SimpleNamespace(signature=self.w3.eth.sign(self.address, data=message.body))


def create_envelope(escrow, challenge_id, records, params, signer):
    challenge = escrow.get_challenge(challenge_id)
    payload = {"version": VERSION, "chain_id": int(escrow.w3.eth.chain_id),
        "escrow_address": escrow.contract.address.lower(), "challenge_id": int(challenge_id),
        "session_id": Web3.to_hex(challenge[0]), "promise_id": int(challenge[1]),
        "records": records, "params": params}
    return sign_payload(payload, signer)


def require_secure_url(base_url, purpose="evidence delivery"):
    url = urlparse(base_url)
    try:
        loopback = ipaddress.ip_address(url.hostname or "").is_loopback
    except ValueError:
        loopback = url.hostname == "localhost"
    if (url.scheme != "https" and not (url.scheme == "http" and loopback)) or url.username or url.password:
        raise ValueError(f"{purpose} requires HTTPS; HTTP is allowed only on loopback")
    if not url.hostname or url.query or url.fragment:
        raise ValueError(f"invalid {purpose} URL")


class EvidenceClient:
    def __init__(self, base_url, *, session=None, timeout=60):
        require_secure_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def submit(self, envelope):
        cid = envelope["payload"]["challenge_id"]
        response = self.session.post(f"{self.base_url}/claims/{cid}/evidence", json=envelope,
                                     timeout=self.timeout, follow_redirects=False) if not isinstance(self.session, requests.Session) else self.session.post(
            f"{self.base_url}/claims/{cid}/evidence", json=envelope, timeout=self.timeout, allow_redirects=False)
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise requests.HTTPError("evidence redirects are not accepted", response=response)
        receipt = response.json()
        if receipt.get("challenge_id") != cid or receipt.get("evidence_hash") != evidence_hash(envelope["payload"]):
            raise ValueError("evidence receipt does not acknowledge the submitted claim and bytes")
        if type(receipt.get("received_at")) is not int:
            raise ValueError("evidence receipt is missing its acceptance time")
        return receipt

    def deliver(self, escrow, provider_addr, signer, store, challenge_id):
        """Read private provider data for one claim and obtain a durable receipt.

        Sends no transaction. Retrying the same submission returns the original receipt.
        Raises EvidenceNotReady before the final commitment and EvidenceRefused once the
        claim is closed or its deadline has passed.
        """
        claim = escrow.get_challenge(challenge_id)
        if int(claim[4]) == 0:   # an RPC answering from behind the filing block
            raise EvidenceNotReady("claim not visible yet")
        if int(claim[4]) != 1:
            raise EvidenceRefused("claim is no longer open")
        if int(escrow.w3.eth.get_block("latest")["timestamp"]) > int(claim[5]) + EVIDENCE_WINDOW:
            raise EvidenceRefused("evidence deadline passed")
        provider = escrow.get_promise(int(claim[1]))[0]
        if Web3.to_checksum_address(provider_addr) != Web3.to_checksum_address(provider):
            raise ValueError("only the challenged provider delivers evidence")
        if Web3.to_checksum_address(signer.address) != Web3.to_checksum_address(provider):
            raise ValueError("evidence signer differs from the challenged provider")
        session = escrow.get_session(Web3.to_hex(claim[0]))
        if bytes(session[2]) == ZERO32:
            raise EvidenceNotReady("session has no final trace commitment yet")
        promise = store.get_promise(int(claim[1]))
        records = store.get_records(Web3.to_hex(claim[0]))
        return self.submit(create_envelope(escrow, challenge_id, records, promise["params"], signer))
