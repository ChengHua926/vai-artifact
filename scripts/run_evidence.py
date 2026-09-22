"""Record reproducible SDK/escrow scenarios on Anvil or Base Sepolia.

This is a protocol test with synthetic authorization decisions, not a live model run.
Native framework hook evidence is collected by the integration tests separately.
The public-network path refuses uncommitted source and never accelerates deadlines.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# The run's revision must identify the Python implementation actually imported, including
# when a worktree reuses another checkout's virtual environment.
ROOT = Path(__file__).resolve().parents[1]
for package in ("commons", "sdk", "store", "verifier"):
    sys.path.insert(0, str(ROOT / "packages" / package))

from web3 import Web3
from web3.exceptions import BlockNotFound, BadFunctionCallOutput, Web3RPCError
import requests

import _config as C
import aa_commons
import aa_sdk
import aa_verifier
from aa_commons import ActionRecord, registry, trace_hash
from aa_sdk import Accountability, HttpStore
from aa_sdk.chain import EscrowClient, _is_missing_block
from aa_verifier import process_challenge


def _integer(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def receipt_summary(receipt, elapsed):
    execution = _integer(receipt["gasUsed"]) * _integer(receipt["effectiveGasPrice"])
    l1 = _integer(receipt["l1Fee"]) if receipt.get("l1Fee") is not None else None
    return {
        "transaction_hash": Web3.to_hex(receipt["transactionHash"]),
        "block_number": _integer(receipt["blockNumber"]),
        "status": _integer(receipt["status"]),
        "gas_used": _integer(receipt["gasUsed"]),
        "effective_gas_price_wei": _integer(receipt["effectiveGasPrice"]),
        "execution_fee_wei": execution,
        "l1_fee_wei": l1,
        "execution_plus_l1_fee_wei": execution + l1 if l1 is not None else None,
        "confirmation_seconds": elapsed,
    }


def validate_revision(revision, status, chain_id, allow_dirty):
    if not revision or (status and (chain_id != 31337 or not allow_dirty)):
        raise ValueError("run requires committed source; --allow-dirty is local-only")


def module_sources(modules, root=ROOT):
    result = {}
    for module in modules:
        source = Path(module.__file__).resolve()
        if not source.is_relative_to(root):
            raise ValueError(f"{module.__name__} was imported outside the recorded checkout")
        result[module.__name__] = str(source.relative_to(root))
    return result


def json_value(value):
    if isinstance(value, bytes):
        return Web3.to_hex(value)
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    return value


def evidence_status(attempts):
    return "passed" if all(a["state"] == "confirmed" for a in attempts) else "scenarios_passed_evidence_incomplete"


def validate_store_inventory(inventory):
    if inventory != {"promise_count": 0, "record_count": 0}:
        raise ValueError("a new deployment requires a fresh dedicated trace store")


def confirmed_runtime(w3, address, expected, block_number, timeout=30, interval=0.5):
    """Read the receipt's block; public RPC heads can lag a returned receipt."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            runtime = w3.eth.get_code(address, block_identifier=block_number)
        except (BlockNotFound, Web3RPCError) as error:
            if not _is_missing_block(error, block_number):
                raise
            runtime = b""
        if runtime:
            if runtime != expected:
                raise ValueError("deployed bytecode differs from the recorded artifact")
            return runtime
        if time.monotonic() >= deadline:
            raise TimeoutError("deployment code unavailable at the receipt block")
        time.sleep(interval)


def confirmed_operator(contract, operator, block_number, timeout=30, interval=0.5):
    """Role reads may reach a different backend after the bytecode check succeeds."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            for function in (contract.functions.owner, contract.functions.verifier):
                if function().call(block_identifier=block_number) != operator:
                    raise ValueError("deployed role differs from the configured operator")
            return
        except (BlockNotFound, Web3RPCError, BadFunctionCallOutput) as error:
            if not isinstance(error, BadFunctionCallOutput) and not _is_missing_block(error, block_number):
                raise
            # Empty eth_call output can come from a backend that has not seen deployment.
            # Decoded role mismatches and all other errors propagate immediately.
            if time.monotonic() >= deadline:
                raise TimeoutError("deployment roles unavailable at the receipt block")
            time.sleep(interval)


class Journal:
    def __init__(self, path, manifest):
        self.path, self.manifest = path, manifest
        self.lock = threading.RLock()
        self.write()

    def write(self):
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.manifest, indent=2) + "\n")
            temporary.replace(self.path)

    def transaction(self, operation, receipt, elapsed):
        with self.lock:
            item = {"operation": operation, **receipt_summary(receipt, elapsed)}
            self.manifest["transactions"].append(item)
            self.write()
            print(operation, item["transaction_hash"], "status", item["status"], flush=True)

    def attempt(self, operation, *, sender, value=0, function_args=()):
        with self.lock:
            item = {"operation": operation, "state": "unconfirmed",
                    "sender": sender, "value_wei": value, "function_args": json_value(function_args),
                    "started_at_utc": datetime.now(timezone.utc).isoformat()}
            self.manifest["attempts"].append(item)
            self.write()
            return item

    def update_attempt(self, attempt, **fields):
        with self.lock:
            attempt.update(fields)
            self.write()


class RecordedEscrow(EscrowClient):
    def _send(self, fn, sender, value=0):
        started = time.monotonic()
        attempt = self.journal.attempt(fn.fn_name, sender=sender, value=value, function_args=fn.args)
        try:
            receipt = super()._send(fn, sender, value)
        except Exception as error:
            self.journal.update_attempt(attempt, error=f"{type(error).__name__}: {error}")
            raise
        self.journal.update_attempt(attempt, state="confirmed", transaction_hash=Web3.to_hex(receipt.transactionHash))
        self.journal.transaction(fn.fn_name, receipt, time.monotonic() - started)
        return receipt


def _wait_for_checkpoint(escrow, session, timeout=90):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        checkpoints = escrow.get_checkpoints(session.session_id)
        if checkpoints:
            return time.monotonic() - started
        time.sleep(1)
    raise RuntimeError(f"checkpoint not observed within {timeout} seconds")


def run(args):
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("output already exists; preserve the previous run")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=C.REPO, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=C.REPO, text=True).strip()
    validate_revision(revision, status, C.CHAIN_ID, args.allow_dirty)
    imported = module_sources((aa_commons, aa_sdk, aa_verifier, C))
    w3 = C.w3()
    actors = C.actors(w3)
    provider, user, verifier = (actors[r][0] for r in ("provider", "challenger", "verifier"))
    assert len({provider, user, verifier}) == 3
    store = HttpStore(C.STORE_URL)
    # Each deployment uses its own store: promise IDs restart at one per contract.
    # The operator must keep this dedicated instance exclusive for the run.
    inventory_response = requests.get(f"{store.base_url}/inventory", timeout=10)
    inventory_response.raise_for_status()
    validate_store_inventory(inventory_response.json())
    artifact = Path(C.ARTIFACT).read_bytes()
    compiled = json.loads(artifact)
    metadata = compiled["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    contract_source = (Path(C.REPO) / "contracts/src/Escrow.sol").read_bytes()
    if metadata["sources"]["src/Escrow.sol"]["keccak256"] != Web3.to_hex(Web3.keccak(contract_source)):
        raise ValueError("compiled contract is stale; rebuild the committed source")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1, "status": "running",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": revision, "source_dirty": bool(status),
        "module_sources": imported,
        "chain_id": w3.eth.chain_id,
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "contract_source_sha256": hashlib.sha256(contract_source).hexdigest(),
        "actors": {r: actors[r][0] for r in ("provider", "challenger", "verifier", "deployer")},
        "balances_before_wei": {r: w3.eth.get_balance(actors[r][0]) for r in ("provider", "challenger", "verifier")},
        "scenario_origin": "SDK protocol scenarios with synthetic authorization decisions; native framework tests are separate",
        "clock_policy": "unchanged contract deadlines; no time travel",
        "checkpoint_defaults": {"records": 10, "seconds_while_dirty": 30, "outage": "continue and retry"},
        "predicates": registry.catalog(), "attempts": [], "transactions": [], "scenarios": [],
    }
    journal = Journal(output, manifest)
    try:
        started = time.monotonic()
        deploy_attempt = journal.attempt("deploy", sender=actors["deployer"][0], function_args=(verifier,))
        deployed = EscrowClient.deploy(w3, C.ARTIFACT, actors["deployer"][0], verifier, C.accounts_map(actors))
        journal.update_attempt(deploy_attempt, state="confirmed", transaction_hash=Web3.to_hex(deployed.deploy_receipt.transactionHash))
        manifest["contract_address"] = deployed.contract.address
        journal.transaction("deploy", deployed.deploy_receipt, time.monotonic() - started)
        escrow = RecordedEscrow(w3, deployed.contract.address, deployed.contract.abi, C.accounts_map(actors))
        escrow.deploy_block = deployed.deploy_block
        escrow.deploy_receipt = deployed.deploy_receipt
        escrow._observe_receipt(deployed.deploy_receipt)
        escrow.journal = journal
        manifest["contract_address"] = escrow.contract.address
        expected_runtime = bytes.fromhex(compiled["deployedBytecode"]["object"].removeprefix("0x"))
        runtime = confirmed_runtime(w3, escrow.contract.address, expected_runtime, deployed.deploy_block)
        manifest["deployed_runtime_keccak256"] = Web3.to_hex(Web3.keccak(runtime))
        confirmed_operator(escrow.contract, verifier, deployed.deploy_block)
        C.save_deployment({"address": escrow.contract.address, "deploy_block": escrow.deploy_block,
            "provider_addr": provider, "challenger_addr": user, "verifier_addr": verifier,
            "source_revision": revision, "artifact_sha256": manifest["artifact_sha256"], "promises": {}})

        acc = Accountability("evidence-provider", store=store, chain=escrow, provider_addr=provider)
        payout = 10**13
        consent = acc.register_promise("no_destructive_without_consent",
            {"destructive_tools": ["exec"], "authorization_mode": "invocation"}, payout, reserve_wei=3*payout)
        scope = acc.register_promise("action_within_declared_scope",
            {"scoped_tools": ["write"], "allow_prefixes": ["workspace/"]}, payout, reserve_wei=4*payout)
        manifest["promises"] = {"consent": consent, "scope": scope, "payout_wei": payout}
        deployment = C.load_deployment()
        deployment["promises"] = {"consent": consent, "scope": scope}
        C.save_deployment(deployment)

        def evidence_for(session):
            onchain = escrow.get_session(session.session_id)
            return {"records": store.get_records(session.session_id),
                "final_trace_hash": Web3.to_hex(onchain[2]), "opened_at": onchain[3],
                "committed_at": onchain[4], "checkpoints": escrow.get_checkpoints(session.session_id)}

        def settle(name, session, promise_id, expected, selected_store=store, respond=False):
            before = escrow.get_promise(promise_id)[4]
            credit_before = escrow.bond_of(provider)
            cid = escrow.challenge(user, session.session_id, promise_id, C.CHALLENGE_BOND)
            if respond:
                escrow.respond(provider, cid)
            verdict = process_challenge(escrow, selected_store, cid, verifier)
            after = escrow.get_promise(promise_id)[4]
            assert verdict["violated"] is expected, verdict
            assert before - after == (payout if expected else 0)
            credit_after = escrow.bond_of(provider)
            assert credit_after - credit_before == (0 if expected else C.CHALLENGE_BOND)
            assert verdict["paid_to_challenger"] == (payout + C.CHALLENGE_BOND if expected else 0)
            manifest["scenarios"].append({"name": name, "session_id": session.session_id,
                "challenge_id": cid, "verdict": verdict, "reserve_before_wei": before,
                "reserve_after_wei": after, "provider_credit_before_wei": credit_before,
                "provider_credit_after_wei": credit_after, "promise": store.get_promise(promise_id),
                **evidence_for(session)})
            journal.write()

        invocation = {"argv": ["/usr/bin/true"]}
        def execute():
            completed = subprocess.run(invocation["argv"], check=False, capture_output=True, text=True)
            return {"exit_code": completed.returncode, "stdout": completed.stdout}

        for name, decision, repeat in (("consent_once_satisfied", "allow", False),
                                       ("consent_once_reuse_violated", "allow", True),
                                       ("consent_denied_but_executed", "deny", False)):
            session = acc.session(user)
            action = f"{session.session_id}:exec:1"
            session.record_authorization(event_id=f"{action}:decision", action_id=action,
                request_id=f"{action}:request", tool="exec", args=invocation,
                decision=decision, scope="once", authority="human", principal=user,
                native_session_id=session.session_id, native_decision=decision)
            session.guard("exec", invocation, execute, action_id=action)
            session.checkpoint(force=True)
            assert escrow.get_checkpoints(session.session_id)
            assert escrow.get_session(session.session_id)[4] == 0
            if repeat:
                session.guard("exec", invocation, execute, action_id=f"{session.session_id}:exec:2")
            session.end()
            settle(name, session, consent, decision == "deny" or repeat)

        session = acc.session(user)
        session.record("write", {"target": "outside/report.txt"}, {"ok": True})
        session.end()
        settle("scope_escape", session, scope, True)

        # Both SDK triggers are exercised with their unchanged 10-record/30-second defaults.
        for name, count in (("count_checkpoint", 10), ("timer_checkpoint", 1)):
            session = acc.session(user)
            started = time.monotonic()
            for i in range(count):
                session.record("observe", {"index": i}, {"ok": True})
            wait = _wait_for_checkpoint(escrow, session)
            assert escrow.get_session(session.session_id)[4] == 0
            checkpoint_status = session.checkpoint_status()
            session.end()
            manifest["scenarios"].append({"name": name, "session_id": session.session_id,
                "record_count": count, "elapsed_seconds": time.monotonic() - started,
                "wait_for_checkpoint_seconds": wait, "checkpoint_status": checkpoint_status,
                **evidence_for(session)})
            journal.write()

        class Unavailable:
            def get_records(self, session_id):
                raise ConnectionError("controlled evidence-availability scenario")
            def get_promise(self, promise_id):
                return store.get_promise(promise_id)

        session = acc.session(user)
        session.record("observe", {}, {"ok": True})
        session.end()
        settle("unavailable_after_provider_response", session, scope, True, Unavailable(), respond=True)

        # Deliberately bypass SDK finalization to model a provider contradicting its own prefix.
        session = acc.session(user)
        session.record("observe", {"value": "original"}, {"ok": True})
        session.checkpoint(force=True)
        original = store.get_records(session.session_id)
        rewritten = json.loads(json.dumps(original))
        rewritten[0]["args"]["value"] = "rewritten"
        escrow.commit_trace(provider, session.session_id,
            trace_hash([ActionRecord.from_dict(r) for r in rewritten]))

        class Rewritten:
            def get_records(self, session_id):
                return rewritten if session_id == session.session_id else store.get_records(session_id)
            def get_promise(self, promise_id):
                return store.get_promise(promise_id)

        settle("anchored_prefix_rewrite", session, scope, True, Rewritten(), respond=True)
        manifest["scenarios"][-1]["presented_records"] = rewritten
        timings = [t["confirmation_seconds"] for t in manifest["transactions"]]
        manifest["confirmation_timing"] = {"n": len(timings), "median_seconds": statistics.median(timings),
            "max_seconds": max(timings), "definition": "client submission through receipt; includes RPC and polling",
            "coverage": "confirmed receipts only",
            "unconfirmed_attempts": sum(a["state"] != "confirmed" for a in manifest["attempts"])}
        manifest["balances_after_wei"] = {r: w3.eth.get_balance(actors[r][0]) for r in ("provider", "challenger", "verifier")}
        manifest["status"] = evidence_status(manifest["attempts"])
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        journal.write()
        print("Evidence saved:", output, flush=True)
    except Exception as error:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        journal.write()
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="new JSON evidence file; never overwrites")
    parser.add_argument("--allow-dirty", action="store_true", help="allow development runs on local chain 31337 only")
    run(parser.parse_args())
