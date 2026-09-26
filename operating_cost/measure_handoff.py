"""Bounded local/mainnet experiments for provider-submitted claim evidence (V2 claim flow).

The provider's whole answer to a claim is signed evidence delivered to the verifier by the
claim's filing time plus EVIDENCE_WINDOW; nothing is sent on chain for it. The verifier settles
every claim. Constructed two-record cases cover evidence checks, refused submissions and, on
the local chain only, the evidence deadline and its race with the verifier's close (Anvil block
timestamps are set exactly; mainnet runs never touch the clock).

No model calls. Mainnet sends require the explicit --execute-mainnet switch.
Wallet material stays in the existing private experiment directory; archives
contain only public addresses, synthetic evidence, source snapshots and receipts.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "contracts/out/Escrow.sol/Escrow.json"
# Mainnet-only inputs, not part of the release. AA_MAINNET_KEYS_DIR holds keystore.json,
# keystore-password.txt, roles/<role>.json and roles/<role>.password; AA_MAINNET_PUBLIC is the
# public funding.json (funding_address, role_addresses). Unset, both name nonexistent paths.
PRIVATE = Path(os.environ.get("AA_MAINNET_KEYS_DIR") or "AA_MAINNET_KEYS_DIR-unset")
PUBLIC = Path(os.environ.get("AA_MAINNET_PUBLIC") or "AA_MAINNET_PUBLIC-unset")
RPC = "https://mainnet.base.org"
MAX_GAS_PRICE = 100_000_000
MAX_FEES = 500_000_000_000_000
PAYOUT = 10_000_000_000
BOND = 1_000_000_000
ROLES = {"provider", "verifier", "challenger"}


def number(value):
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


def fee_wei(receipt):
    return number(receipt["gasUsed"]) * number(receipt["effectiveGasPrice"]) + number(receipt.get("l1Fee", 0))


def topups(balances, targets):
    if set(targets) - ROLES or set(balances) - ROLES:
        raise ValueError("funding is restricted to the three experiment roles")
    return {role: target - balances[role] for role, target in targets.items() if balances[role] < target}


def check_fee_budget(gas, price, l1_upper, spent, maximum=MAX_FEES):
    if price > MAX_GAS_PRICE or spent + gas * price + l1_upper > maximum:
        raise ValueError("experiment fee ceiling exceeded")


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str) + "\n")
    tmp.replace(path)


def utc():
    return datetime.now(timezone.utc).isoformat()


def bootstrap():
    for package in ("commons", "sdk", "store", "verifier"):
        sys.path.insert(0, str(ROOT / "packages" / package))
    from web3 import Web3
    from eth_account import Account
    from aa_sdk.chain import EscrowClient
    from aa_sdk import HttpStore
    from aa_sdk.evidence import (EvidenceClient, EvidenceNotReady, EvidenceRefused, create_envelope,
                                 evidence_deadline)
    from aa_verifier import process_challenge
    from aa_verifier.inbox import EvidenceInbox, create_app
    from aa_commons import ActionRecord, params_hash, trace_hash, registry
    from aa_commons.ids import keccak_hex
    from aa_commons.registry import predicate_hash_for
    return SimpleNamespace(**locals())


def connection(api, url):
    import threading
    import requests
    class Paced(api.Web3.HTTPProvider):
        def __init__(self):
            super().__init__(url, request_kwargs={"timeout": 30}, exception_retry_configuration=None)
            self.last = 0
            self.lock = threading.Lock()
        def make_request(self, method, params):
            with self.lock:
                for attempt in range(6):
                    if url.startswith("https:"):
                        time.sleep(max(0, .26 - (time.monotonic() - self.last)))
                    self.last = time.monotonic()
                    try:
                        result = super().make_request(method, params)
                    except requests.HTTPError as exc:
                        if exc.response is None or exc.response.status_code != 429 or attempt == 5:
                            raise
                        time.sleep(2 ** attempt)
                        continue
                    if result.get("error", {}).get("code") == -32016 and attempt < 5:
                        time.sleep(2 ** attempt)
                        continue
                    return result
    return api.Web3(Paced())


def wallets(api, network, roles=None):
    wanted = tuple(roles or ("funding", "provider", "challenger", "verifier"))
    if set(wanted) - (ROLES | {"funding"}):
        raise ValueError("unknown wallet role")
    if network == "local":
        api.Account.enable_unaudited_hdwallet_features()
        mnemonic = "test test test test test test test test test test test junk"
        return {role: api.Account.from_mnemonic(mnemonic, account_path=f"m/44'/60'/0'/0/{i}")
                for i, role in enumerate(("funding", "provider", "challenger", "verifier")) if role in wanted}
    public = json.loads(PUBLIC.read_text())
    accounts = {}
    for role in wanted:
        key = PRIVATE / "keystore.json" if role == "funding" else PRIVATE / "roles" / f"{role}.json"
        password = PRIVATE / "keystore-password.txt" if role == "funding" else PRIVATE / "roles" / f"{role}.password"
        if key.stat().st_mode & 0o077 or password.stat().st_mode & 0o077:
            raise ValueError("wallet files must have private permissions")
        account = api.Account.from_key(api.Account.decrypt(json.loads(key.read_text()), password.read_text().strip()))
        expected = public["funding_address"] if role == "funding" else public["role_addresses"][role]
        if account.address.lower() != expected.lower():
            raise ValueError("wallet differs from recorded experiment role")
        accounts[role] = account
    if len({account.address for account in accounts.values()}) != len(wanted):
        raise ValueError("experiment wallets must be distinct")
    return accounts


class Transactions:
    def __init__(self, w3, output, mainnet):
        self.w3, self.output, self.mainnet = w3, Path(output), mainnet
        self.reload()
        self.label = "setup"

    def reload(self):
        path = self.output / "transactions.json"
        self.rows = json.loads(path.read_text()) if path.exists() else []
        if any(row["status"] not in {"confirmed", "reverted"} for row in self.rows):
            raise RuntimeError("unresolved transaction journal; reconcile before continuing")
        self.spent = sum(row.get("fee_wei", 0) for row in self.rows)

    def send(self, account, fn=None, value=0, to=None, operation=None):
        w3 = self.w3
        pending = w3.eth.get_transaction_count(account.address, "pending")
        if pending != w3.eth.get_transaction_count(account.address, "latest"):
            raise RuntimeError("pending transaction exists; reconcile journal before retry")
        fields = {"from": account.address, "nonce": pending, "value": value}
        gas = int(fn.estimate_gas(fields) * 1.2) if fn is not None else 21000
        price = int(w3.eth.gas_price * 1.25) + 1 if self.mainnet else 2_000_000_000
        fields.update(gas=gas, gasPrice=price, chainId=w3.eth.chain_id)
        tx = fn.build_transaction(fields) if fn is not None else {**fields, "to": to}
        signed = account.sign_transaction(tx)
        l1_allowance = 0
        if self.mainnet:
            oracle = w3.eth.contract(address="0x420000000000000000000000000000000000000F", abi=[
                {"name": "getL1FeeUpperBound", "type": "function", "stateMutability": "view",
                 "inputs": [{"name": "size", "type": "uint256"}], "outputs": [{"type": "uint256"}]}])
            # Live upper estimate with additional price-change headroom, not a protocol fee cap.
            l1_allowance = 2 * int(oracle.functions.getL1FeeUpperBound(len(signed.raw_transaction) + 300).call()) + 5 * 10**12
            check_fee_budget(gas, price, l1_allowance, self.spent)
        if w3.eth.get_balance(account.address) <= value + gas * price + l1_allowance:
            raise ValueError("wallet does not cover value and conservative fee estimate")
        tx_hash = w3.keccak(signed.raw_transaction).hex()
        row = {"at_utc": utc(), "case": self.label, "operation": operation or getattr(fn, "fn_name", "deploy"),
               "hash": "0x" + tx_hash.removeprefix("0x"), "sender": account.address,
               "to": tx.get("to"), "value_wei": value, "nonce": pending,
               "gas_limit": gas, "gas_price_wei": price, "l1_fee_allowance_wei": l1_allowance,
               "status": "signed_not_sent"}
        self.rows.append(row)
        dump(self.output / "transactions.json", self.rows)
        started = time.perf_counter()
        broadcast = w3.eth.send_raw_transaction(signed.raw_transaction)
        row["status"] = "broadcast"
        dump(self.output / "transactions.json", self.rows)
        receipt = w3.eth.wait_for_transaction_receipt(broadcast, timeout=180, poll_latency=.5)
        row["first_receipt_seconds"] = time.perf_counter() - started
        # Base preconfirmation receipts can have zero block hashes. Refresh before accounting.
        for _ in range(60):
            if bytes(receipt.blockHash) != bytes(32):
                break
            time.sleep(.5)
            receipt = w3.eth.get_transaction_receipt(broadcast)
        if bytes(receipt.blockHash) == bytes(32):
            raise RuntimeError("receipt has not reached canonical L2 inclusion; preserve journal and reconcile")
        if self.mainnet and "l1Fee" not in receipt:
            raise RuntimeError("canonical receipt lacks L1 fee; reconcile before continuing")
        row.update(status="confirmed" if receipt.status == 1 else "reverted", receipt=json.loads(w3.to_json(receipt)),
                   fee_wei=fee_wei(receipt), canonical_receipt_seconds=time.perf_counter() - started)
        self.spent += row["fee_wei"]
        dump(self.output / "transactions.json", self.rows)
        if receipt.status != 1:
            raise RuntimeError(f"transaction reverted: {row['hash']}")
        if self.mainnet and self.spent > MAX_FEES:
            raise RuntimeError("realized fees exceeded the planning ceiling; stop all further sends")
        print(json.dumps({"operation": row["operation"], "case": self.label, "hash": row["hash"],
                          "fee_wei": row["fee_wei"]}), flush=True)
        return receipt


def recorded_escrow(api, w3, address, accounts, transactions):
    class RecordedEscrow(api.EscrowClient):
        def _send(self, fn, sender, value=0):
            receipt = transactions.send(self.accounts[sender], fn, value=value)
            self._observe_receipt(receipt)
            return receipt
    return RecordedEscrow(w3, address, json.loads(ARTIFACT.read_text())["abi"],
                          {a.address: a for a in accounts.values()})


def validate_build(api, artifact):
    metadata = artifact["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    source_hash = api.Web3.to_hex(api.Web3.keccak((ROOT / "contracts/src/Escrow.sol").read_bytes()))
    if metadata["sources"]["src/Escrow.sol"]["keccak256"] != source_hash:
        raise ValueError("compiled contract does not match current Solidity source")


def isolated_verdict(escrow, cid, case, output, network, transactions):
    """Run adjudication without provider credentials or a provider-store client."""
    result_path = output / f"verdict-{cid}.json"
    command = [sys.executable, str(Path(__file__).resolve()), "adjudicate", "--rpc", escrow.w3.provider.endpoint_uri,
               "--contract", escrow.contract.address, "--database", str(output / "verifier-inbox.sqlite"),
               "--claim", str(cid), "--case", case, "--network", network, "--output", str(output),
               "--result", str(result_path), "--min-block", str(escrow._confirmed_block)]
    env = {k: v for k, v in os.environ.items() if k not in {"STORE_TOKEN", "STORE_URL"}}
    with open(output / f"adjudicator-{cid}.log", "w") as log:
        subprocess.run(command, env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    transactions.reload()
    # Later reads must not predate the adjudicator's verdict on a lagging RPC backend.
    verdicts = [row for row in transactions.rows if row["case"] == case and row["operation"] == "submitVerdict"
                and row["status"] == "confirmed"]
    if verdicts:
        escrow._observe_receipt(SimpleNamespace(blockNumber=number(verdicts[-1]["receipt"]["blockNumber"])))
    return json.loads(result_path.read_text())["verdict"]


def parse_ports(text):
    first, _, last = text.partition("-")
    first, last = int(first), int(last or first)
    if not 1024 <= first <= last <= 65535:
        raise argparse.ArgumentTypeError("ports must be an inclusive range such as 38500-38599")
    return first, last


def free_port(ports=None, taken=None):
    """An unused loopback port: OS-assigned, or the first bindable one in an inclusive range."""
    taken = set() if taken is None else taken
    for port in ([0] if ports is None else range(ports[0], ports[1] + 1)):
        if port in taken:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            chosen = sock.getsockname()[1]
        taken.add(chosen)
        return chosen
    raise RuntimeError("no free loopback port in the requested range")


@contextmanager
def child(command, log_path, env=None, cwd=ROOT):
    with open(log_path, "w") as log:
        proc = subprocess.Popen(command, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def ready(url, proc):
    import requests
    for _ in range(200):
        if proc.poll() is not None:
            raise RuntimeError("local HTTP service exited; inspect its log")
        try:
            if requests.get(url + "/health", timeout=.5).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(.1)
    raise RuntimeError("local HTTP service did not become ready")


def source_snapshot(output):
    names = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, text=True).splitlines()
    selected = sorted(set(name for name in names if name.startswith(("packages/", "contracts/src/", "scripts/", "operating_cost/"))
                          and Path(name).suffix in {".py", ".sol", ".toml", ".txt"}))
    hashes = {}
    for name in selected:
        source = ROOT / name
        if not source.is_file():
            continue
        content = source.read_bytes()
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    dump(output / "source-sha256.json", hashes)
    (output / "Escrow.json").write_bytes(ARTIFACT.read_bytes())
    return hashes


# Cases valid on any network. Local-only cases set Anvil block timestamps around the evidence
# deadline D = filing time + EVIDENCE_WINDOW; mainnet runs never touch the clock.
COMMON_CASES = ("satisfied", "violated", "trace_mismatch", "parameter_mismatch", "checkpoint_mismatch",
                "unauthorized_submission", "premature_delivery_refused")
LOCAL_CASES = ("no_evidence", "late_delivery_refused", "no_final_commitment", "delivery_at_deadline",
               "deadline_race_close_first", "deadline_race_delivery_first")
VIOLATED = {"violated", "trace_mismatch", "parameter_mismatch", "checkpoint_mismatch", "no_evidence",
            "late_delivery_refused", "no_final_commitment", "deadline_race_close_first"}
NO_EVIDENCE = {"no_evidence", "late_delivery_refused", "no_final_commitment", "deadline_race_close_first"}


def set_chain_time(w3, timestamp):
    """Mine one Anvil block at exactly ``timestamp``. Local runs only."""
    if w3.eth.chain_id != 31337:
        raise RuntimeError("block timestamps are only set on the local chain")
    w3.provider.make_request("evm_setNextBlockTimestamp", [timestamp])
    w3.provider.make_request("evm_mine", [])
    if w3.eth.get_block("latest")["timestamp"] != timestamp:
        raise RuntimeError(f"could not mine a block at {timestamp}")


def pair_key(api, session_id, promise_id):
    from eth_abi import encode
    return api.Web3.keccak(encode(["bytes32", "uint256"], [bytes.fromhex(session_id[2:]), promise_id]))


def inbox_outcome(database, cid, inbox):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(database)) as db:
        closed = db.execute("SELECT closed_at FROM closed_without_evidence WHERE challenge_id=?", (cid,)).fetchone()
    stored = inbox.get(cid)
    if stored is not None and closed is not None:
        raise AssertionError("a claim has both stored evidence and a closed-without-evidence record")
    return {"evidence_stored": stored is not None,
            "evidence_received_at": stored["received_at"] if stored else None,
            "closed_without_evidence_at": closed[0] if closed else None}


class HeldAfterDeadlineCheck:
    """Receiver-side chain view that pauses once, in the session read the receiver makes after
    its deadline check and before its durable write. Only used to construct race cases."""
    def __init__(self, escrow):
        import threading
        self._escrow = escrow
        self.reached, self.release = threading.Event(), threading.Event()

    def __getattr__(self, name):
        return getattr(self._escrow, name)

    def get_session(self, session_id):
        if not self.reached.is_set():
            self.reached.set()
            if not self.release.wait(300):
                raise TimeoutError("held receiver write was never released")
        return self._escrow.get_session(session_id)


@contextmanager
def held_receiver(api, escrow, database, port):
    """A second receiver instance on the verifier's inbox file (no store client, no wallet)."""
    import threading
    import uvicorn
    view = HeldAfterDeadlineCheck(api.EscrowClient(escrow.w3, escrow.contract.address, escrow.contract.abi))
    inbox = api.EvidenceInbox(database, escrow.w3.eth.chain_id, escrow.contract.address)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # the previous race case's port may be in TIME_WAIT
    sock.bind(("127.0.0.1", port))
    server = uvicorn.Server(uvicorn.Config(api.create_app(view, inbox), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        if not server.started:
            raise RuntimeError("race receiver did not start")
        yield view, f"http://127.0.0.1:{port}"
    finally:
        view.release.set()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def run_cases(api, escrow, inbox, client, provider_store, accounts, txs, output, network, race_port=None):
    import requests
    import threading
    provider, verifier, challenger = (accounts[role] for role in ("provider", "verifier", "challenger"))
    params = {"egress_tools": ["send"], "recipient_allowlist": ["alice"]}
    pred = "egress_within_allowlist"
    database = output / "verifier-inbox.sqlite"
    outcomes = []
    cases = list(COMMON_CASES) + (list(LOCAL_CASES) if network == "local" else [])

    def refused(envelope, status):
        try:
            client.submit(envelope)
        except requests.HTTPError as exc:
            assert exc.response.status_code == status, (exc.response.status_code, exc.response.text)
            return {"status": status, "detail": exc.response.json().get("detail")}
        raise AssertionError("the receiver accepted a submission it must refuse")

    def balance(address):
        return int(escrow._read(lambda block: escrow.w3.eth.get_balance(address, block_identifier=block)))

    for index, name in enumerate(cases):
        txs.label = name
        steps = {}
        pid = escrow.register_promise(provider.address, api.predicate_hash_for(pred), api.params_hash(params), PAYOUT, 2 * PAYOUT)
        sid = api.keccak_hex(f"handoff:{escrow.contract.address}:{index}:{name}".encode())
        escrow.open_session(provider.address, sid, challenger.address)
        records = [api.ActionRecord(1, sid, "read", {"path": "a"}, "x", 1),
                   api.ActionRecord(2, sid, "send", {"target": "eve" if name == "violated" else "alice"}, "ok", 2)]
        anchored = records
        if name == "checkpoint_mismatch":
            anchored = [api.ActionRecord(1, sid, "read", {"path": "different"}, "x", 1), records[1]]
        escrow.checkpoint_trace(provider.address, sid, 1, api.trace_hash(anchored[:1]))
        if name not in {"premature_delivery_refused", "no_final_commitment"}:
            escrow.commit_trace(provider.address, sid, api.trace_hash(records))
        for record in records:
            provider_store.append_record(sid, record.to_dict())
        provider_store.put_promise(pid, {"predicate": pred, "params": params})
        cid = escrow.challenge(challenger.address, sid, pid, BOND)
        deadline = api.evidence_deadline(escrow, cid)
        before = balance(challenger.address)
        reserve_before = int(escrow.get_promise(pid)[4])
        credit_before = int(escrow.bond_of(provider.address))
        # Before its deadline an unanswered claim only waits: this reads and sends nothing.
        waiting = api.process_challenge(escrow, inbox, cid, verifier.address)
        assert waiting["violated"] is None and waiting["pending"], waiting
        assert inbox.get(cid) is None
        honest = api.create_envelope(escrow, cid, [r.to_dict() for r in records], params, provider)
        receipt, delivery_seconds, result = None, None, None

        def deliver():
            started = time.perf_counter()
            delivered = client.deliver(escrow, provider.address, provider, provider_store, cid)
            return delivered, time.perf_counter() - started

        if name in {"satisfied", "violated", "checkpoint_mismatch"}:
            receipt, delivery_seconds = deliver()
        elif name in {"trace_mismatch", "parameter_mismatch"}:
            presented, presented_params = [r.to_dict() for r in records], params
            if name == "trace_mismatch":
                presented[0]["args"]["path"] = "tampered"
            else:
                presented_params = {**params, "recipient_allowlist": ["eve"]}
            started = time.perf_counter()
            receipt = client.submit(api.create_envelope(escrow, cid, presented, presented_params, provider))
            delivery_seconds = time.perf_counter() - started
        elif name == "unauthorized_submission":
            impostor = api.create_envelope(escrow, cid, [r.to_dict() for r in records], params, challenger)
            steps["outsider_submission"] = refused(impostor, 403)
            assert inbox.get(cid) is None
            receipt, delivery_seconds = deliver()
        elif name in {"premature_delivery_refused", "no_final_commitment"}:
            try:
                client.deliver(escrow, provider.address, provider, provider_store, cid)
            except api.EvidenceNotReady as exc:
                steps["provider_client"] = f"not ready: {exc}"
            else:
                raise AssertionError("evidence was delivered before the final commitment")
            if name == "premature_delivery_refused":
                # The prefix the provider held while the session ran: refused, slot left free.
                steps["prefix_submission"] = refused(
                    api.create_envelope(escrow, cid, [records[0].to_dict()], params, provider), 409)
                assert inbox.get(cid) is None
                escrow.commit_trace(provider.address, sid, api.trace_hash(records))
                receipt, delivery_seconds = deliver()
            else:
                set_chain_time(escrow.w3, deadline + 1)
        elif name == "no_evidence":
            set_chain_time(escrow.w3, deadline)
            at_deadline = api.process_challenge(escrow, inbox, cid, verifier.address)
            assert at_deadline["violated"] is None and at_deadline["pending"], at_deadline
            steps["pending_at_deadline"] = at_deadline["reason"]
            set_chain_time(escrow.w3, deadline + 1)
        elif name == "late_delivery_refused":
            set_chain_time(escrow.w3, deadline + 1)
            try:
                client.deliver(escrow, provider.address, provider, provider_store, cid)
            except api.EvidenceRefused as exc:
                steps["provider_client"] = f"refused: {exc}"
            else:
                raise AssertionError("evidence was delivered after the deadline")
            steps["late_submission"] = refused(honest, 409)
            assert inbox.get(cid) is None
        elif name == "delivery_at_deadline":
            set_chain_time(escrow.w3, deadline)
            receipt, delivery_seconds = deliver()
            assert receipt["received_at"] == deadline, receipt
            set_chain_time(escrow.w3, deadline + 1)
        elif name in {"deadline_race_close_first", "deadline_race_delivery_first"}:
            # The receiver checks the deadline at chain time D, is held before its durable write,
            # and the chain moves to D+1. Which inbox transaction commits first decides the claim.
            set_chain_time(escrow.w3, deadline)
            response = {}
            with held_receiver(api, escrow, database, race_port) as (view, url):
                def post():
                    response["http"] = requests.post(f"{url}/claims/{cid}/evidence", json=honest, timeout=300)
                sender = threading.Thread(target=post)
                sender.start()
                if not view.reached.wait(60):
                    raise RuntimeError("race receiver never reached its write")
                set_chain_time(escrow.w3, deadline + 1)
                if name == "deadline_race_close_first":
                    result = isolated_verdict(escrow, cid, name, output, network, txs)
                view.release.set()
                sender.join(300)
            http = response["http"]
            steps["held_write"] = {"status": http.status_code, "body": http.json(),
                                   "deadline_check_at": deadline, "write_released_at_chain_time": deadline + 1,
                                   "verifier_closed_before_write": name == "deadline_race_close_first"}
            if name == "deadline_race_close_first":
                assert http.status_code == 409 and "without evidence" in http.json()["detail"], http.text
            else:
                assert http.status_code == 200, http.text
                receipt = http.json()
                assert receipt["received_at"] == deadline, receipt
        else:
            raise AssertionError(f"unknown case {name}")

        if result is None:
            result = isolated_verdict(escrow, cid, name, output, network, txs)
        expected = name in VIOLATED
        assert result["violated"] is expected, (name, result)
        after = balance(challenger.address)
        reserve_after = int(escrow.get_promise(pid)[4])
        credit_after = int(escrow.bond_of(provider.address))
        assert after - before == (PAYOUT + BOND if expected else 0), (name, before, after)
        assert reserve_before - reserve_after == (PAYOUT if expected else 0)
        assert credit_after - credit_before == (0 if expected else BOND)
        state = int(escrow.get_challenge(cid)[4])
        assert state == (2 if expected else 3), (name, state)
        resolved_fn = escrow.contract.functions.resolved(pair_key(api, sid, pid))
        resolved = bool(escrow._read(lambda block: resolved_fn.call(block_identifier=block)))
        assert resolved, name
        stored = inbox_outcome(database, cid, inbox)
        assert stored["evidence_stored"] is (name not in NO_EVIDENCE), (name, stored)
        assert (stored["closed_without_evidence_at"] is not None) is (name in NO_EVIDENCE), (name, stored)
        outcome = {"case": name, "challenge_id": cid, "promise_id": pid, "session_id": sid,
                   "evidence_deadline": deadline, "expected_violated": expected, "result": result,
                   "receipt": receipt, "delivery_seconds": delivery_seconds, "steps": steps, "inbox": stored,
                   "claimant_balance_delta_wei": after - before, "reserve_delta_wei": reserve_before - reserve_after,
                   "provider_credit_delta_wei": credit_after - credit_before, "state": state,
                   "pair_resolved": resolved, "passed": True}
        outcomes.append(outcome)
        dump(output / "cases.json", outcomes)
        print(json.dumps({"case_passed": name, "violated": expected}), flush=True)
    return outcomes


def experiment(args):
    from contextlib import ExitStack
    api = bootstrap()
    output = args.output.resolve()
    if output.exists():
        raise ValueError("use a fresh output directory; never overwrite an experiment")
    if args.network == "mainnet" and not args.execute_mainnet:
        raise ValueError("mainnet sends require --execute-mainnet")
    output.mkdir(parents=True)
    hashes = source_snapshot(output)
    taken = set()
    with ExitStack() as stack:
        rpc = RPC
        if args.network == "local":
            port = free_port(args.ports, taken)
            rpc = f"http://127.0.0.1:{port}"
            proc = stack.enter_context(child([str(Path.home()/'.foundry/bin/anvil'), "--port", str(port), "--silent"], output / "anvil.log"))
            for _ in range(100):
                try:
                    if connection(api, rpc).eth.chain_id == 31337:
                        break
                except Exception:
                    time.sleep(.1)
        w3 = connection(api, rpc)
        expected_chain = 8453 if args.network == "mainnet" else 31337
        if w3.eth.chain_id != expected_chain:
            raise ValueError("wrong chain")
        accounts = wallets(api, args.network)
        txs = Transactions(w3, output, args.network == "mainnet")
        artifact = json.loads(ARTIFACT.read_text())
        validate_build(api, artifact)
        if args.network == "mainnet":
            oracle = w3.eth.contract(address="0x4200000000000000000000000000000000000015", abi=[
                {"name": name, "type": "function", "stateMutability": "view", "inputs": [],
                 "outputs": [{"type": "uint32" if name.endswith("Scalar") else "uint64"}]}
                for name in ("operatorFeeScalar", "operatorFeeConstant")])
            if oracle.functions.operatorFeeScalar().call() or oracle.functions.operatorFeeConstant().call():
                raise ValueError("operator fees changed; update receipt accounting before running")
        balances_before = {role: w3.eth.get_balance(account.address) for role, account in accounts.items()}
        dump(output / "preflight.json", {"at_utc": utc(), "chain_id": expected_chain,
             "addresses": {k: v.address for k, v in accounts.items()}, "balances_wei": balances_before,
             "fee_planning_ceiling_wei": MAX_FEES, "artifact_sha256": hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()})
        if args.network == "mainnet":
            targets = {"provider": 100 * 10**12, "verifier": 350 * 10**12, "challenger": 80 * 10**12}
            for role, amount in topups({r: balances_before[r] for r in ROLES}, targets).items():
                txs.send(accounts["funding"], value=amount, to=accounts[role].address, operation="fund_" + role)
        factory = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"]["object"])
        deployed = txs.send(accounts["verifier"], factory.constructor(accounts["verifier"].address), operation="deploy")
        from web3.exceptions import BlockNotFound, Web3RPCError
        expected_runtime = bytes.fromhex(artifact["deployedBytecode"]["object"].removeprefix("0x"))
        for attempt in range(30):
            try:
                runtime = bytes(w3.eth.get_code(deployed.contractAddress, block_identifier=deployed.blockNumber))
            except (BlockNotFound, Web3RPCError):
                runtime = b""
            if runtime:
                if runtime != expected_runtime:
                    raise RuntimeError("deployed runtime differs from current build")
                break
            time.sleep(.5)
        else:
            raise RuntimeError("deployed runtime not readable at receipt block")
        escrow = recorded_escrow(api, w3, deployed.contractAddress, accounts, txs)
        escrow._observe_receipt(deployed)
        dump(output / "deployment.json", {"chain_id": expected_chain, "address": deployed.contractAddress,
             "deploy_block": deployed.blockNumber, "artifact_sha256": hashlib.sha256(ARTIFACT.read_bytes()).hexdigest(),
             **{role + "_addr": accounts[role].address for role in ROLES}})
        store_port, inbox_port = free_port(args.ports, taken), free_port(args.ports, taken)
        race_port = free_port(args.ports, taken) if args.network == "local" else None
        token = secrets.token_urlsafe(32)
        store_env = {**os.environ, "STORE_DB": str(output / "provider-store.sqlite"), "STORE_TOKEN": token}
        store_proc = stack.enter_context(child([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(store_port), "--no-access-log"], output / "store.log", store_env, ROOT / "packages/store"))
        store_url, inbox_url = f"http://127.0.0.1:{store_port}", f"http://127.0.0.1:{inbox_port}"
        ready(store_url, store_proc)
        verifier_env = {k: v for k, v in os.environ.items() if k not in {"STORE_URL", "STORE_TOKEN"}}
        command = [sys.executable, str(Path(__file__).resolve()), "receiver", "--rpc", rpc,
                   "--contract", deployed.contractAddress, "--database", str(output / "verifier-inbox.sqlite"), "--port", str(inbox_port)]
        receiver = stack.enter_context(child(command, output / "receiver.log", verifier_env))
        ready(inbox_url, receiver)
        import requests
        assert requests.get(store_url + "/sessions/0x00/records").status_code == 401
        assert requests.get(inbox_url + "/claims/1/evidence").status_code in (404, 405)
        inbox = api.EvidenceInbox(output / "verifier-inbox.sqlite", expected_chain, deployed.contractAddress)
        cases = run_cases(api, escrow, inbox, api.EvidenceClient(inbox_url), api.HttpStore(store_url, token=token),
                          accounts, txs, output, args.network, race_port)
        balances_after = {role: w3.eth.get_balance(account.address) for role, account in accounts.items()}
        result = {"at_utc": utc(), "network": args.network, "chain_id": expected_chain,
                  "contract": deployed.contractAddress, "cases_passed": len(cases),
                  "case_names": [case["case"] for case in cases], "transactions": len(txs.rows),
                  "fees_wei": txs.spent, "balances_after_wei": balances_after,
                  "private_store_rejects_unauthenticated": True, "verifier_process_has_store_credentials": False,
                  "verifier_wallet_roles_loaded": ["verifier"],
                  "fee_ceiling_scope": "preflight gas bound plus twice live L1 upper estimate and 5 microETH headroom; realized fees checked after every receipt; not a protocol cap on changing L1 prices",
                  "deadline_tests_time_advanced": args.network == "local", "status": "completed"}
        drift = [name for name, digest in hashes.items() if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
        result["source_drift"] = drift
        dump(output / "result.json", result)
        if drift:
            raise RuntimeError("source changed during experiment; retain as smoke only")
        print(json.dumps(result, indent=2))


def receiver(args):
    api = bootstrap()
    import uvicorn
    w3 = connection(api, args.rpc)
    contract = api.EscrowClient(w3, args.contract, json.loads(ARTIFACT.read_text())["abi"])
    inbox = api.EvidenceInbox(args.database, w3.eth.chain_id, args.contract)
    uvicorn.run(api.create_app(contract, inbox), host="127.0.0.1", port=args.port, access_log=False)


def adjudicate(args):
    if "STORE_TOKEN" in os.environ or "STORE_URL" in os.environ:
        raise RuntimeError("adjudicator must not receive provider store credentials")
    api = bootstrap()
    w3 = connection(api, args.rpc)
    expected = 8453 if args.network == "mainnet" else 31337
    if w3.eth.chain_id != expected:
        raise ValueError("wrong adjudicator chain")
    accounts = wallets(api, args.network, roles=("verifier",))
    txs = Transactions(w3, args.output, args.network == "mainnet")
    txs.label = args.case
    escrow = recorded_escrow(api, w3, args.contract, accounts, txs)
    # Never read state older than what the driver already confirmed (the challenge, the commitment).
    escrow._observe_receipt(SimpleNamespace(blockNumber=args.min_block))
    inbox = api.EvidenceInbox(args.database, expected, args.contract)
    for attempt in range(20):
        result = api.process_challenge(escrow, inbox, args.claim, accounts["verifier"].address)
        if result["violated"] is not None or not result.get("pending", True):
            break
        time.sleep(.5)
    dump(args.result, {"verdict": result, "wallet_roles_loaded": list(accounts),
                       "provider_store_configured": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    run = sub.add_parser("run")
    run.add_argument("--network", choices=("local", "mainnet"), default="local")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--execute-mainnet", action="store_true")
    run.add_argument("--ports", type=parse_ports, default=None,
                     help="inclusive loopback port range for the local chain and services, e.g. 38500-38599")
    serve = sub.add_parser("receiver")
    serve.add_argument("--rpc", required=True)
    serve.add_argument("--contract", required=True)
    serve.add_argument("--database", type=Path, required=True)
    serve.add_argument("--port", type=int, required=True)
    verify = sub.add_parser("adjudicate")
    verify.add_argument("--rpc", required=True)
    verify.add_argument("--contract", required=True)
    verify.add_argument("--database", type=Path, required=True)
    verify.add_argument("--claim", type=int, required=True)
    verify.add_argument("--case", required=True)
    verify.add_argument("--network", choices=("local", "mainnet"), required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--result", type=Path, required=True)
    verify.add_argument("--min-block", type=int, default=0)
    args = parser.parse_args()
    {"run": experiment, "receiver": receiver, "adjudicate": adjudicate}[args.mode](args)


if __name__ == "__main__":
    main()
