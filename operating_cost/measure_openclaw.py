"""Native OpenClaw cost measurements; real plugin/helper/store, no model calls.

The helper wrapper changes only experiment configuration and adds read-only evidence
endpoints. Protocol code is imported unchanged from this checkout. Mainnet sends are
restricted to the existing experiment escrow and a separately funded provider wallet.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import random
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
for package in ("commons", "sdk", "store", "verifier"):
    sys.path.insert(0, str(ROOT / "packages" / package))
import requests
from eth_account import Account
from web3 import Web3
from aa_commons import ActionRecord, trace_hash
from aa_commons.trace import check_prefixes
from aa_sdk.chain import EscrowClient
import native_pin  # operating_cost/native_pin.py

# Public September 16 inputs (funding.json, mainnet04/evidence.json reused_deployment), read by
# public_metadata() on every run, including local ones.
ARCHIVE = Path(__file__).resolve().parent / "fixtures"
ARTIFACT = ROOT / "contracts/out/Escrow.sol/Escrow.json"
# Mainnet only: the pinned Escrow.json of the reused deployment, with its Escrow.sol alongside
# (unset, a nonexistent path).
DEPLOYED_ARTIFACT = Path(os.environ.get("AA_MAINNET_DEPLOYED_ARTIFACT") or "AA_MAINNET_DEPLOYED_ARTIFACT-unset")
NODE = Path(os.environ.get("AA_NODE") or shutil.which("node") or "node")
OPENCLAW = ROOT.parent / "openclaw"
RPC = "https://mainnet.base.org"
PAYOUT = 10**10
PROVIDER_ALLOWANCE = 3 * 10**14
MAX_GAS_PRICE = 10**8
STRATEGIES = {"10_records_or_30s": (10, 30), "30_records_or_60s": (30, 60),
              "final_only": (10**9, 86400)}


def utc():
    return datetime.now(timezone.utc).isoformat()


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.replace(path)


def jsonable(value):
    return json.loads(Web3.to_json(value))


class PacedHTTPProvider(Web3.HTTPProvider):
    """Same 0.25 second public-RPC pacing as the Hermes measurement."""
    def __init__(self, url, interval=.25):
        super().__init__(url, request_kwargs={"timeout": 30}, exception_retry_configuration=None)
        self.interval, self.lock, self.last = interval, threading.Lock(), 0.0

    def make_request(self, method, params):
        with self.lock:
            for attempt in range(5):
                time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
                self.last = time.monotonic()
                try:
                    return super().make_request(method, params)
                except requests.HTTPError as error:
                    if error.response is None or error.response.status_code != 429 or attempt == 4:
                        raise
                    time.sleep(2 ** (attempt + 1))


def mainnet():
    w3 = Web3(PacedHTTPProvider(RPC))
    if w3.eth.chain_id != 8453:
        raise ValueError("wrong chain; expected Base mainnet 8453")
    return w3


def private_account(key_path, password_path, expected):
    key_path, password_path = Path(key_path), Path(password_path)
    for path in (key_path, password_path):
        if path.stat().st_mode & 0o077:
            raise ValueError("private key files must have private permissions")
    account = Account.from_key(Account.decrypt(json.loads(key_path.read_text()), password_path.read_text().strip()))
    if account.address != Web3.to_checksum_address(expected):
        raise ValueError("signing key differs from recorded public address")
    return account


def public_metadata():
    funding = json.loads((ARCHIVE / "funding.json").read_text())
    evidence = json.loads((ARCHIVE / "mainnet04/evidence.json").read_text())
    return funding, evidence["reused_deployment"]


def validate_existing(w3):
    funding, deployment = public_metadata()
    artifact_bytes = DEPLOYED_ARTIFACT.read_bytes()
    artifact = json.loads(artifact_bytes)
    if hashlib.sha256(artifact_bytes).hexdigest() != deployment["artifact_sha256"]:
        raise ValueError("compiled artifact differs from existing experiment deployment")
    code = bytes.fromhex(artifact["deployedBytecode"]["object"].removeprefix("0x"))
    if bytes(w3.eth.get_code(deployment["address"])) != code:
        raise ValueError("live contract bytecode differs from expected experiment escrow")
    contract = w3.eth.contract(address=deployment["address"], abi=artifact["abi"])
    operator = funding["role_addresses"]["verifier"]
    if contract.functions.owner().call() != operator or contract.functions.verifier().call() != operator:
        raise ValueError("live contract operator mismatch")
    return funding, deployment, artifact


class Journal:
    def __init__(self, path, initial):
        self.path, self.data, self.lock = Path(path), initial, threading.RLock()
        self.data.setdefault("transactions", [])
        self.data.setdefault("signed_transactions", [])
        self.write()

    def write(self):
        with self.lock:
            dump(self.path, self.data)


class LimitedSigner:
    def __init__(self, account, contract, journal):
        self.account, self.address = account, account.address
        self.contract, self.journal = Web3.to_checksum_address(contract), journal

    def sign_transaction(self, transaction):
        if transaction.get("chainId") != 8453:
            raise ValueError("refusing wrong-chain transaction")
        if Web3.to_checksum_address(transaction["to"]) != self.contract:
            raise ValueError("refusing transaction outside the experiment escrow")
        if not 0 < int(transaction["gas"]) <= 350000:
            raise ValueError("transaction exceeds gas ceiling")
        price = int(transaction.get("maxFeePerGas", transaction.get("gasPrice", 0)))
        if not 0 < price <= MAX_GAS_PRICE or int(transaction.get("value", 0)) > PAYOUT:
            raise ValueError("transaction exceeds fee or reserve ceiling")
        with self.journal.lock:
            if len(self.journal.data["signed_transactions"]) >= 160:
                raise ValueError("experiment transaction limit reached")
            signed = self.account.sign_transaction(transaction)
            self.journal.data["signed_transactions"].append({
                "hash": Web3.to_hex(Web3.keccak(signed.raw_transaction)),
                "sender": self.address, "nonce": transaction["nonce"],
                "to": transaction["to"], "value_wei": transaction.get("value", 0),
                "gas_limit": transaction["gas"], "max_fee_per_gas": price,
                "signed_at_utc": utc()})
            self.journal.write()
        return signed


class MeasuredChain(EscrowClient):
    def _send(self, fn, sender, value=0):
        operation = fn.fn_name
        if operation not in {"registerPromise", "openSession", "checkpointTrace", "commitTrace"}:
            raise ValueError("operation outside the cost experiment")
        started, wall_started = time.perf_counter(), time.time()
        receipt = super()._send(fn, sender, value)
        observed = time.time()
        args = list(fn.args)
        row = {"operation": operation, "sender": sender, "value_wei": value,
               "session_id": Web3.to_hex(args[0]) if operation != "registerPromise" else None,
               "record_count": int(args[1]) if operation == "checkpointTrace" else None,
               "started_at_unix": wall_started, "observed_at_unix": observed,
               "seconds": time.perf_counter() - started, "receipt": jsonable(receipt)}
        with self.journal.lock:
            self.journal.data["transactions"].append(row)
            self.journal.write()
        return receipt


def serve(args):
    import uvicorn
    sys.path.insert(0, str(ROOT / "integrations/openclaw"))
    import aa_helper.app as helper
    import aa_openclaw
    from fastapi import HTTPException
    from aa_sdk import HttpStore

    out = args.output
    state = {"mode": "http_store", "checkpoint_records": 10, "checkpoint_seconds": 30}
    journal = Journal(out / "chain-journal.json", {"started_at_utc": utc(), "network": args.network,
                     "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()})
    if args.network == "mainnet":
        w3 = mainnet()
        funding, deployment, artifact = validate_existing(w3)
        provider = funding["role_addresses"]["provider"]
        account = private_account(args.keystore_dir / "provider.json", args.keystore_dir / "provider.password", provider)
        signer = LimitedSigner(account, deployment["address"], journal)
        chain = MeasuredChain(w3, deployment["address"], artifact["abi"], {provider: signer})
        journal.data.update(contract_address=deployment["address"], chain_id=8453,
                            provider=provider, provider_balance_before_wei=w3.eth.get_balance(provider))
    else:
        w3 = Web3(Web3.HTTPProvider(args.rpc_url))
        if w3.eth.chain_id != 31337:
            raise ValueError("local experiment requires chain 31337")
        provider, verifier = w3.eth.accounts[1], w3.eth.accounts[3]
        deployed = EscrowClient.deploy(w3, str(ARTIFACT), verifier, verifier)
        chain = MeasuredChain(w3, deployed.contract.address, json.loads(ARTIFACT.read_text())["abi"])
        journal.data.update(contract_address=deployed.contract.address, chain_id=31337, provider=provider)
    chain.journal = journal
    journal.write()
    helper._chain = lambda: ((chain, provider) if state["mode"] == "http_store_anvil" else (None, None))
    helper._store = lambda: HttpStore(args.store_url)
    os.environ["AA_PAYOUT_WEI"] = str(PAYOUT if args.network == "mainnet" else 1)
    os.environ["AA_SCOPE_PREFIX"] = str(out / "native-files") + "/"
    os.environ["AA_CONSENT_TOOLS"] = "write"
    original_begin = aa_openclaw.begin_session

    def begin(*positional, **kwargs):
        acc, session = original_begin(*positional, **kwargs)
        acc.checkpoint_records, acc.checkpoint_seconds = state["checkpoint_records"], state["checkpoint_seconds"]
        return acc, session
    aa_openclaw.begin_session = begin

    @helper.app.post("/measurement/configure")
    def configure(config: dict):
        if any(binding.summary is None for binding in aa_openclaw._sessions.values()):
            raise HTTPException(409, "a previous measurement session is still active")
        mode = config.get("mode", "http_store")
        if mode not in {"http_store", "http_store_anvil"}:
            raise HTTPException(422, "invalid measurement mode")
        n, seconds = int(config.get("checkpoint_records", 10)), float(config.get("checkpoint_seconds", 30))
        if n < 1 or seconds <= 0:
            raise HTTPException(422, "invalid checkpoint cadence")
        state.update(mode=mode, checkpoint_records=n, checkpoint_seconds=seconds)
        return state

    @helper.app.get("/measurement/snapshot")
    def snapshot(native_session_id: str):
        acc, session = aa_openclaw.current(native_session_id)
        if session is None or session._summary is None:
            raise HTTPException(409, "session did not finalize")
        if session._worker:
            session._worker.join(timeout=5)
            if session._worker.is_alive():
                raise HTTPException(409, "checkpoint worker still active")
        records = acc.store.get_records(session.session_id)
        parsed = [ActionRecord.from_dict(r) for r in records]
        digest = trace_hash(parsed)
        if len(parsed) != session._seq or digest != trace_hash(session.records):
            raise HTTPException(409, "store does not match session evidence")
        checkpoints, chain_state = [], None
        if acc.chain:
            checkpoints = chain.get_checkpoints(session.session_id)
            check_prefixes(parsed, checkpoints, session.session_id)
            s = chain.get_session(session.session_id)
            if Web3.to_hex(s[2]) != digest or not s[4]:
                raise HTTPException(409, "final commitment mismatch")
            chain_state = jsonable(s)
        verdicts = {str(k): {"violated": v.violated, "seq": v.seq, "reason": v.reason}
                    for k, v in acc.self_check(parsed).items()}
        if any(v["violated"] for v in verdicts.values()):
            raise HTTPException(409, "permitted measurement workload triggered a violation")
        result = {"native_session_id": native_session_id, "session_id": session.session_id,
                  "records": records, "recorded_at_unix": session._recorded_at,
                  "trace_hash": digest, "checkpoints": checkpoints, "chain_state": chain_state,
                  "verdicts": verdicts, "summary": session._summary,
                  "transactions": [t for t in journal.data["transactions"] if t["session_id"] == session.session_id]}
        dump(out / "sessions" / f"{native_session_id}.json", result)
        return result

    uvicorn.run(helper.app, host="127.0.0.1", port=args.helper_port, access_log=False)


@contextmanager
def services(args):
    children, logs = [], []
    for port in (args.store_port, args.helper_port, *([args.anvil_port] if args.network == "local" else [])):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    commands = [([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(args.store_port), "--no-access-log"], ROOT / "packages/store", {"STORE_DB": str(args.output / "store.sqlite")}, "store")]
    if args.network == "local":
        commands.append(([str(Path.home() / ".foundry/bin/anvil"), "--host", "127.0.0.1", "--port", str(args.anvil_port), "--chain-id", "31337", "--silent"], ROOT, {}, "anvil"))
    try:
        for command, cwd, env, name in commands:
            log = open(args.output / f"{name}.log", "w")
            logs.append(log)
            children.append(subprocess.Popen(command, cwd=cwd, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT))
        for _ in range(100):
            try:
                ready = requests.get(args.store_url + "/health", timeout=.5).ok
                if args.network == "local":
                    ready = ready and Web3(Web3.HTTPProvider(args.rpc_url)).eth.chain_id == 31337
                if ready:
                    break
            except Exception:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError("measurement store/chain failed to start")
        command = [sys.executable, str(Path(__file__)), "serve", "--output", str(args.output),
                   "--network", args.network, "--store-port", str(args.store_port),
                   "--helper-port", str(args.helper_port), "--anvil-port", str(args.anvil_port)]
        if args.keystore_dir:
            command += ["--keystore-dir", str(args.keystore_dir)]
        log = open(args.output / "helper.log", "w")
        logs.append(log)
        children.append(subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))
        for _ in range(1800):
            if children[-1].poll() is not None:
                raise RuntimeError("measurement helper exited; inspect helper.log")
            try:
                if requests.get(args.helper_url + "/health", timeout=.5).ok:
                    break
            except Exception:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError("measurement helper failed to start")
        yield
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in logs:
            log.close()


def run(args):
    if args.network == "mainnet" and (args.repetitions != 3 or args.actions != 20):
        raise ValueError("authorized mainnet matrix is exactly three repetitions of 20 writes per policy")
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("use a fresh output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    if not native_pin.matches(OPENCLAW, "openclaw"):
        raise ValueError("native OpenClaw must be the upstream.json commit with the release patch applied")
    funding, _ = public_metadata()
    party = funding["role_addresses"]["challenger"] if args.network == "mainnet" else "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
    rng, jobs = random.Random(args.seed), []
    if args.network == "local":
        modes = ["capture_off", "http_store", "http_store_anvil"]
        schedule = [(-1, m) for m in modes] + [(rep, m) for rep in range(args.repetitions) for m in rng.sample(modes, len(modes))]
    else:
        schedule = [(rep, name) for rep in range(args.repetitions) for name in STRATEGIES]
        rng.shuffle(schedule)
    for rep, name in schedule:
        mode = name if args.network == "local" else "http_store_anvil"
        cadence = (10, 30) if args.network == "local" else STRATEGIES[name]
        session_id = f"openclaw-{args.output.name}-{rep}-{name}"
        jobs.append({"helper_url": args.helper_url, "party": party, "session_id": session_id,
                     "directory": str(args.output / "native-files" / session_id),
                     "writes": args.actions, "payload_bytes": 1024, "enabled": mode != "capture_off",
                     "mode": mode, "repetition": rep, "warmup": rep == -1, "strategy": name,
                     "checkpoint_records": cadence[0], "checkpoint_seconds": cadence[1],
                     "pause_after": 2, "pause_seconds": 35 if args.network == "mainnet" else 0,
                     "inter_write_seconds": .25 if args.network == "mainnet" else 0})
        if args.network == "mainnet":
            jobs[-1]["helper_timeout_ms"] = 120000
    dump(args.output / "jobs.json", {"jobs": jobs})
    manifest = {"started_at_utc": utc(), "network": args.network, "seed": args.seed,
                "python": sys.version, "node": subprocess.check_output([str(NODE), "--version"], text=True).strip(),
                "machine": platform.platform(), "openclaw_revision": revision,
                "prototype_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "controlled_approval": "native generic approval request/wait decision receives allow-once; no user UI or model",
                "timed_scope": "native write dispatch through actual plugin HTTP/helper/store plus session finalization; setup excluded",
                "helper_request_timeout_seconds": 120 if args.network == "mainnet" else 5,
                "scripts_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in (ROOT / "operating_cost").glob("*openclaw*") if p.is_file()}}
    source_paths = [p for directory in (ROOT / "packages/sdk/aa_sdk", ROOT / "packages/commons/aa_commons",
                    ROOT / "integrations/openclaw/aa_openclaw", ROOT / "integrations/openclaw/aa_helper",
                    ROOT / "integrations/openclaw/plugin/src")
                    for p in directory.rglob("*") if p.is_file() and p.suffix in {".py", ".ts"}]
    source_paths.append(ROOT / "packages/store/app.py")
    source_paths.extend(p for p in (ROOT / "operating_cost").glob("*openclaw*") if p.is_file())
    manifest["protocol_source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in source_paths}
    for source in source_paths:
        destination = args.output / "source" / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    if args.network == "mainnet":
        manifest["deployed_artifact_sha256"] = hashlib.sha256(DEPLOYED_ARTIFACT.read_bytes()).hexdigest()
        manifest["deployed_contract_source"] = "September 16 Hermes deployment; measured registration/open/checkpoint/final source bodies unchanged"
        for name in ("Escrow.json", "Escrow.sol"):
            destination = args.output / "source/deployed-contract" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((DEPLOYED_ARTIFACT.parent / name).read_bytes())
    dump(args.output / "manifest.json", manifest)
    with services(args):
        command = [str(NODE), str(OPENCLAW / "node_modules/vitest/vitest.mjs"), "run", "--config",
                   str(ROOT / "operating_cost/openclaw_native_driver.config.ts")]
        env = {**os.environ, "PATH": str(NODE.parent) + os.pathsep + os.environ["PATH"],
               "AA_MEASUREMENT_JOB": str(args.output / "jobs.json"),
               "AA_MEASUREMENT_OUTPUT": str(args.output / "native-results.json")}
        dump(args.output / "driver-command.json", command)
        with open(args.output / "native-driver.log", "w") as log:
            subprocess.run(command, cwd=OPENCLAW, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    manifest.update(finished_at_utc=utc(), status="completed")
    dump(args.output / "manifest.json", manifest)
    print("completed", args.output, flush=True)


def preflight(args):
    w3 = mainnet()
    funding, deployment, _ = validate_existing(w3)
    addresses = {"funding": funding["funding_address"], **funding["role_addresses"]}
    report = {"at_utc": utc(), "chain_id": 8453, "contract_address": deployment["address"],
              "balances_wei": {k: w3.eth.get_balance(v) for k, v in addresses.items()},
              "addresses": addresses, "gas_price_wei": w3.eth.gas_price,
              "provider_funding_ceiling_wei": PROVIDER_ALLOWANCE,
              "artifact_sha256": hashlib.sha256(DEPLOYED_ARTIFACT.read_bytes()).hexdigest()}
    if args.output:
        dump(args.output, report)
    print(json.dumps(report, indent=2))


def fund(args):
    if args.output.exists():
        raise ValueError("funding journal exists; reconcile it before any retry")
    w3 = mainnet()
    funding, _, _ = validate_existing(w3)
    address, target = funding["funding_address"], funding["role_addresses"]["provider"]
    account = private_account(args.funding_dir / "keystore.json", args.funding_dir / "keystore-password.txt", address)
    value = max(0, PROVIDER_ALLOWANCE - w3.eth.get_balance(target))
    if not value:
        print("provider already has its experiment allowance")
        return
    if value > PROVIDER_ALLOWANCE or w3.eth.get_transaction_count(address, "pending") != w3.eth.get_transaction_count(address, "latest"):
        raise ValueError("funding ceiling or pending nonce check failed")
    latest = w3.eth.get_block("latest")
    maximum = 2 * int(latest["baseFeePerGas"]) + 1000000
    if maximum > MAX_GAS_PRICE or w3.eth.get_balance(address) <= value + 21000 * maximum + 10**12:
        raise ValueError("funding balance/fee check failed")
    tx = {"chainId": 8453, "nonce": w3.eth.get_transaction_count(address, "pending"),
          "to": target, "value": value, "gas": 21000,
          "maxFeePerGas": maximum, "maxPriorityFeePerGas": 1000000}
    signed = account.sign_transaction(tx)
    tx_hash = Web3.to_hex(Web3.keccak(signed.raw_transaction))
    report = {"at_utc": utc(), "state": "prepared", "transaction": tx, "hash": tx_hash}
    dump(args.output, report)
    try:
        actual = w3.eth.send_raw_transaction(signed.raw_transaction)
        assert Web3.to_hex(actual) == tx_hash
        report["state"] = "submitted"
        dump(args.output, report)
        receipt = w3.eth.wait_for_transaction_receipt(actual)
        assert receipt.status == 1
        report.update(state="confirmed", receipt=jsonable(receipt))
        dump(args.output, report)
        print("provider funding confirmed", tx_hash, "ETH", Decimal(value) / Decimal(10**18), flush=True)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        dump(args.output, report)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["serve", "run", "preflight", "fund"])
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--network", choices=["local", "mainnet"], default="local")
    p.add_argument("--keystore-dir", type=Path)
    p.add_argument("--funding-dir", type=Path)
    p.add_argument("--store-port", type=int, default=18761)
    p.add_argument("--helper-port", type=int, default=18762)
    p.add_argument("--anvil-port", type=int, default=18763)
    p.add_argument("--repetitions", type=int, default=30)
    p.add_argument("--actions", type=int, default=100)
    p.add_argument("--seed", type=int, default=20260924)
    args = p.parse_args()
    args.output = args.output.resolve()
    args.store_url = f"http://127.0.0.1:{args.store_port}"
    args.helper_url = f"http://127.0.0.1:{args.helper_port}"
    args.rpc_url = f"http://127.0.0.1:{args.anvil_port}"
    {"serve": serve, "run": run, "preflight": preflight, "fund": fund}[args.mode](args)


if __name__ == "__main__":
    main()
