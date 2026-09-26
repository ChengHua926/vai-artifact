"""Native Hermes settlement matrix on the V2 claim flow: 11 sessions, 14 challenges.

Reproduces the September 16 Base mainnet settlement matrix after the protocol change: the provider sends
nothing on chain in answer to a claim. Its service delivers signed evidence to the verifier's
inbox, and the verifier's service settles every accepted claim. No model calls; approval
answers are controlled. operating_cost/README.md describes the runs.

Processes. This driver is the provider's native Hermes runtime (dispatcher, approval hooks,
adapter and SDK with the provider key) and the challenger (challenger key). It deploys the
escrow with the verifier key and discards that key before the first claim.
scripts/provider_service.py (provider key and store token) and scripts/verifier_service.py
(verifier key, inbox and receiver, no store credentials) run as child processes through the
`service` subcommand. It calls their unchanged main() after adding the experiment's signing
guard, RPC pacing, output-local state files and result recording.

Safety. Every signature in every process passes one guard: chain, this escrow (or the single
deployment), a per-role function list, gas <= 3M, a maximum fee per gas, value <= 1e10 wei, a
total fee ceiling over realized plus reserved fees, and no new signature while an earlier one
from the same wallet lacks a receipt. Each hash is journaled before broadcast. Mainnet needs
--network mainnet --execute-mainnet, Python 3.12.13 and a clean committed checkout. The runner
never transfers funds. `preflight` is read-only.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack, contextmanager
import copy
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
for _package in ("commons", "sdk", "store", "verifier"):
    _path = str(ROOT / "packages" / _package)
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# September 25 handoff driver: paced RPC with identical retries, wallet loading and address
# checks, receipt fee accounting, build validation, child-process and readiness helpers.
H = _load(ROOT / "operating_cost/measure_handoff.py", "settlement_handoff_helpers")
native_pin = _load(ROOT / "operating_cost/native_pin.py", "native_pin")

import requests  # noqa: E402
from eth_abi import encode as abi_encode  # noqa: E402
from eth_account import Account  # noqa: E402
from web3 import Web3  # noqa: E402
from web3.exceptions import BadFunctionCallOutput, BlockNotFound, TransactionNotFound, Web3RPCError  # noqa: E402

from aa_commons import ActionRecord, canonical_bytes, params_hash, registry, trace_hash  # noqa: E402
from aa_commons.trace import check_prefixes  # noqa: E402
from aa_sdk import HttpStore  # noqa: E402
from aa_sdk.chain import EscrowClient, _is_missing_block  # noqa: E402
from aa_sdk.evidence import (EVIDENCE_WINDOW, VERSION as EVIDENCE_VERSION, EvidenceClient,  # noqa: E402
                             evidence_hash, recover_provider)

API = SimpleNamespace(Web3=Web3, Account=Account)
ARTIFACT = ROOT / "contracts/out/Escrow.sol/Escrow.json"
PAYOUT = 10**10                     # run_public.PAYOUT, September 16
BOND = 10**10                       # run_public.BOND, September 16
HERMES_ROOT = ROOT.parent / "hermes-agent"
STRATEGIES = {"10_records_or_30s": (10, 30), "30_records_or_60s": (30, 60),
              "final_only": (10**9, 86400)}
FORMAL_PYTHON = (3, 12, 13)
KEYSTORE_DIR = H.PRIVATE / "roles"
DEFAULT_MAINNET_OUTPUT = ROOT / "operating_cost/results/new-run"
ANVIL = Path.home() / ".foundry/bin/anvil"
ANVIL_MNEMONIC = "test test test test test test test test test test test junk"
L1_ORACLE = "0x420000000000000000000000000000000000000F"
L1_BLOCK = "0x4200000000000000000000000000000000000015"
MAX_GAS = 3_000_000
MAX_VALUE = 10**10
MAX_TRANSACTIONS = 100
L1_HEADROOM_WEI = 5 * 10**12        # measure_handoff.Transactions.send allowance headroom
PROFILES = {
    # Anvil suggests a 1 gwei priority fee whatever its flags, so local runs use a local profile.
    "mainnet": {"chain_id": 8453, "max_fee_per_gas": 10**8, "fee_ceiling": 150 * 10**12},
    "local": {"chain_id": 31337, "max_fee_per_gas": 2 * 10**9, "fee_ceiling": 10**17},
}
ROLE_FUNCTIONS = {
    ("driver", "provider"): ("registerPromise", "openSession", "checkpointTrace", "commitTrace"),
    ("driver", "challenger"): ("challenge",),
    ("driver", "verifier"): (),                  # the one contract creation only
    ("provider-service", "provider"): (),        # evidence signatures only
    ("verifier-service", "verifier"): ("submitVerdict",),
}
OPERATION_ROLE = {"deploy": "verifier", "registerPromise": "provider", "openSession": "provider",
                  "checkpointTrace": "provider", "commitTrace": "provider",
                  "challenge": "challenger", "submitVerdict": "verifier"}
OPERATION_VALUE = {"registerPromise": PAYOUT, "challenge": BOND}
# Largest gas limits (SDK estimates) in the local V2 smoke run; mainnet executes the same EVM
# code. Preflight adds 20% before projecting fees.
PLAN_GAS = {"deploy": 2_186_000, "registerPromise": 164_000, "openSession": 94_000,
            "checkpointTrace": 98_000, "commitTrace": 54_000, "challenge": 211_000,
            "submitVerdict": 97_000}
PLAN_TX_BYTES = {"deploy": 9_800}           # other calls: at most 300 bytes signed
TABLE = (
    ("within_scope", "Approved write within scope, checked for scope", 3, False),
    ("within_authorization", "Same approved write, checked for authorization", 3, False),
    ("outside_scope", "Approved write outside the promised scope", 3, True),
    ("without_approval", "Native write without the promise's required approval", 3, True),
    ("altered_after_final", "Evidence altered after the final commitment", 1, True),
    ("checkpoint_rewrite", "Final trace contradicts an earlier checkpoint", 1, True),
)
EXPECTED = {key: violated for key, _, _, violated in TABLE}
LABELS = {key: label for key, label, _, _ in TABLE}
PREDICATE = {0: "action_within_declared_scope", 1: "no_destructive_without_consent"}
CURRENT = {"case": "setup"}


def utc():
    return H.utc()


def jsonable(value):
    return json.loads(Web3.to_json(value))


def session_plan():
    """September 16 order: three repetitions of within/outside/without approval, then faults.

    Each challenge is (name, promise index, table key); promise 0 is the scope promise (AAP-2)
    and promise 1 the authorization promise (AAP-1), as registered by aa_hermes.begin_session.
    """
    plan = []
    for rep in range(3):
        plan += [
            {"name": f"native-within-{rep}", "kind": "within", "outside": False, "decision": "allow_once",
             "cadence": (10, 30), "records": 4,
             "challenges": [(f"native-within-{rep}", 0, "within_scope"),
                            (f"native-within-{rep}-authorization", 1, "within_authorization")]},
            {"name": f"native-outside-{rep}", "kind": "outside", "outside": True, "decision": "allow_once",
             "cadence": (10, 30), "records": 4,
             "challenges": [(f"native-outside-{rep}", 0, "outside_scope")]},
            {"name": f"native-without-approval-{rep}", "kind": "without_approval", "outside": False,
             "decision": None, "cadence": (10, 30), "records": 1,
             "challenges": [(f"native-without-approval-{rep}", 1, "without_approval")]},
        ]
    # "controlled-altered-after-final" takes the place of September 16's controlled-unavailable.
    for name, kind in (("controlled-altered-after-final", "altered_after_final"),
                       ("controlled-rewrite", "checkpoint_rewrite")):
        plan.append({"name": name, "kind": kind, "outside": False, "decision": "allow_once",
                     "cadence": STRATEGIES["final_only"], "records": 4, "challenges": [(name, 0, kind)]})
    return plan


def planned_operations(deploy=True):
    plan = session_plan()
    sessions, challenges = len(plan), sum(len(s["challenges"]) for s in plan)
    ops = {"registerPromise": 2 * sessions, "openSession": sessions, "checkpointTrace": sessions,
           "commitTrace": sessions, "challenge": challenges, "submitVerdict": challenges}
    return {"deploy": 1, **ops} if deploy else ops


# Identical to operating_cost/native_hermes_original.py.txt, which checkpoint_refresh.py runs.
class NativeHermes:
    """Actual patched dispatch/approval hooks with controlled ACP user responses."""
    def __init__(self, hermes_root, fixtures):
        self.root, self.fixtures = Path(hermes_root).resolve(), Path(fixtures).resolve()
        if not native_pin.matches(self.root, "hermes"):
            raise ValueError("Hermes must be the upstream.json commit with the release patch applied")
        self.fixtures.mkdir(parents=True, exist_ok=True)
        (self.fixtures / "workspace").mkdir(exist_ok=True)
        os.environ["HERMES_HOME"] = str(self.fixtures / "hermes-home")
        os.environ["HERMES_INTERACTIVE"] = "1"
        os.environ["AA_SCOPE_PREFIX"] = str(self.fixtures / "workspace") + "/"
        for path in (str(self.root), str(ROOT / "integrations/hermes")):
            sys.path.insert(0, path)
        import model_tools
        import aa_hermes
        from hermes_cli.plugins import get_plugin_manager
        self.plugin, self.dispatch = aa_hermes, model_tools.handle_function_call
        manager = get_plugin_manager()
        manager._hooks, manager._middleware = {}, {}
        class Context:
            def register_hook(self, name, callback):
                manager._hooks.setdefault(name, []).append(callback)
            def register_middleware(self, name, callback):
                manager._middleware.setdefault(name, []).append(callback)
        aa_hermes.register(Context())
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.sources = {"model_tools": str(Path(model_tools.__file__).resolve()),
                        "aa_hermes": str(Path(aa_hermes.__file__).resolve())}

    def begin(self, name, store, escrow, provider, party, cadence=(10, 30)):
        acc, session = self.plugin.begin_session(party=party, store=store, chain=escrow,
            provider_addr=provider, payout_wei=PAYOUT, native_session_id=name)
        acc.checkpoint_records, acc.checkpoint_seconds = cadence
        return acc, session

    def write(self, name, index, *, outside=False, decision="allow_once"):
        from acp.schema import AllowedOutcome
        from acp_adapter.edit_approval import (make_acp_edit_approval_requester,
            set_edit_approval_requester, reset_edit_approval_requester)
        async def answer(**kwargs):
            return SimpleNamespace(outcome=AllowedOutcome(outcome="selected", option_id=decision))
        token = (set_edit_approval_requester(make_acp_edit_approval_requester(answer, self.loop, name))
                 if decision is not None else None)
        path = self.fixtures / ("outside" if outside else "workspace") / f"{name}-{index}.txt"
        path.parent.mkdir(exist_ok=True)
        content = f"native experiment fixture {index}\n" + "x" * 1024
        try:
            started = time.perf_counter()
            result = self.dispatch("write_file", {"path": str(path), "content": content},
                session_id=name, tool_call_id=f"{name}-{index}")
            elapsed = time.perf_counter() - started
            if decision != "deny":
                assert path.read_text() == content, result
            else:
                assert not path.exists(), result
            return {"tool": "write_file", "path": str(path), "result": result,
                    "effect_sha256": hashlib.sha256(content.encode()).hexdigest() if path.exists() else None,
                    "action_seconds": elapsed, "completed_at_unix": time.time()}
        finally:
            if token is not None:
                reset_edit_approval_requester(token)

    def end(self, name):
        return self.plugin.end_session(native_session_id=name)


# ── shared transaction journal and signing guard ────────────────────────────────────────────
def _find(rows, tx_hash):
    return next((row for row in rows if row["hash"].lower() == tx_hash.lower()), None)


def committed_fee(row):
    """Realized canonical fee, else the conservative reservation made at signing."""
    return row["fee_wei"] if row.get("fee_wei") is not None else row.get("fee_upper_wei", 0)


class Journal:
    """One signed-transaction journal for the driver and both services.

    A POSIX lock serializes each read-modify-write across processes, so the fee ceiling check
    and the row append are atomic, and rows are replaced atomically with fsync.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def _write(self, rows):
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(temporary, "w") as handle:
            handle.write(json.dumps(rows, indent=2, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    @contextmanager
    def locked(self):
        with open(self.lock_path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                rows = self._read()
                yield rows
                self._write(rows)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def rows(self):
        with open(self.lock_path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            try:
                return self._read()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _first(receipt, seconds):
        return {"block_number": int(receipt["blockNumber"]), "block_hash": Web3.to_hex(receipt["blockHash"]),
                "status": int(receipt["status"]), "preconfirmation": bytes(receipt["blockHash"]) == bytes(32),
                "observed_at_unix": time.time(), "seconds": seconds}

    def observe(self, receipt, seconds=None):
        """Record the first receipt the sender saw (may be a Base preconfirmation)."""
        tx_hash = Web3.to_hex(receipt["transactionHash"])
        first = self._first(receipt, seconds)
        with self.locked() as rows:
            row = _find(rows, tx_hash)
            if row is None:   # a transaction that bypassed every guard fails the completeness check
                rows.append({"hash": tx_hash, "status": "unjournaled", "sender": receipt["from"],
                             "operation": None, "fee_upper_wei": 0, "first_receipt": first})
                return
            row.setdefault("first_receipt", first)
            if row["status"] == "signed":
                row["status"] = "observed" if first["status"] == 1 else "reverted"

    def canonicalize(self, w3, tx_hash, mainnet, wait=30.0):
        """Refresh to the canonical receipt (nonzero block hash) and record its exact fees."""
        tx_hash = Web3.to_hex(tx_hash)
        deadline = time.monotonic() + wait
        while True:
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                receipt = None
            if receipt is not None and bytes(receipt["blockHash"]) != bytes(32):
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(.5)
        if mainnet and "l1Fee" not in receipt:
            raise RuntimeError("canonical receipt lacks its L1 data fee; reconcile before continuing")
        execution = int(receipt["gasUsed"]) * int(receipt["effectiveGasPrice"])
        l1 = H.number(receipt.get("l1Fee", 0))
        with self.locked() as rows:
            row = _find(rows, tx_hash)
            if row is None:
                rows.append({"hash": tx_hash, "status": "unjournaled", "sender": receipt["from"],
                             "operation": None, "fee_upper_wei": 0})
                row = rows[-1]
            row.setdefault("first_receipt", self._first(receipt, None))
            row.update(status="canonical" if int(receipt["status"]) == 1 else "reverted",
                       block_number=int(receipt["blockNumber"]), block_hash=Web3.to_hex(receipt["blockHash"]),
                       gas_used=int(receipt["gasUsed"]), effective_gas_price_wei=int(receipt["effectiveGasPrice"]),
                       execution_fee_wei=execution, l1_fee_wei=l1, fee_wei=execution + l1,
                       canonical_at_utc=utc(), receipt=jsonable(receipt))
        return True

    def reconcile(self, w3, mainnet, wait=30.0):
        """Canonicalize every row without a realized fee; return hashes still unresolved."""
        pending = [row["hash"] for row in self.rows() if row.get("fee_wei") is None]
        return [tx_hash for tx_hash in pending if not self.canonicalize(w3, tx_hash, mainnet, wait)]


def signing_policy(w3, network, process, role, *, target=None, init_code=None, fee_ceiling=None,
                   case_label=lambda: None):
    profile = PROFILES[network]
    abi = json.loads(ARTIFACT.read_text())["abi"]
    selectors = {}
    for entry in abi:
        if entry["type"] == "function" and entry["name"] in ROLE_FUNCTIONS[(process, role)]:
            signature = f"{entry['name']}({','.join(item['type'] for item in entry['inputs'])})"
            selectors[Web3.to_hex(Web3.keccak(text=signature)[:4])] = entry["name"]
    decoder = w3.eth.contract(abi=abi)
    oracle = w3.eth.contract(address=L1_ORACLE, abi=[{
        "name": "getL1FeeUpperBound", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "size", "type": "uint256"}], "outputs": [{"type": "uint256"}]}])

    def l1_allowance(size):
        # measure_handoff.Transactions.send: live upper estimate x2 plus 5 microETH headroom.
        if network != "mainnet":
            return 0
        return 2 * int(oracle.functions.getL1FeeUpperBound(size + 300).call()) + L1_HEADROOM_WEI

    def decode(data):
        try:
            _, arguments = decoder.decode_function_input(data)
        except Exception:
            return {}
        return {key: Web3.to_hex(value) if isinstance(value, (bytes, bytearray)) else value
                for key, value in arguments.items()}

    def receipt_of(tx_hash):
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None

    return {"network": network, "process": process, "role": role, "chain_id": profile["chain_id"],
            "target": Web3.to_checksum_address(target) if target else None, "deploy_init_code": init_code,
            "selectors": selectors, "max_gas": MAX_GAS, "max_fee_per_gas": profile["max_fee_per_gas"],
            "max_value": MAX_VALUE, "fee_ceiling": fee_ceiling or profile["fee_ceiling"],
            "max_transactions": MAX_TRANSACTIONS, "l1_allowance": l1_allowance,
            "balance_of": lambda address: int(w3.eth.get_balance(address, "latest")),
            "receipt_of": receipt_of, "decode": decode, "case_label": case_label,
            "sign_messages": process == "provider-service"}


class GuardedSigner:
    """September 16 LimitedSigner limits plus the September 25 guard's journal and ceiling."""
    def __init__(self, account, *, journal, policy):
        self.account, self.address = account, account.address
        self.journal, self.policy = journal, policy
        self.deployed = False

    def sign_message(self, message):
        if not self.policy["sign_messages"]:
            raise ValueError("this experiment signer does not sign evidence")
        return self.account.sign_message(message)

    def _resolve_previous(self):
        for row in self.journal.rows():
            if row.get("sender") == self.address and row["status"] == "signed":
                receipt = self.policy["receipt_of"](row["hash"])
                if receipt is not None:
                    self.journal.observe(receipt)

    def sign_transaction(self, tx):
        p = self.policy
        if int(tx.get("chainId", 0)) != p["chain_id"]:
            raise ValueError("refusing a transaction on the wrong chain")
        data = tx.get("data", "0x")
        data = (Web3.to_hex(data) if isinstance(data, (bytes, bytearray)) else data).lower()
        if tx.get("to") in (None, "", b""):
            if not p["deploy_init_code"] or self.deployed or data != p["deploy_init_code"].lower():
                raise ValueError("refusing any contract creation except the one experimental escrow")
            operation, arguments = "deploy", {"verifier": self.address}
        else:
            if p["target"] is None or Web3.to_checksum_address(tx["to"]) != p["target"]:
                raise ValueError("refusing a transaction outside the experimental escrow")
            operation = p["selectors"].get(data[:10])
            if operation is None:
                raise ValueError(f"refusing a function the {p['process']} {p['role']} does not call")
            arguments = p["decode"](data)
        gas = int(tx["gas"])
        price = int(tx.get("maxFeePerGas", tx.get("gasPrice", 0)))
        value = int(tx.get("value", 0))
        if not 0 < gas <= p["max_gas"]:
            raise ValueError("gas limit exceeds the experiment ceiling")
        if not 0 < price <= p["max_fee_per_gas"]:
            raise ValueError("maximum fee per gas exceeds the experiment ceiling")
        if not 0 <= value <= p["max_value"]:
            raise ValueError("value exceeds the experimental reserve or bond")
        signed = self.account.sign_transaction(tx)
        raw = bytes(signed.raw_transaction)
        tx_hash = Web3.to_hex(Web3.keccak(raw))
        l1_allowance = p["l1_allowance"](len(raw))
        upper = gas * price + l1_allowance
        if p["balance_of"](self.address) < value + upper:
            raise ValueError("wallet does not cover value and the conservative fee")
        self._resolve_previous()
        with self.journal.locked() as rows:
            if any(r.get("sender") == self.address and r["status"] == "signed" for r in rows):
                raise RuntimeError("an earlier signed transaction from this wallet has no receipt; "
                                   "reconcile the journal before any new signature")
            if len(rows) >= p["max_transactions"]:
                raise ValueError("experiment transaction limit reached")
            committed = sum(committed_fee(r) for r in rows)
            if committed + upper > p["fee_ceiling"]:
                raise ValueError(f"experiment fee ceiling: {committed} committed + {upper} "
                                 f"upper bound > {p['fee_ceiling']} wei")
            rows.append({"index": len(rows), "hash": tx_hash, "status": "signed", "process": p["process"],
                         "pid": os.getpid(), "role": p["role"], "sender": self.address,
                         "operation": operation, "case": p["case_label"](), "arguments": arguments,
                         "nonce": int(tx["nonce"]), "to": tx.get("to"), "value_wei": value,
                         "gas_limit": gas, "max_fee_per_gas_wei": price,
                         "max_priority_fee_per_gas_wei": tx.get("maxPriorityFeePerGas"),
                         "raw_size_bytes": len(raw), "l1_fee_allowance_wei": l1_allowance,
                         "fee_upper_wei": upper, "committed_before_wei": committed,
                         "signed_at_utc": utc()})
        if operation == "deploy":
            self.deployed = True
        return signed


def install_receipt_recorder(journal, mainnet):
    """Record the first receipt and canonical fees for every SDK send in this process."""
    if getattr(EscrowClient, "_settlement_recorder", False):
        return
    original = EscrowClient._send

    def _send(self, fn, sender, value=0):
        started = time.perf_counter()
        receipt = original(self, fn, sender, value)
        journal.observe(receipt, time.perf_counter() - started)
        try:
            journal.canonicalize(self.w3, receipt["transactionHash"], mainnet)
        except Exception as error:   # accounting never changes the SDK call's outcome
            print(f"canonical receipt refresh failed; reconciled later: {error}", file=sys.stderr, flush=True)
        return receipt

    EscrowClient._send = _send
    EscrowClient._settlement_recorder = True


def append_jsonl(path, entry, lock=threading.Lock()):
    with lock, open(path, "a") as handle:
        handle.write(json.dumps(entry, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def monotonic_head(w3):
    """Never report a lower block height than this process has already seen.

    Load-balanced public RPC backends can answer eth_blockNumber from behind. The services scan
    Challenged events up to one head and then read the claim at another; a lagging second head
    makes a fresh claim look nonexistent (status 0), which the services treat as closed. A
    monotonic head changes no protocol logic; reads at a block a backend lacks still retry.
    """
    provider, lock, highest = w3.provider, threading.Lock(), [0]
    original = provider.make_request

    def make_request(method, params):
        response = original(method, params)
        if method == "eth_blockNumber" and isinstance(response, dict) and isinstance(response.get("result"), str):
            with lock:
                highest[0] = max(highest[0], int(response["result"], 16))
                response = {**response, "result": hex(highest[0])}
        return response
    provider.make_request = make_request
    return w3


def local_account(address):
    API.Account.enable_unaudited_hdwallet_features()
    for index in range(10):
        account = API.Account.from_mnemonic(ANVIL_MNEMONIC, account_path=f"m/44'/60'/0'/0/{index}")
        if account.address == Web3.to_checksum_address(address):
            return account
    raise ValueError("local role is not an Anvil development account")


# ── service processes ───────────────────────────────────────────────────────────────────────
def recorded_process_challenge(process_challenge, path):
    errors = {}

    def wrapped(escrow, inbox, challenge_id, verifier_addr):
        try:
            result = process_challenge(escrow, inbox, challenge_id, verifier_addr)
        except Exception as error:
            text = f"{type(error).__name__}: {str(error)[:300]}"
            if errors.get(challenge_id) != text:
                errors[challenge_id] = text
                append_jsonl(path, {"challenge_id": challenge_id, "error": text, "at_utc": utc()})
            raise
        if result.get("violated") is not None:
            append_jsonl(path, {**result, "at_utc": utc(), "at_unix": time.time(), "pid": os.getpid()})
        return result
    return wrapped


def record_deliveries(path):
    original = EvidenceClient.submit

    def submit(self, envelope):
        payload = envelope["payload"]
        entry = {"challenge_id": payload.get("challenge_id"), "session_id": payload.get("session_id"),
                 "promise_id": payload.get("promise_id"), "evidence_hash": evidence_hash(payload),
                 "record_count": len(payload.get("records") or []), "signer": recover_provider(envelope),
                 "submitted_at_utc": utc(), "pid": os.getpid()}
        try:
            entry["records_trace_hash"] = trace_hash([ActionRecord.from_dict(r) for r in payload["records"]])
            entry["params_hash"] = params_hash(payload["params"])
        except Exception as error:
            entry["digest_error"] = str(error)
        try:
            receipt = original(self, envelope)
        except Exception as error:
            append_jsonl(path, {**entry, "outcome": "error", "error": f"{type(error).__name__}: {error}"})
            raise
        append_jsonl(path, {**entry, "outcome": "delivered", "receipt": receipt, "at_unix": time.time()})
        return receipt
    EvidenceClient.submit = submit


def service(args):
    """Run scripts/{provider,verifier}_service.py main() inside the experiment envelope."""
    role, network, output = args.role, args.network, Path(args.output).resolve()
    mainnet = network == "mainnet"
    if mainnet and not args.execute_mainnet:
        raise ValueError("mainnet services require --execute-mainnet")
    if role == "verifier" and {"STORE_TOKEN", "STORE_URL"} & set(os.environ):
        raise RuntimeError("the verifier must not receive provider store credentials")
    if role == "provider" and not os.environ.get("STORE_TOKEN"):
        raise RuntimeError("the provider service needs its private store token")
    sys.path.insert(0, str(ROOT / "scripts"))
    import _config as C
    if C.CHAIN_ID != PROFILES[network]["chain_id"] or C.LOCAL == mainnet:
        raise ValueError("service chain configuration differs from the experiment network")
    module = importlib.import_module(f"{role}_service")
    C.verify_package_origins()
    journal = Journal(output / "transactions.json")
    install_receipt_recorder(journal, mainnet)
    C.RUNTIME = str(output / "runtime")          # cursors live with this run, not in the checkout

    def paced_w3():
        C.verify_package_origins()
        connected = monotonic_head(H.connection(API, C.RPC_URL))
        if int(connected.eth.chain_id) != C.CHAIN_ID:
            raise ValueError("connected chain differs from the service configuration")
        return connected
    C.w3 = paced_w3
    original_role_actors = C._role_actors

    def guarded_role_actors(connected_w3, wanted, deployment=None):
        if wanted != role:
            raise ValueError("a service may load only its own role")
        out = original_role_actors(connected_w3, wanted, deployment)
        deployment = C.load_deployment() if deployment is None else deployment
        address, account = out[wanted]
        if account is None:
            if mainnet:
                raise ValueError("mainnet roles need keystore accounts")
            account = local_account(address)
        policy = signing_policy(connected_w3, network, f"{role}-service", wanted,
                                target=deployment["address"], fee_ceiling=args.fee_ceiling_wei)
        out[wanted] = (address, GuardedSigner(account, journal=journal, policy=policy))
        out["deployer"] = out["verifier"]
        return out
    C._role_actors = guarded_role_actors
    original_escrow_client = C.escrow_client

    def announced_escrow_client(connected_w3, actors):
        client = original_escrow_client(connected_w3, actors)   # runtime, artifact and role checks
        print(json.dumps({"ready": role, "escrow": client.contract.address, "at_utc": utc()}), flush=True)
        return client
    C.escrow_client = announced_escrow_client
    if role == "verifier":
        module.process_challenge = recorded_process_challenge(module.process_challenge,
                                                              output / "verifier-results.jsonl")
    else:
        record_deliveries(output / "provider-deliveries.jsonl")
    print(json.dumps({"service": role, "pid": os.getpid(), "network": network,
                      "module": str(Path(module.__file__).resolve().relative_to(ROOT)),
                      "runtime_state": C.RUNTIME, "started_at_utc": utc()}), flush=True)
    module.main()


# ── chain and store helpers ─────────────────────────────────────────────────────────────────
def operator_fees(w3):
    oracle = w3.eth.contract(address=L1_BLOCK, abi=[
        {"name": name, "type": "function", "stateMutability": "view", "inputs": [],
         "outputs": [{"type": "uint32" if name.endswith("Scalar") else "uint64"}]}
        for name in ("operatorFeeScalar", "operatorFeeConstant")])
    return int(oracle.functions.operatorFeeScalar().call()), int(oracle.functions.operatorFeeConstant().call())


def role_addresses(network):
    if network == "mainnet":
        public = json.loads(H.PUBLIC.read_text())["role_addresses"]
        return {role: Web3.to_checksum_address(public[role]) for role in ("provider", "challenger", "verifier")}
    accounts = H.wallets(API, "local", roles=("provider", "challenger", "verifier"))
    return {role: account.address for role, account in accounts.items()}


DEPLOYMENT_KEYS = ("chain_id", "address", "deploy_block", "artifact_sha256",
                   "provider_addr", "verifier_addr", "challenger_addr")


def verify_deployment(w3, path, network, addresses):
    """Reuse only a deployment of the current build, with the experiment roles, no open claims."""
    deployment = json.loads(Path(path).read_text())
    if not set(DEPLOYMENT_KEYS) <= set(deployment):
        raise ValueError(f"deployment.json needs {DEPLOYMENT_KEYS}")
    deployment = {key: deployment[key] for key in DEPLOYMENT_KEYS}
    artifact_bytes = ARTIFACT.read_bytes()
    artifact = json.loads(artifact_bytes)
    H.validate_build(API, artifact)
    if deployment.get("chain_id") != PROFILES[network]["chain_id"] or int(w3.eth.chain_id) != deployment["chain_id"]:
        raise ValueError("deployment chain differs from the experiment network")
    if hashlib.sha256(artifact_bytes).hexdigest() != deployment.get("artifact_sha256"):
        raise ValueError("deployment artifact differs from the current build")
    code = bytes.fromhex(artifact["deployedBytecode"]["object"].removeprefix("0x"))
    if bytes(w3.eth.get_code(deployment["address"])) != code:
        raise ValueError("deployed runtime differs from the current build")
    contract = w3.eth.contract(address=deployment["address"], abi=artifact["abi"])
    for role in ("provider", "challenger", "verifier"):
        if Web3.to_checksum_address(deployment[f"{role}_addr"]) != addresses[role]:
            raise ValueError(f"deployment {role} differs from the experiment role")
    if contract.functions.owner().call() != addresses["verifier"] or contract.functions.verifier().call() != addresses["verifier"]:
        raise ValueError("deployment operator differs from the verifier role")
    for cid in range(1, int(contract.functions.nextChallengeId().call())):
        if int(contract.functions.challenges(cid).call()[4]) == 1:
            raise ValueError(f"deployment has open claim {cid}; settle it before reuse")
    return deployment


def read_at(block, read, attempts=60):
    """Read one block's state; public RPC backends can briefly lag a returned receipt.

    Only reads are retried: a missing block or a transport failure, never returned data.
    """
    for attempt in range(attempts):
        try:
            return read(block)
        except (BlockNotFound, Web3RPCError, requests.RequestException) as error:
            transient = isinstance(error, requests.RequestException) or _is_missing_block(error, block)
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(.5)


def confirm_deployment(w3, address, block, expected, operator, timeout=90):
    """Runtime and roles at the receipt block (run_evidence.confirmed_runtime/operator logic)."""
    contract = w3.eth.contract(address=address, abi=json.loads(ARTIFACT.read_text())["abi"])
    deadline = time.monotonic() + timeout
    while True:
        try:
            runtime = bytes(w3.eth.get_code(address, block))
            if runtime and runtime != expected:
                raise RuntimeError("deployed runtime differs from the build")
            if runtime:
                roles = (contract.functions.owner().call(block_identifier=block),
                         contract.functions.verifier().call(block_identifier=block))
                if roles != (operator, operator):
                    raise RuntimeError("deployed owner/verifier differs from the verifier role")
                return
        except (BlockNotFound, Web3RPCError, BadFunctionCallOutput, requests.RequestException) as error:
            if isinstance(error, (BlockNotFound, Web3RPCError)) and not _is_missing_block(error, block):
                raise
        if time.monotonic() >= deadline:
            raise TimeoutError("deployment not readable at its receipt block")
        time.sleep(.5)


def wait_until_visible(w3, address, expected, needed=3, timeout=120):
    """Services check the runtime at 'latest'; start them only once several reads agree."""
    deadline, streak = time.monotonic() + timeout, 0
    while streak < needed:
        try:
            streak = streak + 1 if bytes(w3.eth.get_code(address)) == expected else 0
        except requests.RequestException:
            streak = 0
        if streak < needed:
            if time.monotonic() >= deadline:
                raise TimeoutError("deployment not consistently visible at the latest block")
            time.sleep(.5)


def pair_key(session_id, promise_id):
    return Web3.keccak(abi_encode(["bytes32", "uint256"], [bytes.fromhex(session_id[2:]), int(promise_id)]))


def claim_state(escrow, block, *, session_id, promise_id, challenge_id, provider, challenger):
    functions, w3 = escrow.contract.functions, escrow.w3
    key = pair_key(session_id, promise_id)

    def read(b):
        return {"block": b, "reserve_wei": int(functions.promises(promise_id).call(block_identifier=b)[4]),
                "provider_bond_credit_wei": int(functions.bonds(provider).call(block_identifier=b)),
                "challenger_balance_wei": int(w3.eth.get_balance(challenger, b)),
                "contract_balance_wei": int(w3.eth.get_balance(escrow.contract.address, b)),
                "challenge": jsonable(functions.challenges(challenge_id).call(block_identifier=b)),
                "pair_resolved": bool(functions.resolved(key).call(block_identifier=b)),
                "pair_challenged": bool(functions.pairChallenged(key).call(block_identifier=b)),
                "open_challenges": int(functions.openChallenges(promise_id).call(block_identifier=b))}
    return read_at(block, read)


def wait_for_event(event, cid, from_block, timeout, services, poll):
    deadline = time.monotonic() + timeout
    while True:
        for name, proc in services.items():
            if proc.poll() is not None:
                raise RuntimeError(f"{name} exited with {proc.returncode}; inspect its log")
        try:
            logs = event().get_logs(from_block=from_block, argument_filters={"challengeId": cid})
        except (BlockNotFound, Web3RPCError, requests.RequestException):
            logs = []                     # a transient read failure; poll again
        if logs:
            return logs[0]
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no {event.event_name} event for claim {cid} within {timeout} seconds")
        time.sleep(poll)


def wait_for_line(path, cid, key, timeout):
    deadline = time.monotonic() + timeout
    while True:
        rows = [row for row in read_jsonl(path) if row.get("challenge_id") == cid and key(row)]
        if rows or time.monotonic() >= deadline:
            return rows
        time.sleep(.25)


def rewrite_store_record(db_path, session_id, seq, content):
    """Provider-side tampering: rewrite one landed record in its own store database.

    The HTTP API is first-write-wins, so the provider edits the SQLite file it controls.
    """
    db = sqlite3.connect(db_path, timeout=30)
    try:
        row = db.execute("SELECT payload FROM records WHERE session_id=? AND seq=?", (session_id, seq)).fetchone()
        if row is None:
            raise ValueError("record to rewrite does not exist")
        before = json.loads(row[0])
        after = copy.deepcopy(before)
        after["args"]["content"] = content
        if after == before:
            raise ValueError("rewrite must change the record")
        with db:
            db.execute("UPDATE records SET payload=? WHERE session_id=? AND seq=?",
                       (json.dumps(after, ensure_ascii=False, allow_nan=False), session_id, seq))
    finally:
        db.close()
    return before, after


def inbox_contents(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        evidence = {int(r[0]): {"payload": json.loads(r[1]), "signature": r[2], "provider": r[3],
                                "evidence_hash": r[4], "received_at": int(r[5])}
                    for r in db.execute("SELECT challenge_id, payload, signature, provider, evidence_hash, "
                                        "received_at FROM evidence")}
        closed = {int(r[0]): int(r[1]) for r in db.execute("SELECT challenge_id, closed_at FROM closed_without_evidence")}
    finally:
        db.close()
    return evidence, closed


def snapshot(escrow, store, session):
    state = escrow.get_session(session.session_id)
    return {"records": store.get_records(session.session_id),
            "final_trace_hash": Web3.to_hex(state[2]), "opened_at": state[3],
            "committed_at": state[4], "checkpoints": escrow.get_checkpoints(session.session_id)}


def git_state():
    run = lambda *command: subprocess.check_output(["git", *command], cwd=ROOT, text=True).strip()
    return run("rev-parse", "HEAD"), run("status", "--porcelain"), run("diff", "--stat", "HEAD")


SNAPSHOT_PREFIXES = ("packages/", "contracts/src/", "scripts/", "operating_cost/", "integrations/hermes/aa_hermes/")
SNAPSHOT_SUFFIXES = {".py", ".sol", ".toml", ".txt", ".yaml", ".md", ".ts", ".json"}


def source_hashes():
    names = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, text=True).splitlines()
    selected = sorted({name for name in names if name.startswith(SNAPSHOT_PREFIXES)
                       and Path(name).suffix in SNAPSHOT_SUFFIXES and (ROOT / name).is_file()})
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in selected}
    hashes["contracts/out/Escrow.sol/Escrow.json"] = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
    return hashes


def source_snapshot(output):
    hashes = source_hashes()
    for name in hashes:
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    H.dump(output / "source-sha256.json", hashes)
    return hashes


def free(port):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


# ── cost planning (shared by preflight and the mainnet start check) ─────────────────────────
def plan_costs(w3, network, deploy, *, deploy_gas=None, init_code_bytes=None):
    """Expected and conservative fees by role at the current Base fee, plus reservation peak."""
    mainnet = network == "mainnet"
    block = w3.eth.get_block("latest")
    base = int(block.get("baseFeePerGas", 0))
    priority = int(w3.eth.max_priority_fee)
    max_fee = priority + 2 * base               # web3's default EIP-1559 fill used by the SDK
    oracle = w3.eth.contract(address=L1_ORACLE, abi=[{
        "name": "getL1FeeUpperBound", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "size", "type": "uint256"}], "outputs": [{"type": "uint256"}]}])
    l1_upper = {}
    for op in planned_operations(deploy):
        size = (init_code_bytes or PLAN_TX_BYTES["deploy"]) + 150 if op == "deploy" else 300
        l1_upper[op] = int(oracle.functions.getL1FeeUpperBound(size + 300).call()) if mainnet else 0
    rows, roles = {}, {}
    for op, count in planned_operations(deploy).items():
        gas = int((deploy_gas if op == "deploy" and deploy_gas else PLAN_GAS[op]) * 1.2)
        expected = gas * (base + priority) + l1_upper[op]
        conservative = gas * max_fee + 2 * l1_upper[op]
        reservation = conservative + (L1_HEADROOM_WEI if mainnet else 0)
        rows[op] = {"count": count, "planning_gas": gas, "expected_fee_each_wei": expected,
                    "conservative_fee_each_wei": conservative, "reservation_each_wei": reservation,
                    "value_each_wei": OPERATION_VALUE.get(op, 0)}
        role = roles.setdefault(OPERATION_ROLE[op], {"transactions": 0, "expected_fee_wei": 0,
                                                     "conservative_fee_wei": 0, "value_wei": 0,
                                                     "returned_value_wei": 0, "largest_reservation_wei": 0})
        role["transactions"] += count
        role["expected_fee_wei"] += count * expected
        role["conservative_fee_wei"] += count * conservative
        role["value_wei"] += count * OPERATION_VALUE.get(op, 0)
        role["largest_reservation_wei"] = max(role["largest_reservation_wei"], reservation)
    violations = sum(EXPECTED[key] for spec in session_plan() for _, _, key in spec["challenges"])
    roles["challenger"]["returned_value_wei"] = violations * (PAYOUT + BOND)
    for role in roles.values():
        # Enough for every expected fee and value plus one conservative reservation at the end.
        role["required_balance_wei"] = role["expected_fee_wei"] + role["value_wei"] + role["largest_reservation_wei"]
        # Net wallet change: fees plus reserves/bonds sent, less payouts received (8 x (p + b)).
        role["projected_spend_wei"] = role["expected_fee_wei"] + role["value_wei"] - role["returned_value_wei"]
        role["projected_spend_conservative_wei"] = (role["conservative_fee_wei"] + role["value_wei"]
                                                    - role["returned_value_wei"])
    expected_total = sum(r["count"] * r["expected_fee_each_wei"] for r in rows.values())
    small = sorted((r["reservation_each_wei"] for op, r in rows.items() if op != "deploy"), reverse=True)
    # Receipts are canonicalized right after each send, so at most two transactions (driver and
    # verifier service) are reserved at once; the deployment is reserved alone at the start.
    peak = max(expected_total + sum(small[:2]), rows["deploy"]["reservation_each_wei"] if deploy else 0)
    return {"base_fee_per_gas_wei": base, "max_priority_fee_per_gas_wei": priority,
            "sdk_max_fee_per_gas_wei": max_fee, "operations": rows, "roles": roles,
            "transactions": sum(planned_operations(deploy).values()),
            "expected_total_fee_wei": expected_total, "peak_committed_fee_wei": peak}


# ── the experiment ──────────────────────────────────────────────────────────────────────────
class Run:
    def __init__(self, args):
        self.args = args
        self.network = args.network
        self.mainnet = self.network == "mainnet"
        self.profile = PROFILES[self.network]
        self.fee_ceiling = args.fee_ceiling_wei or self.profile["fee_ceiling"]
        self.output = Path(args.output or (DEFAULT_MAINNET_OUTPUT if self.mainnet else "")).resolve()
        self.services = {}
        self.cases = []
        self.sessions = {}

    def write_evidence(self):
        H.dump(self.output / "evidence.json", {**self.evidence, "scenarios": self.cases,
                                               "transactions": self.journal.rows()})

    def preconditions(self):
        args = self.args
        if self.mainnet and not args.execute_mainnet:
            raise ValueError("mainnet sends require --network mainnet --execute-mainnet")
        if not args.output and not self.mainnet:
            raise ValueError("local runs need --output")
        if self.output.exists():
            raise ValueError("use a fresh output directory; never overwrite an experiment")
        if self.mainnet and tuple(sys.version_info[:3]) != FORMAL_PYTHON:
            raise ValueError("formal mainnet runs require the original Python 3.12.13 runtime")
        self.revision, self.status, self.diffstat = git_state()
        if self.status and (self.mainnet or not args.allow_dirty):
            raise ValueError("run requires committed clean source; --allow-dirty is local-only")
        if self.fee_ceiling > self.profile["fee_ceiling"] and self.mainnet:
            raise ValueError("mainnet fee ceiling may only be lowered")
        ports = [args.store_port, args.evidence_port] + ([args.anvil_port] if not (self.mainnet or args.rpc_url) else [])
        for port in ports:
            if not 18500 <= port <= 18599:
                raise ValueError("experiment ports must stay within 18500-18599")
            free(port)

    def start(self, stack):
        args = self.args
        self.base_env = {k: v for k, v in os.environ.items()
                         if not k.startswith(("AA_", "STORE_", "EVIDENCE_", "HERMES_"))}
        self.output.mkdir(parents=True)
        self.hashes = source_snapshot(self.output)
        self.journal = Journal(self.output / "transactions.json")
        install_receipt_recorder(self.journal, self.mainnet)
        self.started = time.time()
        self.evidence = {
            "status": "running", "started_at_utc": utc(), "network": self.network,
            "claim_flow": "V2: no on-chain response; provider service delivers signed evidence to the "
                          "verifier inbox; verifier service settles every accepted claim",
            "source_revision": self.revision, "source_dirty": bool(self.status),
            "source_status": self.status.splitlines(), "source_diffstat": self.diffstat.splitlines(),
            "artifact_sha256": hashlib.sha256(ARTIFACT.read_bytes()).hexdigest(),
            "hermes_revision": native_pin.describe("hermes"), "python": sys.version, "executable": sys.executable,
            "machine": platform.platform(), "payout_wei": PAYOUT, "challenge_bond_wei": BOND,
            "fee_ceiling_wei": self.fee_ceiling, "signing_limits": {
                "chain_id": self.profile["chain_id"], "max_gas": MAX_GAS,
                "max_fee_per_gas_wei": self.profile["max_fee_per_gas"], "max_value_wei": MAX_VALUE,
                "max_transactions": MAX_TRANSACTIONS, "l1_allowance": "2 x getL1FeeUpperBound(size+300) + 5 microETH (mainnet)"},
            "planned_operations": planned_operations(not args.deployment),
            "clock_policy": "unchanged contract windows; evidence deadline (filing + 3 days) and verdict window "
                            "(10 days) never reached; no time travel",
            "capture_origin": "real Hermes tool handler/ACP approval hooks; deterministic controlled user answers, no model",
            "roles": "separate keys; driver holds provider+challenger (verifier only for deployment); provider "
                     "and verifier services are separate processes; verifier has no store credentials; one "
                     "experimental operator",
            "predicates": registry.catalog(), "table": [
                {"key": k, "case": label, "challenges": n, "expected": "violation" if v else "no violation"}
                for k, label, n, v in TABLE]}
        if self.mainnet:
            self.rpc = args.rpc_url or H.RPC
        else:
            self.rpc = args.rpc_url or f"http://127.0.0.1:{args.anvil_port}"
            if not args.rpc_url:
                # Base-like 0.006 gwei base fee; Anvil still suggests a 1 gwei priority fee.
                stack.enter_context(H.child([str(ANVIL), "--host", "127.0.0.1", "--port", str(args.anvil_port),
                                             "--chain-id", "31337", "--base-fee", "6000000", "--silent"],
                                            self.output / "anvil.log"))
                for _ in range(100):
                    try:
                        if Web3(Web3.HTTPProvider(self.rpc)).eth.chain_id == 31337:
                            break
                    except Exception:
                        time.sleep(.1)
        self.w3 = w3 = monotonic_head(H.connection(API, self.rpc))
        if int(w3.eth.chain_id) != self.profile["chain_id"]:
            raise ValueError("connected chain differs from the experiment network")
        self.evidence.update(chain_id=int(w3.eth.chain_id), rpc_url=self.rpc,
                             rpc_min_interval_seconds=.26 if self.rpc.startswith("https:") else 0)
        self.artifact = json.loads(ARTIFACT.read_text())
        H.validate_build(API, self.artifact)
        if self.mainnet:
            scalar, constant = operator_fees(w3)
            if scalar or constant:
                raise ValueError("operator fees changed; update receipt accounting before running")
            self.evidence["operator_fee"] = {"scalar": scalar, "constant": constant}
        self.addresses = role_addresses(self.network)
        wanted = ("provider", "challenger") if args.deployment else ("provider", "challenger", "verifier")
        accounts = H.wallets(API, self.network, roles=wanted)   # mainnet: 0600 keystores, public-record checks
        for role, account in accounts.items():
            if account.address != self.addresses[role]:
                raise ValueError(f"{role} key differs from the recorded role address")
        self.evidence["actors"] = dict(self.addresses)
        self.nonces_before = {r: int(w3.eth.get_transaction_count(a, "latest")) for r, a in self.addresses.items()}
        for role, address in self.addresses.items():
            if int(w3.eth.get_transaction_count(address, "pending")) != self.nonces_before[role]:
                raise RuntimeError(f"{role} has a pending transaction; reconcile it first")
        self.balances_before = {r: int(w3.eth.get_balance(a)) for r, a in self.addresses.items()}
        self.evidence["balances_before_wei"] = self.balances_before
        self.evidence["nonces_before"] = self.nonces_before
        self.write_evidence()

        # Private provider store: fresh, authenticated, token only in the provider's processes.
        self.token = secrets.token_urlsafe(32)
        self.store_db = self.output / "provider-store.sqlite"
        store_env = {**self.base_env, "STORE_DB": str(self.store_db), "STORE_TOKEN": self.token}
        self.store_url = f"http://127.0.0.1:{args.store_port}"
        proc = stack.enter_context(H.child([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                                            "--port", str(args.store_port), "--no-access-log"],
                                           self.output / "store.log", store_env, ROOT / "packages/store"))
        self.services["store"] = proc
        H.ready(self.store_url, proc)
        if requests.get(self.store_url + "/inventory", timeout=5).status_code != 401:
            raise AssertionError("store accepts unauthenticated inventory reads")
        self.store = HttpStore(self.store_url, token=self.token)
        if self.store.inventory() != {"promise_count": 0, "record_count": 0}:
            raise AssertionError("a run needs a fresh dedicated store")

        # Deployment (verifier key, driver process) or verified reuse; then discard that key.
        deploy_gas, details = None, {}
        if args.deployment:
            deployment = verify_deployment(w3, args.deployment, self.network, self.addresses)
            details = {"reused_from": str(Path(args.deployment).resolve())}
            self.check_balances(deploy=False)
        else:
            CURRENT["case"] = "deploy"
            verifier = self.addresses["verifier"]
            ctor = w3.eth.contract(abi=self.artifact["abi"],
                                   bytecode=self.artifact["bytecode"]["object"]).constructor(verifier)
            deploy_gas = int(ctor.estimate_gas({"from": verifier}))
            self.check_balances(deploy=True, deploy_gas=deploy_gas,
                                init_code_bytes=len(ctor.data_in_transaction) // 2 - 1)
            guard = GuardedSigner(accounts.pop("verifier"), journal=self.journal, policy=signing_policy(
                w3, self.network, "driver", "verifier", init_code=ctor.data_in_transaction,
                fee_ceiling=self.fee_ceiling, case_label=lambda: CURRENT["case"]))
            started = time.perf_counter()
            deployed = EscrowClient.deploy(w3, str(ARTIFACT), verifier, verifier, {verifier: guard})
            receipt = deployed.deploy_receipt
            self.journal.observe(receipt, time.perf_counter() - started)
            del guard, deployed.accounts                # the driver keeps no verifier signer
            if not self.journal.canonicalize(w3, receipt.transactionHash, self.mainnet, wait=60):
                raise RuntimeError("deployment receipt did not become canonical; reconcile before continuing")
            expected = bytes.fromhex(self.artifact["deployedBytecode"]["object"].removeprefix("0x"))
            confirm_deployment(w3, receipt.contractAddress, int(receipt.blockNumber), expected, verifier)
            deployment = {"chain_id": self.profile["chain_id"], "address": receipt.contractAddress,
                          "deploy_block": int(receipt.blockNumber),
                          "artifact_sha256": self.evidence["artifact_sha256"],
                          **{f"{r}_addr": a for r, a in self.addresses.items()}}
            details = {"deploy_transaction": Web3.to_hex(receipt.transactionHash),
                       "runtime_sha256": hashlib.sha256(expected).hexdigest(), "source_revision": self.revision,
                       "source_dirty": bool(self.status), "deployed_at_utc": utc()}
        # deployment.json has exactly the measure_handoff keys (the checkpoint study validates
        # them); the services also need a listener-state identity, as in the September 25
        # handoff's service-deployment.json.
        self.deployment = {key: deployment[key] for key in DEPLOYMENT_KEYS}
        self.service_deployment = {**self.deployment, "deployment_id": uuid.uuid4().hex}
        H.dump(self.output / "deployment.json", self.deployment)
        H.dump(self.output / "service-deployment.json", self.service_deployment)
        self.evidence.update(deployment={**self.service_deployment, **details},
                             contract_address=deployment["address"], deployment_estimate_gas=deploy_gas)
        runtime = bytes.fromhex(self.artifact["deployedBytecode"]["object"].removeprefix("0x"))
        wait_until_visible(w3, deployment["address"], runtime)
        self.contract_before = int(w3.eth.get_balance(deployment["address"]))
        provider, challenger = self.addresses["provider"], self.addresses["challenger"]
        signers = {role: GuardedSigner(accounts[role], journal=self.journal, policy=signing_policy(
            w3, self.network, "driver", role, target=deployment["address"], fee_ceiling=self.fee_ceiling,
            case_label=lambda: CURRENT["case"])) for role in ("provider", "challenger")}
        self.escrow = EscrowClient(w3, deployment["address"], self.artifact["abi"],
                                   {provider: signers["provider"], challenger: signers["challenger"]})
        self.escrow.deploy_block = deployment["deploy_block"]
        self.escrow._observe_receipt(SimpleNamespace(blockNumber=deployment["deploy_block"]))
        self.first_challenge_id = (int(self.escrow.contract.functions.nextChallengeId().call())
                                   if args.deployment else 1)

        # Services: separate processes, own keys only; the verifier never sees store credentials.
        service_env = {**self.base_env, "AA_LOCAL": "0" if self.mainnet else "1",
                       "AA_CHAIN_ID": str(self.profile["chain_id"]), "AA_RPC_URL": self.rpc,
                       "AA_DEPLOYMENT": str(self.output / "service-deployment.json")}
        if self.mainnet:
            service_env["AA_KEYSTORE_DIR"] = str(KEYSTORE_DIR)
        command = [sys.executable, str(Path(__file__).resolve()), "service", "--output", str(self.output),
                   "--network", self.network, "--fee-ceiling-wei", str(self.fee_ceiling)]
        if self.mainnet:
            command.append("--execute-mainnet")
        self.evidence_url = f"http://127.0.0.1:{args.evidence_port}"
        self.inbox_path = self.output / "verifier-inbox.sqlite"
        verifier_env = {**service_env, "EVIDENCE_PORT": str(args.evidence_port), "EVIDENCE_DB": str(self.inbox_path)}
        proc = stack.enter_context(H.child(command + ["--role", "verifier"], self.output / "verifier-service.log",
                                           verifier_env))
        self.services["verifier-service"] = proc
        self.wait_ready("verifier", proc, self.evidence_url)
        if requests.get(self.evidence_url + "/claims/1/evidence", timeout=5).status_code not in (404, 405):
            raise AssertionError("the receiver exposes stored evidence")
        provider_env = {**service_env, "STORE_URL": self.store_url, "STORE_TOKEN": self.token,
                        "EVIDENCE_URL": self.evidence_url}
        proc = stack.enter_context(H.child(command + ["--role", "provider"], self.output / "provider-service.log",
                                           provider_env))
        self.services["provider-service"] = proc
        self.wait_ready("provider", proc)
        self.evidence["services"] = {name: {"pid": p.pid} for name, p in self.services.items()}
        self.write_evidence()
        self.native = NativeHermes(args.hermes_root, self.output / "fixtures")
        self.evidence["native_modules"] = self.native.sources
        self.evidence["module_sources"] = {m.__name__: str(Path(m.__file__).resolve().relative_to(ROOT))
                                           for m in (sys.modules["aa_sdk"], sys.modules["aa_sdk.chain"],
                                                     sys.modules["aa_sdk.evidence"], sys.modules["aa_commons"],
                                                     sys.modules[self.native.plugin.__name__])}

    def wait_ready(self, role, proc, url=None):
        """A service is ready once its verified escrow client exists (and its receiver answers)."""
        log = self.output / f"{role}-service.log"
        deadline = time.monotonic() + (180 if self.mainnet else 60)
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"{role} service exited; inspect {log.name}")
            ready = log.exists() and f'"ready": "{role}"' in log.read_text(errors="replace")
            if ready and url:
                try:
                    ready = requests.get(url + "/health", timeout=2).status_code == 200
                except requests.RequestException:
                    ready = False
            if ready:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{role} service did not become ready; inspect {log.name}")
            time.sleep(.2)

    def check_balances(self, deploy, deploy_gas=None, init_code_bytes=None):
        plan = plan_costs(self.w3, self.network, deploy, deploy_gas=deploy_gas, init_code_bytes=init_code_bytes)
        self.evidence["cost_plan"] = plan
        if plan["peak_committed_fee_wei"] > self.fee_ceiling:
            raise ValueError("projected peak committed fees exceed the fee ceiling")
        for role, need in plan["roles"].items():
            if self.balances_before[role] < need["required_balance_wei"]:
                raise ValueError(f"{role} balance does not cover its projected fees and values")

    # ── one session and its claims ──
    def session(self, spec):
        name, native, store, escrow = spec["name"], self.native, self.store, self.escrow
        provider, challenger = self.addresses["provider"], self.addresses["challenger"]
        CURRENT["case"] = name
        acc, session = native.begin(name, store, escrow, provider, challenger, spec["cadence"])
        sid = session.session_id
        effect = native.write(name, 0, outside=spec["outside"], decision=spec["decision"])
        auths = [r for r in session.records if r.tool == "user_authorization"]
        if spec["decision"] is None:
            assert not auths, "native write unexpectedly carried an approval"
        else:
            assert len(auths) == 1 and auths[0].args["decision"] == "allow", auths
        assert len(session.records) == spec["records"], [r.tool for r in session.records]
        checkpoint = session.checkpoint(force=True)
        assert checkpoint["success"] and checkpoint["submitted"], checkpoint
        original = store.get_records(sid)
        fault, summary = None, None
        if spec["kind"] == "checkpoint_rewrite":
            content = "controlled changed prefix"
            before, after = rewrite_store_record(self.store_db, sid, spec["records"], content)
            served = store.get_records(sid)
            assert served[-1]["args"]["content"] == content and served[:-1] == original[:-1]
            try:
                native.end(name)
            except ValueError as error:
                refusal = str(error)
            else:
                raise AssertionError("the SDK finalized store records that contradict its checkpoint")
            assert "checkpoint prefix mismatch" in refusal, refusal
            session._stop.set()
            session._wake.set()
            if session._worker:
                session._worker.join(timeout=5)
                assert not session._worker.is_alive()
            committed = trace_hash([ActionRecord.from_dict(r) for r in served])
            # The tampering provider bypasses its own SDK and commits the store's hash directly.
            escrow.commit_trace(provider, sid, committed)
            summary = {"session_id": sid, "trace_hash": committed, "n_actions": len(served)}
            fault = {"kind": spec["kind"], "rewritten_seq": spec["records"], "record_before": before,
                     "record_after": after, "sdk_finalize_refusal": refusal,
                     "final_commitment": "provider key, EscrowClient.commit_trace of the rewritten store's hash"}
        else:
            summary = native.end(name)
            assert summary is not None and summary["n_actions"] == spec["records"], summary
            if session._worker:
                session._worker.join(timeout=5)
                assert not session._worker.is_alive()
            if spec["kind"] == "altered_after_final":
                content = "controlled changed after final commitment"
                before, after = rewrite_store_record(self.store_db, sid, spec["records"], content)
                served = store.get_records(sid)
                assert served[-1]["args"]["content"] == content and served[:-1] == original[:-1]
                fault = {"kind": spec["kind"], "rewritten_seq": spec["records"], "record_before": before,
                         "record_after": after}
        snap = snapshot(escrow, store, session)
        parsed = [ActionRecord.from_dict(r) for r in snap["records"]]
        honest = [ActionRecord.from_dict(r) for r in original]
        checkpoints = snap["checkpoints"]
        checks = {
            "one_checkpoint_of_all_records": len(checkpoints) == 1 and checkpoints[0]["record_count"] == spec["records"],
            "checkpoint_is_original_prefix": checkpoints[0]["prefix_hash"] == trace_hash(honest),
            "summary_matches_chain": summary["trace_hash"] == snap["final_trace_hash"],
            "effect_on_disk": hashlib.sha256(Path(effect["path"]).read_bytes()).hexdigest() == effect["effect_sha256"],
            "effect_location": Path(effect["path"]).parent.name == ("outside" if spec["outside"] else "workspace"),
        }
        prefix_error = None
        try:
            check_prefixes(parsed, checkpoints, sid)
        except ValueError as error:
            prefix_error = str(error)
        if spec["kind"] in ("within", "outside", "without_approval"):
            checks["store_is_committed_trace"] = snap["final_trace_hash"] == trace_hash(parsed) and snap["records"] == original
            checks["checkpoints_valid"] = prefix_error is None
        elif spec["kind"] == "altered_after_final":
            checks["final_commits_original"] = snap["final_trace_hash"] == trace_hash(honest)
            checks["store_differs_from_final"] = trace_hash(parsed) != snap["final_trace_hash"]
        else:
            checks["final_commits_rewritten_store"] = snap["final_trace_hash"] == trace_hash(parsed) != trace_hash(honest)
            checks["store_contradicts_checkpoint"] = (prefix_error or "").startswith("checkpoint prefix mismatch")
        assert all(checks.values()), checks
        self_check = {str(pid): jsonable(verdict.__dict__) for pid, verdict in acc.self_check(honest).items()}
        record = {"acc": acc, "session": session, "effect": effect, "snapshot": snap, "original_records": original,
                  "fault": fault, "summary": summary, "session_checks": checks, "self_check": self_check,
                  "prefix_error": prefix_error}
        if spec["kind"] in ("within", "outside"):
            verdict = acc.self_check(session.records)[acc.promises[1].promise_id]
            record["authorization_verdict"] = jsonable(verdict.__dict__)
            assert verdict.violated is False
        self.sessions[name] = record
        return record

    def settle(self, spec, record, name, promise_index, key):
        escrow, w3 = self.escrow, self.w3
        provider, challenger = self.addresses["provider"], self.addresses["challenger"]
        acc, session = record["acc"], record["session"]
        sid, pid = session.session_id, acc.promises[promise_index].promise_id
        expected = EXPECTED[key]
        CURRENT["case"] = name
        started, search_from = time.time(), int(w3.eth.block_number)
        cid = escrow.challenge(challenger, sid, pid, BOND)
        timeout = self.args.verdict_timeout or (900 if self.mainnet else 240)
        filed = wait_for_event(escrow.contract.events.Challenged, cid, search_from, 60, self.services, 1)
        b0 = int(filed["blockNumber"])
        state = dict(session_id=sid, promise_id=pid, challenge_id=cid, provider=provider, challenger=challenger)
        before = claim_state(escrow, b0, **state)
        event = wait_for_event(escrow.contract.events.Verdict, cid, b0, timeout, self.services,
                               2 if self.mainnet else .5)
        observed = time.time()
        b1 = int(event["blockNumber"])
        after = claim_state(escrow, b1, **state)
        violated, paid = bool(event["args"]["violated"]), int(event["args"]["paidToChallenger"])
        results = wait_for_line(self.output / "verifier-results.jsonl", cid, lambda r: "violated" in r, 120)
        deliveries = [d for d in read_jsonl(self.output / "provider-deliveries.jsonl")
                      if d.get("challenge_id") == cid and d["outcome"] == "delivered"]
        unresolved = self.journal.reconcile(w3, self.mainnet)
        if unresolved:
            raise RuntimeError(f"unresolved transactions after claim {cid}: {unresolved}")
        rows = self.journal.rows()
        verdict_rows = [r for r in rows if r["operation"] == "submitVerdict"
                        and r["hash"].lower() == Web3.to_hex(event["transactionHash"]).lower()]
        challenge_rows = [r for r in rows if r["hash"].lower() == Web3.to_hex(filed["transactionHash"]).lower()]
        evidence, closed = inbox_contents(self.inbox_path)
        inbox = evidence.get(cid)
        expected_payload = {"version": EVIDENCE_VERSION, "chain_id": self.profile["chain_id"],
                            "escrow_address": escrow.contract.address.lower(), "challenge_id": cid,
                            "session_id": sid, "promise_id": pid, "records": self.store.get_records(sid),
                            "params": self.store.get_promise(pid)["params"]}
        result = results[0] if results else {}
        reason = str(result.get("reason", ""))
        if key == "checkpoint_rewrite":
            reason_ok = result.get("predicate") is None and "checkpoint prefix mismatch" in reason
        elif key == "altered_after_final":
            reason_ok = result.get("predicate") is None and "records hash != on-chain traceHash" in reason
        else:
            reason_ok = result.get("predicate") == PREDICATE[promise_index]
        effect = record["effect"]
        checks = {
            "verdict_matches_expectation": violated is expected,
            "verifier_result_matches_event": len(results) == 1 and result.get("violated") is violated
                                             and result.get("paid_to_challenger") == paid,
            "verifier_reason": reason_ok,
            "paid_amount": paid == (PAYOUT + BOND if expected else 0),
            "challenger_balance_delta": after["challenger_balance_wei"] - before["challenger_balance_wei"] == paid,
            "reserve_delta": before["reserve_wei"] - after["reserve_wei"] == (PAYOUT if expected else 0),
            "provider_bond_credit_delta": after["provider_bond_credit_wei"] - before["provider_bond_credit_wei"]
                                          == (0 if expected else BOND),
            "contract_balance_delta": before["contract_balance_wei"] - after["contract_balance_wei"] == paid,
            "challenge_status": int(before["challenge"][4]) == 1 and int(after["challenge"][4]) == (2 if expected else 3),
            "pair_resolved": after["pair_resolved"] and not after["pair_challenged"] and after["open_challenges"] == 0,
            "delivered_once": len(deliveries) == 1,
            "inbox_matches_delivery": inbox is not None and len(deliveries) == 1
                                      and inbox["evidence_hash"] == deliveries[0]["evidence_hash"]
                                      == deliveries[0]["receipt"]["evidence_hash"],
            "inbox_is_store_evidence": inbox is not None and canonical_bytes(inbox["payload"]) == canonical_bytes(expected_payload)
                                       and inbox["evidence_hash"] == evidence_hash(expected_payload),
            "inbox_signed_by_provider": inbox is not None and inbox["provider"].lower() == provider.lower()
                                        and recover_provider({"payload": inbox["payload"], "signature": inbox["signature"]}) == provider,
            "inbox_before_deadline": inbox is not None and inbox["received_at"] <= int(before["challenge"][5]) + EVIDENCE_WINDOW,
            "not_closed_without_evidence": cid not in closed,
            "verdict_sent_by_verifier_service": len(verdict_rows) == 1 and verdict_rows[0]["process"] == "verifier-service"
                                                and verdict_rows[0]["status"] == "canonical",
            "challenge_sent_by_driver": len(challenge_rows) == 1 and challenge_rows[0]["process"] == "driver"
                                        and challenge_rows[0]["role"] == "challenger",
            "native_effect_intact": hashlib.sha256(Path(effect["path"]).read_bytes()).hexdigest() == effect["effect_sha256"],
        }
        snap = record["snapshot"]
        row = {"name": name, "case": key, "table_row": LABELS[key], "expected_violated": expected,
               "session": spec["name"], "session_id": sid, "challenge_id": cid, "promise_id": pid,
               "promise_index": promise_index,
               "verdict": {**result, "event": {"violated": violated, "paid_to_challenger": paid, "block": b1,
                                               "transaction": Web3.to_hex(event["transactionHash"])}},
               "verifier_process": result.get("pid"),
               "reserve_before_wei": before["reserve_wei"], "reserve_after_wei": after["reserve_wei"],
               "challenger_balance_delta_wei": after["challenger_balance_wei"] - before["challenger_balance_wei"],
               "provider_credit_delta_wei": after["provider_bond_credit_wei"] - before["provider_bond_credit_wei"],
               "state_before": before, "state_after": after,
               "promise": self.store.get_promise(pid), **snap,
               "original_records": record["original_records"] if record["fault"] else None,
               "tool_effect": effect, "self_check": record["self_check"], "fault": record["fault"],
               "session_checks": record["session_checks"],
               "delivery": deliveries[0] if deliveries else None,
               "inbox": {k: v for k, v in (inbox or {}).items() if k != "payload"} | {
                   "record_count": len((inbox or {}).get("payload", {}).get("records", []))},
               "challenge_transaction": Web3.to_hex(filed["transactionHash"]),
               "timing": {"challenge_block": b0, "verdict_block": b1, "challenge_to_verdict_observed_seconds": observed - started,
                          "evidence_received_at": (inbox or {}).get("received_at"),
                          "challenged_at": int(before["challenge"][5])},
               "checks": checks, "passed": all(checks.values())}
        if "authorization_verdict" in record and promise_index == 0:
            row["authorization_verdict"] = record["authorization_verdict"]
        self.cases.append(row)
        H.dump(self.output / "cases" / f"{name}.json", row)
        self.write_evidence()
        print(json.dumps({"case": name, "challenge_id": cid, "violated": violated, "expected": expected,
                          "passed": row["passed"]}), flush=True)
        if not row["passed"]:
            raise AssertionError(f"{name} failed checks: {[k for k, v in checks.items() if not v]}")
        return row

    def finish(self):
        w3, escrow, rows = self.w3, self.escrow, None
        unresolved = self.journal.reconcile(w3, self.mainnet, wait=60)
        if unresolved:
            raise RuntimeError(f"unresolved transactions: {unresolved}")
        rows = self.journal.rows()
        final_block = max(r["block_number"] for r in rows)
        after = read_at(final_block, lambda b: {r: int(w3.eth.get_balance(a, b)) for r, a in self.addresses.items()})
        contract_after = read_at(final_block, lambda b: int(w3.eth.get_balance(escrow.contract.address, b)))
        nonces_after = read_at(final_block, lambda b: {r: int(w3.eth.get_transaction_count(a, b))
                                                        for r, a in self.addresses.items()})
        by_sender = Counter(r["sender"] for r in rows)
        fees = {r: sum(x["fee_wei"] for x in rows if x["sender"] == a) for r, a in self.addresses.items()}
        values = {r: sum(x["value_wei"] for x in rows if x["sender"] == a) for r, a in self.addresses.items()}
        paid = sum(c["verdict"]["event"]["paid_to_challenger"] for c in self.cases)
        received = {"provider": 0, "verifier": 0, "challenger": paid}
        evidence, closed = inbox_contents(self.inbox_path)
        claim_ids = sorted(c["challenge_id"] for c in self.cases)
        verdict_hashes = {c["verdict"]["event"]["transaction"].lower() for c in self.cases}
        cursor_dir = self.output / "runtime"
        repo_runtime = ROOT / "_runtime" / str(self.profile["chain_id"])
        drift = sorted(name for name, digest in source_hashes().items() if self.hashes.get(name) != digest)
        drift += sorted(set(self.hashes) - set(source_hashes()))
        operations = Counter(r["operation"] for r in rows)
        violated = sum(c["verdict"]["event"]["violated"] for c in self.cases)
        checks = {
            "all_cases_passed": len(self.cases) == 14 and all(c["passed"] for c in self.cases),
            "sessions": len({c["session_id"] for c in self.cases}) == 11,
            "violations_and_rejections": violated == 8 and len(self.cases) - violated == 6,
            "all_transactions_canonical_success": all(r["status"] == "canonical" for r in rows),
            "operations_match_plan": dict(operations) == planned_operations(not self.args.deployment),
            "nonce_deltas_match_journal": all(nonces_after[r] - self.nonces_before[r] == by_sender.get(a, 0)
                                              for r, a in self.addresses.items()),
            "verdicts_from_verifier_service": verdict_hashes == {r["hash"].lower() for r in rows
                                                                  if r["operation"] == "submitVerdict"
                                                                  and r["process"] == "verifier-service"},
            "balance_conservation": all(self.balances_before[r] - after[r] == fees[r] + values[r] - received[r]
                                        for r in self.addresses),
            "contract_balance": contract_after - self.contract_before
                                == values["provider"] + values["challenger"] - paid,
            "fresh_contract_holds_14p_plus_6b": bool(self.args.deployment) or contract_after == 14 * PAYOUT + 6 * BOND,
            "inbox_one_row_per_claim": sorted(evidence) == claim_ids and not closed,
            "claims_contiguous_from_run_start": claim_ids == list(range(self.first_challenge_id,
                                                                        self.first_challenge_id + 14)),
            "cursors_in_output": sorted(p.name.split("-")[0] for p in cursor_dir.glob("*.cursor")) == ["provider", "verifier"]
                                 and not any(self.service_deployment["deployment_id"] in p.name
                                             for p in repo_runtime.glob("*")),
            "total_fee_within_ceiling": sum(fees.values()) <= self.fee_ceiling,
            "source_unchanged": not drift,
        }
        op_summary = {}
        for op in sorted(operations):
            group = [r for r in rows if r["operation"] == op]
            op_summary[op] = {"transactions": len(group), "total_fee_wei": sum(r["fee_wei"] for r in group),
                              "mean_fee_microeth": sum(r["fee_wei"] for r in group) / len(group) / 1e12,
                              "gas_used": [min(r["gas_used"] for r in group), max(r["gas_used"] for r in group)],
                              "gas_limit_max": max(r["gas_limit"] for r in group)}
        self.evidence.update(
            balances_after_wei=after, nonces_after=nonces_after, contract_balance_before_wei=self.contract_before,
            contract_balance_wei=contract_after, fees_by_role_wei=fees, values_by_role_wei=values,
            paid_to_challenger_wei=paid, total_fee_wei=sum(fees.values()), operations=op_summary,
            # Chained into the checkpoint study's --prior-fees-wei: every signed transaction,
            # deployment included, from canonical receipts (L2 execution + L1 data fee).
            all_transaction_fee_wei=sum(r["fee_wei"] for r in rows), all_transaction_count=len(rows),
            transaction_count=len(rows), final_checks=checks, source_drift=drift,
            outcome_table=[{"case": label, "challenges": sum(c["case"] == key for c in self.cases),
                            "expected": "violation" if v else "no violation",
                            "observed": sorted({"violation" if c["verdict"]["event"]["violated"] else "no violation"
                                                for c in self.cases if c["case"] == key})}
                           for key, label, n, v in TABLE],
            wall_seconds=time.time() - self.started, finished_at_utc=utc(),
            status="passed" if all(checks.values()) else "failed_final_checks")
        self.write_evidence()
        H.dump(self.output / "summary.json", {key: self.evidence[key] for key in (
            "status", "network", "chain_id", "contract_address", "all_transaction_fee_wei",
            "all_transaction_count", "fees_by_role_wei", "operations", "outcome_table", "final_checks",
            "balances_before_wei", "balances_after_wei", "wall_seconds", "source_revision", "source_dirty")})
        if not all(checks.values()):
            raise AssertionError(f"final checks failed: {[k for k, v in checks.items() if not v]}")

    def execute(self):
        self.preconditions()
        with ExitStack() as stack:
            try:
                self.start(stack)
                for spec in session_plan():
                    record = self.session(spec)
                    for name, promise_index, key in spec["challenges"]:
                        self.settle(spec, record, name, promise_index, key)
                self.finish()
            except BaseException as error:
                if hasattr(self, "evidence"):
                    self.evidence.update(status="failed", error=f"{type(error).__name__}: {error}",
                                         finished_at_utc=utc())
                    self.write_evidence()
                raise
            finally:
                if hasattr(self, "native"):
                    self.native.loop.call_soon_threadsafe(self.native.loop.stop)
                    self.native.thread.join(timeout=5)
        print(json.dumps({"status": self.evidence["status"], "output": str(self.output),
                          "transactions": self.evidence["transaction_count"],
                          "total_fee_wei": self.evidence["total_fee_wei"],
                          "wall_seconds": round(self.evidence["wall_seconds"], 1)}), flush=True)


def preflight(args):
    """Read-only: chain, fees, deployment gas, role balances/nonces and the projected run cost."""
    rpc = args.rpc_url or H.RPC
    w3 = H.connection(API, rpc)
    if int(w3.eth.chain_id) != 8453:
        raise ValueError("preflight is for Base mainnet (8453)")
    artifact = json.loads(ARTIFACT.read_text())
    H.validate_build(API, artifact)
    addresses = role_addresses("mainnet")
    funding = json.loads(H.PUBLIC.read_text())["funding_address"]
    keystore = {}
    for role in addresses:
        files = [KEYSTORE_DIR / f"{role}.json", KEYSTORE_DIR / f"{role}.password"]
        keystore[role] = all(p.is_file() and not p.stat().st_mode & 0o077 for p in files)   # stat only
    scalar, constant = operator_fees(w3)
    deploy = not args.deployment
    report = {"at_utc": utc(), "chain_id": 8453, "rpc_url": rpc, "block": int(w3.eth.block_number),
              "artifact_sha256": hashlib.sha256(ARTIFACT.read_bytes()).hexdigest(),
              "operator_fee": {"scalar": scalar, "constant": constant, "zero": not (scalar or constant)},
              "eth_gas_price_wei": int(w3.eth.gas_price), "fee_ceiling_wei": args.fee_ceiling_wei or PROFILES["mainnet"]["fee_ceiling"],
              "keystore_dir": str(KEYSTORE_DIR), "keystore_files_private": keystore}
    deploy_gas = init_bytes = None
    if deploy:
        ctor = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"]).constructor(addresses["verifier"])
        deploy_gas = int(ctor.estimate_gas({"from": addresses["verifier"]}))
        init_bytes = len(ctor.data_in_transaction) // 2 - 1
        report["deployment"] = {"needed": True, "estimate_gas": deploy_gas, "within_gas_limit": deploy_gas <= MAX_GAS,
                                "init_code_bytes": init_bytes}
    else:
        deployment = verify_deployment(w3, args.deployment, "mainnet", addresses)
        report["deployment"] = {"needed": False, "reuse": str(Path(args.deployment).resolve()),
                                "address": deployment["address"], "verified": True}
    plan = plan_costs(w3, "mainnet", deploy, deploy_gas=deploy_gas, init_code_bytes=init_bytes)
    report["plan"] = plan
    report["sdk_max_fee_within_limit"] = plan["sdk_max_fee_per_gas_wei"] <= PROFILES["mainnet"]["max_fee_per_gas"]
    report["peak_within_fee_ceiling"] = plan["peak_committed_fee_wei"] <= report["fee_ceiling_wei"]
    roles, shortfall = {}, 0
    for role, address in addresses.items():
        balance = int(w3.eth.get_balance(address))
        latest, pending = int(w3.eth.get_transaction_count(address, "latest")), int(w3.eth.get_transaction_count(address, "pending"))
        cost = plan["roles"][role]
        need = cost["required_balance_wei"]
        roles[role] = {"address": address, "balance_wei": balance, "balance_microeth": balance / 1e12,
                       "nonce_latest": latest, "nonce_pending": pending, "no_pending": latest == pending,
                       "planned_transactions": cost["transactions"],
                       "expected_fee_wei": cost["expected_fee_wei"], "value_sent_wei": cost["value_wei"],
                       "value_returned_wei": cost["returned_value_wei"],
                       "projected_spend_wei": cost["projected_spend_wei"],
                       "projected_spend_microeth": cost["projected_spend_wei"] / 1e12,
                       "remaining_after_run_wei": balance - cost["projected_spend_wei"],
                       "remaining_after_run_microeth": (balance - cost["projected_spend_wei"]) / 1e12,
                       "remaining_after_conservative_spend_microeth":
                           (balance - cost["projected_spend_conservative_wei"]) / 1e12,
                       "required_balance_wei": need, "covered": balance >= need,
                       "top_up_needed_wei": max(0, need - balance)}
        shortfall += roles[role]["top_up_needed_wei"]
    report["roles"] = roles
    # The checkpoint studies that follow want the provider at >= 150 microETH before Hermes.
    target = 150 * 10**12
    remaining = roles["provider"]["remaining_after_run_wei"]
    report["checkpoint_study_provider_target"] = {
        "target_wei": target, "provider_remaining_after_run_wei": remaining,
        "meets_target": remaining >= target, "top_up_to_target_wei": max(0, target - remaining)}
    report["funding_wallet"] = {"address": funding, "balance_wei": int(w3.eth.get_balance(funding)),
                                "note": "read only; this runner never transfers funds"}
    report["funding_needed_wei"] = shortfall
    report["ready"] = (report["operator_fee"]["zero"] and report["sdk_max_fee_within_limit"]
                       and report["peak_within_fee_ceiling"] and all(keystore.values())
                       and all(r["covered"] and r["no_pending"] for r in roles.values())
                       and (not deploy or report["deployment"]["within_gas_limit"]))
    if args.output:
        path = Path(args.output)
        if path.exists():
            raise ValueError("preflight output exists; choose a new path")
        H.dump(path, report)
    print(json.dumps(report, indent=2, default=str))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    run_parser = sub.add_parser("run", help="run the 14-challenge matrix")
    run_parser.add_argument("--network", choices=("local", "mainnet"), default="local")
    run_parser.add_argument("--output", type=Path, help=f"fresh directory (mainnet default {DEFAULT_MAINNET_OUTPUT})")
    run_parser.add_argument("--execute-mainnet", action="store_true")
    run_parser.add_argument("--deployment", type=Path, help="reuse a verified V2 deployment.json")
    run_parser.add_argument("--rpc-url", help="mainnet: RPC endpoint (paced); local: attach to a running Anvil")
    run_parser.add_argument("--hermes-root", type=Path, default=HERMES_ROOT)
    run_parser.add_argument("--fee-ceiling-wei", type=int)
    run_parser.add_argument("--verdict-timeout", type=float)
    run_parser.add_argument("--allow-dirty", action="store_true", help="local only")
    run_parser.add_argument("--anvil-port", type=int, default=18541)
    run_parser.add_argument("--store-port", type=int, default=18542)
    run_parser.add_argument("--evidence-port", type=int, default=18543)
    pre = sub.add_parser("preflight", help="read-only Base mainnet readiness and cost projection")
    pre.add_argument("--deployment", type=Path)
    pre.add_argument("--rpc-url")
    pre.add_argument("--fee-ceiling-wei", type=int)
    pre.add_argument("--output", type=Path, help="optional new file for the report")
    svc = sub.add_parser("service", help="internal: run a protocol service inside the experiment guard")
    svc.add_argument("--role", choices=("provider", "verifier"), required=True)
    svc.add_argument("--output", type=Path, required=True)
    svc.add_argument("--network", choices=("local", "mainnet"), required=True)
    svc.add_argument("--fee-ceiling-wei", type=int, required=True)
    svc.add_argument("--execute-mainnet", action="store_true")
    args = parser.parse_args()
    if args.mode == "run":
        Run(args).execute()
    elif args.mode == "preflight":
        preflight(args)
    else:
        service(args)


if __name__ == "__main__":
    main()
