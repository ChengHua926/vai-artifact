"""EscrowClient — a thin web3.py wrapper over the Escrow contract.

Pure infrastructure: NO predicate logic (that stays in aa_commons, the single source of truth).
Used by the provider (via Accountability), the challenger, and the verifier — each passes the
address it acts as. Local anvil uses unlocked accounts; keyed networks sign transactions with
serialized per-account nonce allocation. IDs come from event receipts (return values aren't readable from a
receipt); every bytes32 is an already-keccak'd hex string from aa_commons, converted bytes-only.
"""
from __future__ import annotations

import json
import threading
import time

from web3 import Web3
from web3.exceptions import BlockNotFound, Web3RPCError
from web3.logs import DISCARD

_TX_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_TX_LOCKS_GUARD = threading.Lock()


def _sender_lock(w3, sender):
    # Native sessions create separate Web3 connections to the same chain. Coordinate by chain
    # ID and account, never connection identity, so timer and foreground sends cannot select
    # the same pending nonce. Independent networks reusing a chain ID serialize conservatively;
    # separate processes still need separate actor coordination.
    key = (int(w3.eth.chain_id), Web3.to_checksum_address(sender))
    with _TX_LOCKS_GUARD:
        return _TX_LOCKS.setdefault(key, threading.Lock())


def _require_success(receipt):
    if receipt.status != 1:
        raise RuntimeError(f"transaction reverted: {receipt.transactionHash.hex()}")


def _is_missing_block(error, block):
    if isinstance(error, BlockNotFound):
        return True
    if not isinstance(error, Web3RPCError) or not isinstance(error.rpc_response, dict):
        return False
    # Base's public endpoint leaves this exact JSON-RPC response unclassified in web3.py.
    detail = error.rpc_response.get("error", {})
    return (isinstance(detail, dict) and detail.get("code") == -32001
            and detail.get("message") == f"block not found: {hex(block)}")


def _b32(hexstr: str) -> bytes:
    assert isinstance(hexstr, str) and hexstr.startswith("0x") and len(hexstr) == 66, f"not bytes32 hex: {hexstr!r}"
    return Web3.to_bytes(hexstr=hexstr)


class EscrowClient:
    def __init__(self, w3: Web3, address: str, abi: list, accounts: dict | None = None):
        self.w3 = w3
        self.contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
        # {checksum_addr: LocalAccount} for the keyed (Base Sepolia) path; empty => anvil unlocked
        self.accounts = {Web3.to_checksum_address(a): acct for a, acct in (accounts or {}).items()}
        self.deploy_block = None
        self.deploy_receipt = None
        self._confirmed_block = 0
        self._confirmed_block_lock = threading.Lock()

    @staticmethod
    def deploy(w3: Web3, artifact_path: str, deployer: str, verifier: str, accounts: dict | None = None) -> "EscrowClient":
        art = json.loads(open(artifact_path).read())
        abi = art["abi"]
        bytecode = art["bytecode"]["object"]
        accounts = {Web3.to_checksum_address(a): acct for a, acct in (accounts or {}).items()}
        deployer = Web3.to_checksum_address(deployer)
        ctor = w3.eth.contract(abi=abi, bytecode=bytecode).constructor(Web3.to_checksum_address(verifier))
        with _sender_lock(w3, deployer):
            if deployer in accounts:
                tx = ctor.build_transaction({"from": deployer, "nonce": w3.eth.get_transaction_count(deployer, "pending")})
                h = w3.eth.send_raw_transaction(accounts[deployer].sign_transaction(tx).raw_transaction)
            else:
                h = ctor.transact({"from": deployer})
            rcpt = w3.eth.wait_for_transaction_receipt(h)
            _require_success(rcpt)
        client = EscrowClient(w3, rcpt.contractAddress, abi, accounts)
        client.deploy_block = rcpt.blockNumber
        client.deploy_receipt = rcpt
        client._observe_receipt(rcpt)
        return client

    def _observe_receipt(self, receipt):
        with self._confirmed_block_lock:
            self._confirmed_block = max(self._confirmed_block, int(receipt.blockNumber))

    def _read(self, read_at_block):
        """Use one observed snapshot, never older than this client's successful receipts.

        Load-balanced RPC backends may lag a receipt or eth_blockNumber. Retry only a
        missing explicit block, at most six attempts (2.5 seconds of retry delays).
        Returned data and other errors are never reinterpreted. This assumes honest RPC
        block semantics; a receipt is not a finality guarantee or a reorg-proof anchor.
        """
        head = int(self.w3.eth.block_number)
        with self._confirmed_block_lock:
            block = max(head, self._confirmed_block)
        for attempt in range(6):
            try:
                return read_at_block(block)
            except (BlockNotFound, Web3RPCError) as error:
                if not _is_missing_block(error, block) or attempt == 5:
                    raise
                time.sleep(0.5)

    def _send(self, fn, sender: str, value: int = 0):
        sender = Web3.to_checksum_address(sender)
        with _sender_lock(self.w3, sender):
            if sender in self.accounts:
                tx = {"from": sender, "value": value,
                      "nonce": self.w3.eth.get_transaction_count(sender, "pending")}
                # build_transaction otherwise estimates at implicit latest, which can predate
                # a just-confirmed prerequisite (for example challenge before respond).
                tx["gas"] = self._read(lambda block: fn.estimate_gas(tx, block_identifier=block))
                tx = fn.build_transaction(tx)
                h = self.w3.eth.send_raw_transaction(self.accounts[sender].sign_transaction(tx).raw_transaction)
            else:
                h = fn.transact({"from": sender, "value": value})
            receipt = self.w3.eth.wait_for_transaction_receipt(h)
            _require_success(receipt)
            self._observe_receipt(receipt)
            return receipt

    # ── provider ──
    def register_promise(self, sender: str, predicate_hash: str, params_hash: str,
                         payout_wei: int, reserve_wei: int) -> int:
        """Born funded: the initial reserve rides in msg.value and must cover >= one payout."""
        rcpt = self._send(
            self.contract.functions.registerPromise(_b32(predicate_hash), _b32(params_hash), payout_wei),
            sender, value=reserve_wei)
        ev = self.contract.events.PromiseRegistered().process_receipt(rcpt, errors=DISCARD)
        return ev[0]["args"]["promiseId"]

    def fund_promise(self, sender: str, promise_id: int, amount_wei: int):
        """Top up the reserve (anyone; a top-up is a gift). Cures a lapse."""
        return self._send(self.contract.functions.fundPromise(promise_id), sender, value=amount_wei)

    def retire_promise(self, sender: str, promise_id: int):
        """Close new coverage; starts the withdrawal cooldown (retiredAt + CHALLENGE_WINDOW)."""
        return self._send(self.contract.functions.retirePromise(promise_id), sender)

    def withdraw_reserve(self, sender: str, promise_id: int):
        """After the cooldown, with no open challenges: reclaim the remaining reserve."""
        return self._send(self.contract.functions.withdrawReserve(promise_id), sender)

    def open_session(self, sender: str, session_id: str, party: str):
        """Phase 1, at session START: coverage becomes an on-chain fact before any covered action runs."""
        return self._send(
            self.contract.functions.openSession(_b32(session_id), Web3.to_checksum_address(party)), sender)

    def commit_trace(self, sender: str, session_id: str, trace_hash: str):
        """Phase 2, at session END: bind the canonical trace to the opened session."""
        return self._send(self.contract.functions.commitTrace(_b32(session_id), _b32(trace_hash)), sender)

    def checkpoint_trace(self, sender: str, session_id: str, record_count: int, prefix_hash: str):
        """Anchor a cumulative prefix without starting the final challenge clock."""
        return self._send(self.contract.functions.checkpointTrace(
            _b32(session_id), record_count, _b32(prefix_hash)), sender)

    def get_checkpoints(self, session_id: str) -> list[dict]:
        """All historical prefixes from a consistent block snapshot; never just the latest root."""
        sid = _b32(session_id)
        def read_at_block(block):
            count = self.contract.functions.checkpointCount(sid).call(block_identifier=block)
            out = []
            for i in range(count):
                n, h = self.contract.functions.traceCheckpoints(sid, i).call(block_identifier=block)
                out.append({"record_count": int(n), "prefix_hash": Web3.to_hex(h)})
            return out
        return self._read(read_at_block)

    def respond(self, sender: str, challenge_id: int):
        """Provider's on-chain 'the trace is available' — turns off the default path."""
        return self._send(self.contract.functions.respond(challenge_id), sender)

    def claim_default(self, sender: str, challenge_id: int):
        """The store rule: silence past the response window settles the challenge as a violation."""
        rcpt = self._send(self.contract.functions.claimDefault(challenge_id), sender)
        ev = self.contract.events.DefaultClaimed().process_receipt(rcpt, errors=DISCARD)
        return ev[0]["args"]  # {challengeId, paidToChallenger}

    def withdraw_challenge(self, sender: str, challenge_id: int):
        """Challenger exit (bond back, no slash) when no verdict can come."""
        return self._send(self.contract.functions.withdrawChallenge(challenge_id), sender)

    # ── challenger ──
    def challenge(self, sender: str, session_id: str, promise_id: int, bond_wei: int) -> int:
        rcpt = self._send(self.contract.functions.challenge(_b32(session_id), promise_id), sender, value=bond_wei)
        ev = self.contract.events.Challenged().process_receipt(rcpt, errors=DISCARD)
        return ev[0]["args"]["challengeId"]

    # ── verifier (trusted role) ──
    def submit_verdict(self, sender: str, challenge_id: int, violated: bool):
        rcpt = self._send(self.contract.functions.submitVerdict(challenge_id, violated), sender)
        ev = self.contract.events.Verdict().process_receipt(rcpt, errors=DISCARD)
        return ev[0]["args"]  # {challengeId, violated, paidToChallenger}

    # ── reads (gas-free) ──
    def bond_of(self, addr: str) -> int:
        fn = self.contract.functions.bonds(Web3.to_checksum_address(addr))
        return self._read(lambda block: fn.call(block_identifier=block))

    def get_promise(self, promise_id: int):
        fn = self.contract.functions.promises(promise_id)
        return self._read(lambda block: fn.call(block_identifier=block))  # (provider, predHash, paramsHash, payout, reserve, registeredAt, retiredAt)

    def get_session(self, session_id: str):
        fn = self.contract.functions.sessions(_b32(session_id))
        return self._read(lambda block: fn.call(block_identifier=block))  # (provider, party, traceHash, openedAt, committedAt, exists)

    def get_challenge(self, challenge_id: int):
        fn = self.contract.functions.challenges(challenge_id)
        return self._read(lambda block: fn.call(block_identifier=block))  # (sessionId, promiseId, challenger, bond, status, challengedAt, respondedAt)

    def check_coverage(self, session_id: str, party: str, promise_ids: list) -> dict:
        """The harness-side receipt check — everything a party wants to know at session start,
        from public reads: is my session open, am I the party, is the trace committed yet, and is
        each promise currently in force for this session (registered before my open, not retired
        for it, reserve funded). Pure reads; anyone can run it."""
        provider, s_party, trace_hash_b, opened_at, committed_at, exists = self.get_session(session_id)
        out = {
            "session_open": bool(exists),
            "party_matches": exists and Web3.to_checksum_address(party) == s_party,
            "trace_committed": committed_at != 0,
            "promises": {},
        }
        for pid in promise_ids:
            p_provider, _ph, _qh, payout, reserve, registered_at, retired_at = self.get_promise(pid)
            covered = (exists and p_provider == provider
                       and registered_at <= opened_at
                       and (retired_at == 0 or opened_at <= retired_at))
            out["promises"][pid] = {
                "covers_session": covered,
                "funded": reserve >= payout and payout > 0,
                "in_force": covered and reserve >= payout,
            }
        return out
