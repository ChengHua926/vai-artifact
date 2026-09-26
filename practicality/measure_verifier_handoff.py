"""Refresh verifier scaling on frozen native traces through signed HTTP evidence delivery.

Signing, HTTP delivery/receiver authentication/durable acknowledgment, and verifier processing
are timed separately in fresh processes. Their sum includes transfer of all evidence bytes.
Chain reads and settlement are fixture operations, as in the archived experiment. Loopback
delivery does not estimate Internet latency. Provider store retrieval and process startup are
outside these intervals. No RPC endpoint, wallet configuration, or real transactions are used.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import random
import resource
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

import compare_verifier_prefixes as original

REPO = original.REPO
CHAIN_ID = 31337
ESCROW_ADDRESS = "0x" + "12" * 20
CLAIM_ID = 1
FILED_AT = 1000     # fixture challengedAt: the evidence deadline is FILED_AT + EVIDENCE_WINDOW
CHAIN_TIME = 1002   # fixture latest block timestamp, inside the evidence deadline
# Public deterministic benchmark key, with no funded wallet or production role.
BENCHMARK_KEY = "0x" + "37" * 32
PARAMS = {"destructive_tools": ["write_file", "patch", "terminal", "execute_code"],
          "authorization_mode": "invocation"}
METRICS = ("signing_seconds", "delivery_seconds", "verification_seconds", "total_seconds",
           "inbox_read_seconds", "final_hash_seconds", "params_hash_seconds",
           "prefix_validation_seconds", "predicate_seconds", "verifier_cpu_seconds",
           "verifier_peak_rss_bytes", "verifier_peak_rss_growth_bytes", "receiver_peak_rss_bytes",
           "combined_service_peak_rss_bytes", "combined_service_verification_seconds")
# Paired prefix comparison (--mode compare-prefixes): the September 21 design and worker phase
# definitions on the V2 evidence path. Sep 21 names: total_seconds = verification_seconds,
# cpu_seconds = verifier_cpu_seconds, peak_rss(_growth)_bytes = verifier_peak_rss(_growth)_bytes;
# the V2 inbox read replaces HTTP store retrieval, which no longer exists.
PREFIX_METRICS = ("verification_seconds", "inbox_read_seconds", "final_hash_seconds", "params_hash_seconds",
                  "prefix_validation_seconds", "predicate_seconds", "verifier_cpu_seconds",
                  "verifier_peak_rss_bytes", "verifier_peak_rss_growth_bytes")
PREFIX_BOUNDARY = (
    "Fresh sequential worker per run. Evidence is already in the verifier inbox (signed V2 envelope saved before "
    "all workers; delivery excluded). Total verification is one actual aa_verifier.process_challenge call over the "
    "inbox with fixture chain reads and stub settlement. Phase timers: inbox read, final trace hash, parameter hash, "
    "prefix validation, predicate evaluation. CPU is process time over the call; peak RSS is the worker lifetime "
    "high-water mark (imports and fixture read included; no separate store process exists); growth is peak minus "
    "the high-water mark before the call. The only arm difference is the worker-local aa_verifier.check_prefixes "
    "binding: streaming = the verifier's own check_prefixes_streaming, legacy = aa_commons.trace.check_prefixes.")


def rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def elapsed(fn):
    before = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - before


def api_for(args):
    return original.bootstrap(args.dependency_path)


class FixtureEscrow:
    """Accepted-claim fixture only; preserves all archived roots and record identities.

    V2 chain view: challenges(id) is (sessionId, promiseId, challenger, bond, status,
    challengedAt). The claim is open, the session's final commitment is on chain and fixture
    chain time is inside the evidence deadline, so the receiver accepts the first submission.
    """
    def __init__(self, fixture, provider):
        self.fixture, self.provider = fixture, provider
        self.w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=CHAIN_ID,
                                    get_block=lambda _tag: {"timestamp": CHAIN_TIME}))
        self.contract = SimpleNamespace(address=ESCROW_ADDRESS)

    def get_challenge(self, cid):
        assert cid == CLAIM_ID
        return (bytes.fromhex(self.fixture["session_id"][2:]), self.fixture["records"],
                "0x" + "34" * 20, 1, 1, FILED_AT)

    def get_promise(self, pid):
        assert pid == self.fixture["records"]
        f = self.fixture
        return (self.provider, bytes.fromhex(f["predicate_hash"][2:]),
                bytes.fromhex(f["params_hash"][2:]), 1, 1, 1, 0)

    def get_session(self, sid):
        assert sid == self.fixture["session_id"]
        return (self.provider, "0x" + "34" * 20, bytes.fromhex(self.fixture["trace_hash"][2:]), 1, 1, True)

    def get_checkpoints(self, sid):
        assert sid == self.fixture["session_id"]
        return self.fixture["checkpoints"]

    def submit_verdict(self, _sender, cid, _violated):
        assert cid == CLAIM_ID
        return {"paidToChallenger": 0}


class ArchivedFixtureEscrow(FixtureEscrow):
    """V1 seven-field challenge view for the archived September 16 verifier only.

    That verifier unpacks respondedAt; it runs off the clock as a verdict cross-check.
    """
    def get_challenge(self, cid):
        return (*super().get_challenge(cid), CHAIN_TIME)


def reconstructed_records(args, api, fixture):
    definitions = original.archived_functions(args.source_archive, api)
    seed = json.loads((args.fixtures / "native-seed-records.json").read_text())
    records = definitions["expanded_native_records"](seed, fixture["records"], fixture["session_id"])
    if api.trace_hash(records) != fixture["trace_hash"] or api.params_hash(PARAMS) != fixture["params_hash"]:
        raise AssertionError("reconstructed evidence differs from archived commitments")
    spec = api.registry.resolve_hash(fixture["predicate_hash"])
    if spec is None or spec.evaluate(records, PARAMS).violated:
        raise AssertionError("frozen predicate or native record result changed")
    return records, spec


def old_verdict(args, api, fixture, records, spec, provider):
    """Run archived pre-handoff verifier off the clock; never call a private HTTP store."""
    with tarfile.open(args.source_archive) as archive:
        source = archive.extractfile("packages/verifier/aa_verifier/__init__.py").read()
    namespace = {"__name__": "archived_benchmark_verifier"}
    exec(compile(source, "archived-verifier.py", "exec"), namespace)
    store = SimpleNamespace(get_records=lambda _sid: [record.to_dict() for record in records],
                            get_promise=lambda _pid: {"predicate": spec.spec_id, "params": PARAMS})
    return namespace["process_challenge"](ArchivedFixtureEscrow(fixture, provider),
                                          store, CLAIM_ID, "verifier")


def server(args):
    api = api_for(args)
    from eth_account import Account
    from aa_verifier.inbox import EvidenceInbox, create_app
    import uvicorn
    fixture = json.loads(args.fixture.read_text())
    inbox = EvidenceInbox(args.database, CHAIN_ID, ESCROW_ADDRESS)
    app = create_app(FixtureEscrow(fixture, Account.from_key(BENCHMARK_KEY).address), inbox)

    @app.get("/benchmark-memory")
    def memory():
        return {"peak_rss_bytes": rss_bytes()}

    @app.post("/benchmark-verify")
    def combined_process():
        # Benchmark-only trigger after the provider received its delivery receipt. The real
        # service revisits open claims every poll; this fixture does not send transactions.
        chain = FixtureEscrow(fixture, Account.from_key(BENCHMARK_KEY).address)
        verdict, duration = elapsed(lambda: api.verifier.process_challenge(chain, inbox, CLAIM_ID, "verifier"))
        return {"verdict": verdict, "verification_seconds": duration, "peak_rss_bytes": rss_bytes()}

    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


def deliver(args):
    api_for(args)
    from eth_account import Account
    from aa_sdk.evidence import EvidenceClient, create_envelope
    import requests
    fixture = json.loads(args.fixture.read_text())
    provider_data = json.loads(args.provider_data.read_text())
    account = Account.from_key(BENCHMARK_KEY)
    chain = FixtureEscrow(fixture, account.address)
    client = EvidenceClient(args.url)
    envelope, signing = elapsed(lambda: create_envelope(chain, CLAIM_ID, provider_data["records"],
                         provider_data["params"], account))
    receipt, delivery = elapsed(lambda: client.submit(envelope))
    # Size uses requests' actual JSON-body preparation, outside the reported intervals.
    body = requests.Request("POST", args.url, json=envelope).prepare().body
    original.dump(args.worker_output, {"signing_seconds": signing, "delivery_seconds": delivery,
                  "submitted_body_bytes": len(body), "receipt": receipt,
                  "envelope_version": envelope["payload"]["version"],
                  "envelope_payload_keys": sorted(envelope["payload"]),
                  "provider_peak_rss_bytes": rss_bytes(), "provider_pid": os.getpid()})


def verify(args):
    api = api_for(args)
    from eth_account import Account
    from aa_verifier.inbox import EvidenceInbox
    fixture = json.loads(args.fixture.read_text())
    phases = {key: 0.0 for key in ("inbox_read_seconds", "final_hash_seconds", "params_hash_seconds",
                                   "prefix_validation_seconds", "predicate_seconds")}
    digests = []

    def measured(name, fn, *values):
        result, seconds = elapsed(lambda: fn(*values))
        phases[name] += seconds
        return result

    class Inbox(EvidenceInbox):
        def get(self, cid):
            return measured("inbox_read_seconds", super().get, cid)

    def hash_records(records):
        digest = measured("final_hash_seconds", api.trace_hash, records)
        digests.append(digest)
        return digest

    class Registry:
        def resolve_hash(self, digest):
            spec = api.registry.resolve_hash(digest)
            if spec is None:
                return None
            return SimpleNamespace(spec_id=spec.spec_id,
                evaluate=lambda records, params: measured("predicate_seconds", spec.evaluate, records, params))

    if api.verifier.check_prefixes is not api.streaming:
        raise AssertionError("the current verifier no longer binds the streaming prefix checker")
    # Worker-local rebinding only; aa_commons and aa_verifier sources are unchanged.
    prefix_check = api.trace.check_prefixes if args.prefix_implementation == "legacy" else api.streaming
    api.verifier.trace_hash = hash_records
    api.verifier.params_hash = lambda params: measured("params_hash_seconds", api.params_hash, params)
    api.verifier.check_prefixes = lambda records, points, sid: measured(
        "prefix_validation_seconds", prefix_check, records, points, sid)
    api.verifier.registry = Registry()
    inbox = Inbox(args.database, CHAIN_ID, ESCROW_ADDRESS)
    chain = FixtureEscrow(fixture, Account.from_key(BENCHMARK_KEY).address)
    rss_before, cpu_before = rss_bytes(), time.process_time()
    verdict, duration = elapsed(lambda: api.verifier.process_challenge(chain, inbox, CLAIM_ID, "verifier"))
    cpu_seconds, peak = time.process_time() - cpu_before, rss_bytes()
    if verdict["violated"] is not False or digests != [fixture["trace_hash"]]:
        raise AssertionError(f"new evidence path changed the frozen verdict: {verdict}")
    original.dump(args.worker_output, {"verification_seconds": duration, **phases,
        "verifier_cpu_seconds": cpu_seconds, "verifier_peak_rss_bytes": peak,
        "verifier_peak_rss_before_processing_bytes": rss_before,
        "verifier_peak_rss_growth_bytes": max(0, peak - rss_before), "verdict": verdict,
        "final_digest": digests[0], "verifier_pid": os.getpid(),
        "prefix_implementation": args.prefix_implementation,
        "prefix_function_sha256": hashlib.sha256(inspect.getsource(prefix_check).encode()).hexdigest()})


def subprocess_command(args, mode, *options):
    command = [sys.executable, str(Path(__file__).resolve()), "--mode", mode, *map(str, options)]
    if args.dependency_path:
        command += ["--dependency-path", str(args.dependency_path.resolve())]
    return command


@contextmanager
def receiver(args, cell_dir, fixture_path):
    import requests
    with socket.socket() as probe:
        # Default: an OS-assigned free port. A fixed --receiver-port is reused by successive
        # receivers (as uvicorn does, tolerating TIME_WAIT) but must have no active listener.
        if args.receiver_port:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", args.receiver_port or 0))
        port = probe.getsockname()[1]
    database = cell_dir / "evidence.sqlite"
    command = subprocess_command(args, "server", "--fixture", fixture_path,
                                  "--database", database, "--port", port)
    env = {key: value for key, value in os.environ.items() if key != "STORE_TOKEN"}
    with (cell_dir / "receiver.log").open("w") as log:
        child = subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                if child.poll() is not None:
                    raise RuntimeError(f"receiver exited; inspect {cell_dir / 'receiver.log'}")
                try:
                    if requests.get(url + "/health", timeout=.5).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.05)
            else:
                raise RuntimeError("receiver startup timed out")
            yield url, database, child.pid
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def sources():
    manifest = original.source_manifest()
    # Include fixture reconstruction helper and the new driver in the immutable run snapshot.
    for path in (Path(__file__).resolve(), Path(original.__file__).resolve()):
        manifest[str(path.relative_to(REPO))] = original.sha256(path)
    return manifest


def run(args):
    if args.output is None or args.repetitions < 1:
        raise ValueError("--output and positive repetitions are required")
    if any(n not in (100, 1000, 10000) for n in args.sizes) or len(set(args.sizes)) != len(args.sizes):
        raise ValueError("choose unique frozen sizes from 100, 1000, 10000")
    api = api_for(args)
    from eth_account import Account
    from aa_sdk.evidence import DOMAIN, EVIDENCE_WINDOW, VERSION
    import requests
    original.assert_legacy_unchanged(args.source_archive, api)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "inputs"
    inputs.mkdir()
    paths = [args.fixtures / "native-seed-records.json", args.source_archive]
    paths += [args.fixtures / f"scaling-fixture-{n}-{cadence}.json"
              for n in args.sizes for cadence in args.cadences]
    input_hashes = {str(path.resolve()): original.sha256(path) for path in paths}
    for path in paths:
        shutil.copyfile(path, inputs / path.name)
    args.fixtures, args.source_archive = inputs, inputs / args.source_archive.name
    source_hashes = sources()
    for relative in source_hashes:
        target = args.output / "source-snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    metadata = {"command": [sys.executable, *sys.argv], "started_unix": time.time(),
        "python": sys.version, "executable": sys.executable, "platform": platform.platform(),
        "source_sha256": source_hashes, "archived_input_sha256": input_hashes,
        "original_worker_source_sha256": original.ORIGINAL_SHA256,
        "repetitions_per_cell": args.repetitions, "seed": args.seed,
        "chain_id": CHAIN_ID, "escrow": ESCROW_ADDRESS, "real_transactions": False,
        "measurement_boundary": __doc__,
        "memory_scope": "Fresh verifier process peak/growth and receiver peak are separate. Combined service peak is measured by running the same verifier in the receiver process after delivery. Provider peak is also retained.",
        "fixture_identity_mapping": "Original scaling-N promise string maps to uint N; session ID, records, all committed roots, checkpoints, predicate and parameters unchanged.",
        "claim_flow": {"protocol": "V2 verifier-owned evidence deadline", "envelope_domain": DOMAIN.decode().strip(),
                       "envelope_version": VERSION, "evidence_window_seconds": EVIDENCE_WINDOW,
                       "fixture_filed_at": FILED_AT, "fixture_chain_time": CHAIN_TIME,
                       "fixture_claim": "open, final trace committed, not closed without evidence"},
        "response": "No on-chain response exists; the signed delivery is the provider's entire response. The benchmark starts verification after the durable receipt; the verifier service's polling delay and verdict transaction are excluded.",
        "old_verifier_source": "Archived September 16 process_challenge, invoked outside timings on identical reconstructed evidence through a seven-field V1 challenge fixture.",
        "versions": {name: importlib.metadata.version(name) for name in
                     ("web3", "requests", "uvicorn", "fastapi", "pycryptodome", "safe-pysha3")},
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True)}
    original.dump(args.output / "metadata.json", metadata)
    prepared = {}
    for n in args.sizes:
        for cadence in args.cadences:
            fixture_path = inputs / f"scaling-fixture-{n}-{cadence}.json"
            fixture = json.loads(fixture_path.read_text())
            if len(fixture["checkpoints"]) != (0 if cadence == "final_only" else n // 10):
                raise AssertionError("unexpected frozen checkpoint count")
            records, spec = reconstructed_records(args, api, fixture)
            verdict = old_verdict(args, api, fixture, records, spec, Account.from_key(BENCHMARK_KEY).address)
            if verdict["violated"] is not False:
                raise AssertionError(f"archived verifier outcome changed: {verdict}")
            provider_data = inputs / f"provider-data-{n}-{cadence}.json"
            original.dump(provider_data, {"records": [record.to_dict() for record in records],
                                         "params": PARAMS})
            prepared[n, cadence] = (fixture_path, fixture, provider_data, verdict)
    jobs = [{"records": n, "cadence": c, "repetition": r} for n in args.sizes
            for c in args.cadences for r in range(args.repetitions)]
    random.Random(args.seed).shuffle(jobs)
    original.dump(args.output / "schedule.json", jobs)
    rows = []
    for position, job in enumerate(jobs):
        fixture_path, fixture, provider_data, old = prepared[job["records"], job["cadence"]]
        cell_dir = args.output / f"worker-{position:03d}"
        cell_dir.mkdir()
        with receiver(args, cell_dir, fixture_path) as (url, database, receiver_pid):
            delivery_path, verification_path = cell_dir / "delivery.json", cell_dir / "verification.json"
            subprocess.run(subprocess_command(args, "deliver", "--fixture", fixture_path,
                "--provider-data", provider_data, "--url", url, "--worker-output", delivery_path), check=True, cwd=REPO)
            receiver_memory = requests.get(url + "/benchmark-memory", timeout=10).json()["peak_rss_bytes"]
            subprocess.run(subprocess_command(args, "verify", "--fixture", fixture_path,
                "--database", database, "--worker-output", verification_path), check=True, cwd=REPO)
            combined_response = requests.post(url + "/benchmark-verify", timeout=120)
            combined_response.raise_for_status()
            combined = combined_response.json()
            row = {**job, **json.loads(delivery_path.read_text()), **json.loads(verification_path.read_text()),
                "receiver_peak_rss_bytes": receiver_memory, "receiver_pid": receiver_pid,
                "combined_service_peak_rss_bytes": combined["peak_rss_bytes"],
                "combined_service_verification_seconds": combined["verification_seconds"],
                "combined_service_verdict": combined["verdict"],
                "old_verdict": old, "checkpoint_count": len(fixture["checkpoints"]),
                "fixture_sha256": original.sha256(fixture_path), "order_position": position}
            row["total_seconds"] = row["signing_seconds"] + row["delivery_seconds"] + row["verification_seconds"]
            if any(row["verdict"].get(key) != old.get(key) for key in ("predicate", "violated", "seq", "reason")):
                raise AssertionError("old and new verifier results differ")
            if row["combined_service_verdict"] != row["verdict"]:
                raise AssertionError("combined verifier service changed the verdict")
            if row["envelope_version"] != VERSION or "predicate" in row["envelope_payload_keys"]:
                raise AssertionError("delivery did not use the current evidence envelope")
            rows.append(row)
            with (args.output / "handoff-raw.jsonl").open("a") as target:
                target.write(json.dumps(row) + "\n")
            print(f"{position + 1}/{len(jobs)} {job['records']} {job['cadence']}: "
                  f"delivery {row['delivery_seconds']:.6f}s; verify {row['verification_seconds']:.6f}s", flush=True)
    summary = {f"{n}:{cadence}": {metric: original.stats([row[metric] for row in rows
               if row["records"] == n and row["cadence"] == cadence]) for metric in METRICS}
               for n in args.sizes for cadence in args.cadences}
    original.dump(args.output / "handoff-summary.json", summary)
    if sources() != source_hashes or any(original.sha256(path) != digest for path, digest in input_hashes.items()):
        raise RuntimeError("source or frozen inputs changed during benchmark; results must not be used")
    metadata.update(completed_unix=time.time(), all_digest_and_verdict_agreement=True,
                    source_unchanged_during_run=True, workers=len(rows),
                    raw_sha256=original.sha256(args.output / "handoff-raw.jsonl"),
                    summary_sha256=original.sha256(args.output / "handoff-summary.json"))
    original.dump(args.output / "metadata.json", metadata)


def compare_prefixes(args):
    """September 21 paired legacy/streaming prefix comparison on the current V2 verifier."""
    if args.output is None or args.repetitions < 1:
        raise ValueError("--output and positive repetitions are required")
    if any(n not in (100, 1000, 10000) for n in args.sizes) or len(set(args.sizes)) != len(args.sizes):
        raise ValueError("choose unique frozen sizes from 100, 1000, 10000")
    if set(args.cadences) != set(original.CADENCES):
        raise ValueError("compare-prefixes pairs both cadences for every size; do not narrow --cadences")
    api = api_for(args)
    from eth_account import Account
    from aa_sdk.evidence import DOMAIN, EVIDENCE_WINDOW, VERSION, create_envelope, recover_provider
    from aa_verifier.inbox import EvidenceInbox
    original.assert_legacy_unchanged(args.source_archive, api)
    if api.verifier.check_prefixes is not api.streaming:
        raise AssertionError("the current verifier no longer binds the streaming prefix checker")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.output / "inputs"
    inputs.mkdir()
    # Same archived inputs as the September 21 run, copied and hash-checked before and after.
    paths = [args.fixtures / name for name in (
        "native-seed-records.json", "metadata.json", "scaling-raw.jsonl", "scaling-summary.json")]
    paths += [args.fixtures / f"scaling-fixture-{n}-{cadence}.json"
              for n in args.sizes for cadence in original.CADENCES]
    paths.append(args.source_archive)
    input_hashes = {str(path.resolve()): original.sha256(path) for path in paths}
    for path in paths:
        shutil.copyfile(path, inputs / path.name)
    args.fixtures, args.source_archive = inputs, inputs / args.source_archive.name
    source_hashes = sources()
    for relative in source_hashes:
        target = args.output / "source-snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    metadata = {"command": [sys.executable, *sys.argv], "started_unix": time.time(),
        "platform": platform.platform(), "machine": platform.machine(),
        "python": sys.version, "executable": sys.executable, "repo": str(REPO),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True),
        "versions": {name: importlib.metadata.version(name) for name in
                     ("web3", "requests", "uvicorn", "fastapi", "eth-hash", "pycryptodome", "safe-pysha3")},
        "dependency_path": str(args.dependency_path.resolve()) if args.dependency_path else None,
        "hash_backends": original.hash_backend_manifest(),
        "seed": args.seed, "repetitions_per_cell_per_arm": args.repetitions,
        "source_sha256": source_hashes, "archived_input_sha256": input_hashes,
        "original_worker_source_sha256": original.ORIGINAL_SHA256,
        "design": "September 21 design: shuffled (size, cadence, repetition) pairs, shuffled arm order within pairs, one fresh sequential process per worker",
        "measurement_boundary": PREFIX_BOUNDARY, "metrics": PREFIX_METRICS,
        "claim_flow": {"protocol": "V2 verifier-owned evidence deadline", "envelope_domain": DOMAIN.decode().strip(),
                       "envelope_version": VERSION, "evidence_window_seconds": EVIDENCE_WINDOW,
                       "fixture_filed_at": FILED_AT, "fixture_chain_time": CHAIN_TIME},
        "differences_from_sep21": "The evidence read is the verifier's own SQLite inbox; V2 has no provider-store retrieval, so HTTP retrieval is absent. The inbox handle is opened before the timed call, as the verifier service opens it once. Parameter hashing has its own timer and remains inside total. The worker imports the V2 inbox module.",
        "fixture_identity_mapping": "Original scaling-N promise string maps to uint N; session ID, records, all committed roots, checkpoints, predicate and parameters unchanged.",
        "blockchain_rpc": False, "transactions": False}
    original.dump(args.output / "metadata.json", metadata)
    account = Account.from_key(BENCHMARK_KEY)
    prepared = {}
    for n in args.sizes:
        fixtures = {cadence: json.loads((inputs / f"scaling-fixture-{n}-{cadence}.json").read_text())
                    for cadence in original.CADENCES}
        final_only, checkpointed = fixtures["final_only"], fixtures["every_10_records"]
        if ({k: v for k, v in final_only.items() if k != "checkpoints"}
                != {k: v for k, v in checkpointed.items() if k != "checkpoints"}):
            raise AssertionError("paired archived fixtures differ beyond checkpoints")
        if final_only["checkpoints"] or len(checkpointed["checkpoints"]) != n // 10:
            raise AssertionError("unexpected archived checkpoint cadence")
        records, _spec = reconstructed_records(args, api, final_only)
        evidence = [record.to_dict() for record in records]
        for cadence, fixture in fixtures.items():
            # The receiver's authenticate-then-save step, outside all workers and timings.
            envelope = create_envelope(FixtureEscrow(fixture, account.address), CLAIM_ID, evidence, PARAMS, account)
            provider = recover_provider(envelope)
            if provider != account.address:
                raise AssertionError("benchmark evidence envelope does not authenticate")
            database = args.output / "inboxes" / f"inbox-{n}-{cadence}.sqlite"
            inbox = EvidenceInbox(database, CHAIN_ID, ESCROW_ADDRESS)
            receipt = inbox.save(CLAIM_ID, envelope, provider, CHAIN_TIME)
            reread = [api.ActionRecord.from_dict(item) for item in inbox.get(CLAIM_ID)["payload"]["records"]]
            if api.trace_hash(reread) != fixture["trace_hash"]:
                raise AssertionError("inbox roundtrip changed the archived native trace")
            prepared[n, cadence] = {"fixture": inputs / f"scaling-fixture-{n}-{cadence}.json",
                                    "database": database, "receipt": receipt,
                                    "checkpoints": len(fixture["checkpoints"])}
        print(f"prepared {n}-record inboxes", flush=True)
    metadata["prepared_inboxes"] = {f"{n}:{cadence}": {"database": str(item["database"]), "receipt": item["receipt"],
                                    "checkpoints": item["checkpoints"]} for (n, cadence), item in prepared.items()}
    original.dump(args.output / "metadata.json", metadata)
    runs = original.schedule(args.sizes, args.repetitions, args.seed)
    original.dump(args.output / "schedule.json", runs)
    (args.output / "workers").mkdir()
    rows = []
    for cell in runs:
        item = prepared[cell["records"], cell["cadence"]]
        worker_output = args.output / "workers" / f"worker-{cell['order_position']:03d}.json"
        command = subprocess_command(args, "verify", "--fixture", item["fixture"], "--database", item["database"],
                                     "--worker-output", worker_output,
                                     "--prefix-implementation", cell["implementation"])
        subprocess.run(command, check=True, cwd=REPO)
        row = json.loads(worker_output.read_text())
        if row["prefix_implementation"] != cell["implementation"]:
            raise AssertionError("worker ran a different prefix implementation")
        row.update(cell, checkpoint_count=item["checkpoints"],
                   fixture_sha256=original.sha256(item["fixture"]), command=command)
        rows.append(row)
        with (args.output / "paired-raw.jsonl").open("a") as target:
            target.write(json.dumps(row) + "\n")
        print(f"{len(rows)}/{len(runs)} {cell['records']} {cell['cadence']} {cell['implementation']} "
              f"rep {cell['repetition'] + 1}: total {row['verification_seconds']:.6f}s; "
              f"prefix {row['prefix_validation_seconds']:.6f}s", flush=True)
    summary = original.summarize(rows, PREFIX_METRICS)   # asserts digest/verdict agreement per pair
    original.dump(args.output / "paired-summary.json", summary)
    if sources() != source_hashes or any(original.sha256(path) != digest for path, digest in input_hashes.items()):
        raise RuntimeError("source or frozen inputs changed during benchmark; results must not be used")
    metadata.update(completed_unix=time.time(), worker_count=len(rows),
                    all_digest_and_verdict_agreement=True, source_unchanged_during_run=True,
                    raw_sha256=original.sha256(args.output / "paired-raw.jsonl"),
                    summary_sha256=original.sha256(args.output / "paired-summary.json"))
    original.dump(args.output / "metadata.json", metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("run", "compare-prefixes", "server", "deliver", "verify"), default="run",
                        help="run: evidence handling timing; compare-prefixes: paired legacy/streaming prefix check "
                             "(always both cadences)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures", type=Path, default=original.FIXTURES)
    parser.add_argument("--source-archive", type=Path,
        default=original.FIXTURES / "local-measurement-source-278bd53.tar.gz")
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--cadences", nargs="+", choices=original.CADENCES, default=list(original.CADENCES))
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--seed", type=int, help="default 20260925 (run) or 20260921 (compare-prefixes)")
    parser.add_argument("--prefix-implementation", choices=original.IMPLEMENTATIONS, default="streaming",
                        help="verify worker only: aa_verifier.check_prefixes binding inside this process")
    parser.add_argument("--dependency-path", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--provider-data", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--worker-output", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--port", type=int)
    parser.add_argument("--receiver-port", type=int,
                        help="fixed loopback port for every fresh receiver; default is OS-assigned")
    args = parser.parse_args()
    if args.seed is None:
        args.seed = 20260921 if args.mode == "compare-prefixes" else 20260925
    {"run": run, "compare-prefixes": compare_prefixes, "server": server, "deliver": deliver,
     "verify": verify}[args.mode](args)


if __name__ == "__main__":
    main()
