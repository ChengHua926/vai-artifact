"""Paired, fresh-process rerun of the archived native verifier scaling experiment.

Both arms use the actual current process_challenge and HTTP store. The only arm
difference is its check_prefixes binding. The SHA-pinned September 16 worker and
native-record expansion functions are executed unchanged; chain reads and
settlement are the worker's existing fixtures. No RPC or wallet configuration is
used. Run with the original Hermes Python 3.12 environment when available.
"""
from __future__ import annotations

import argparse
import ast
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
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
ORIGINAL_MEMBER = "scripts/practicality/measure_local.py"
ORIGINAL_SHA256 = "4445b92268c814c9f494eff10e302b7069fb556287ee0187dcfdc3ab3de00bb6"
CADENCES = ("final_only", "every_10_records")
IMPLEMENTATIONS = ("legacy", "streaming")
METRICS = ("total_seconds", "retrieval_seconds", "final_hash_seconds",
           "prefix_validation_seconds", "predicate_seconds", "cpu_seconds",
           "peak_rss_bytes", "peak_rss_growth_bytes")


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str) + "\n")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bootstrap(dependency_path=None):
    # Bind local packages directly, without importing any wallet/config helpers.
    if dependency_path is not None:
        sys.path.insert(0, str(dependency_path.resolve()))
    for package in ("commons", "sdk", "store", "verifier"):
        sys.path.insert(0, str(REPO / "packages" / package))
    from aa_commons import ActionRecord, params_hash, registry, trace_hash
    from aa_commons import trace
    from aa_sdk import HttpStore
    import aa_verifier
    for name, module in tuple(sys.modules.items()):
        if name.split(".", 1)[0] in {"aa_commons", "aa_sdk", "aa_verifier"}:
            if not Path(module.__file__).resolve().is_relative_to(REPO):
                raise RuntimeError(f"wrong checkout imported for {name}")
    from aa_verifier.prefixes import check_prefixes_streaming
    return SimpleNamespace(ActionRecord=ActionRecord, params_hash=params_hash,
                           registry=registry, trace_hash=trace_hash, trace=trace,
                           HttpStore=HttpStore, verifier=aa_verifier,
                           streaming=check_prefixes_streaming)


def read_original(archive):
    with tarfile.open(archive) as source:
        raw = source.extractfile(ORIGINAL_MEMBER).read()
    if hashlib.sha256(raw).hexdigest() != ORIGINAL_SHA256:
        raise RuntimeError("archived benchmark source does not match the published run")
    return raw.decode()


def archived_functions(archive, api):
    """Execute only these function definitions, never archived imports or main."""
    names = {"elapsed", "rss_bytes", "expanded_native_records", "verify_worker"}
    tree = ast.parse(read_original(archive))
    definitions = [node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in definitions} != names:
        raise RuntimeError("missing archived benchmark functions")
    namespace = {"json": json, "resource": resource, "sys": sys, "time": time,
                 "SimpleNamespace": SimpleNamespace, "dump": dump,
                 "ActionRecord": api.ActionRecord, "HttpStore": api.HttpStore,
                 "registry": api.registry, "trace_hash": api.trace_hash,
                 "check_prefixes": api.trace.check_prefixes}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), ORIGINAL_MEMBER, "exec"), namespace)
    return namespace


def assert_legacy_unchanged(archive, api):
    with tarfile.open(archive) as source:
        old = ast.parse(source.extractfile("packages/commons/aa_commons/trace.py").read())
    old_functions = {node.name: node for node in old.body if isinstance(node, ast.FunctionDef)}
    for name in ("trace_hash", "check_prefixes"):
        current = ast.parse(inspect.getsource(getattr(api.trace, name))).body[0]
        if ast.dump(current) != ast.dump(old_functions[name]):
            raise RuntimeError(f"legacy {name} differs from the archived implementation")


def worker(args):
    parsed = urlparse(args.store_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("worker store must be an explicit HTTP loopback URL")
    api = bootstrap(args.dependency_path)
    namespace = archived_functions(args.source_archive, api)
    selected = (api.trace.check_prefixes if args.implementation == "legacy"
                else api.streaming)
    namespace["check_prefixes"] = selected
    original_elapsed = namespace["elapsed"]
    final_digests = []

    def observe_elapsed(fn):
        value, seconds = original_elapsed(fn)
        # Observation is after the original phase timer, with no second hashing.
        # Only the archived measured_hash call returns a 32-byte hex string.
        if isinstance(value, str) and value.startswith("0x") and len(value) == 66:
            final_digests.append(value)
        return value, seconds

    namespace["elapsed"] = observe_elapsed
    namespace["verify_worker"](args)
    fixture = json.loads(args.fixture.read_text())
    row = json.loads(args.worker_output.read_text())
    if final_digests != [fixture["trace_hash"]]:
        raise AssertionError(f"unexpected final hashes: {final_digests}")
    row.update(implementation=args.implementation, worker_pid=os.getpid(),
               final_digest=final_digests[0], expected_final_digest=fixture["trace_hash"],
               fixture_sha256=sha256(args.fixture),
               prefix_function_sha256=hashlib.sha256(inspect.getsource(selected).encode()).hexdigest(),
               python=sys.version)
    dump(args.worker_output, row)


@contextmanager
def local_store(output):
    import requests
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    database = output / "trace-store.sqlite"
    command = [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
               "--port", str(port), "--no-access-log"]
    url = f"http://127.0.0.1:{port}"
    dump(output / "service-command.json", {"command": command, "STORE_DB": str(database)})
    with (output / "store.log").open("w") as log:
        child = subprocess.Popen(command, cwd=REPO / "packages/store",
                                 env={**os.environ, "STORE_DB": str(database)},
                                 stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if child.poll() is not None:
                    raise RuntimeError("local store exited; inspect store.log")
                try:
                    if requests.get(url + "/health", timeout=.5).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("local store failed readiness")
            yield url
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def prepare_fixtures(args, api, store_url):
    functions = archived_functions(args.source_archive, api)
    seed_path = args.fixtures / "native-seed-records.json"
    seed = json.loads(seed_path.read_text())
    store = api.HttpStore(store_url)
    params = {"destructive_tools": ["write_file", "patch", "terminal", "execute_code"],
              "authorization_mode": "invocation"}
    prepared = []
    for n in args.sizes:
        paths = [args.fixtures / f"scaling-fixture-{n}-{cadence}.json" for cadence in CADENCES]
        fixtures = [json.loads(path.read_text()) for path in paths]
        final_only, checkpointed = fixtures
        if {k: v for k, v in final_only.items() if k != "checkpoints"} != {
                k: v for k, v in checkpointed.items() if k != "checkpoints"}:
            raise AssertionError("paired archived fixtures differ beyond checkpoints")
        if final_only["checkpoints"] or len(checkpointed["checkpoints"]) != n // 10:
            raise AssertionError("unexpected archived checkpoint cadence")
        sid = final_only["session_id"]
        records = functions["expanded_native_records"](seed, n, sid)
        digest = api.trace_hash(records)
        if digest != final_only["trace_hash"] or api.params_hash(params) != final_only["params_hash"]:
            raise AssertionError("reconstructed native records or promise differs from archived roots")
        spec = api.registry.resolve_hash(final_only["predicate_hash"])
        if spec is None or spec.evaluate(records, params).violated:
            raise AssertionError("archived predicate is unavailable or reconstructed verdict changed")
        # Same POST/PUT store setup as the original, outside all worker timings.
        for record in records:
            store.append_record(sid, record.to_dict())
        store.put_promise(final_only["promise_id"], {"predicate": spec.spec_id, "params": params})
        reread = [api.ActionRecord.from_dict(record) for record in store.get_records(sid)]
        if api.trace_hash(reread) != digest:
            raise AssertionError("HTTP store roundtrip changed the archived native trace")
        # Checkpoint root agreement is established by both arms in every measured
        # worker. Do not run an extra quadratic validation before measurement.
        prepared.append({"records": n, "session_id": sid, "trace_hash": digest,
                         "predicate_hash": final_only["predicate_hash"],
                         "params_hash": final_only["params_hash"],
                         "stored_records": len(reread)})
        print(f"prepared original {n}-record native fixture", flush=True)
    return prepared


def schedule(sizes, repetitions, seed):
    rng = random.Random(seed)
    pairs = [(n, cadence, rep) for n in sizes for cadence in CADENCES for rep in range(repetitions)]
    rng.shuffle(pairs)
    runs = []
    for pair_position, (n, cadence, rep) in enumerate(pairs):
        order = rng.sample(IMPLEMENTATIONS, len(IMPLEMENTATIONS))
        for within_pair, implementation in enumerate(order):
            runs.append({"records": n, "cadence": cadence, "repetition": rep,
                         "implementation": implementation, "pair_position": pair_position,
                         "within_pair_order": within_pair, "order_position": len(runs)})
    return runs


def stats(values):
    ordered = sorted(values)
    at = (len(ordered) - 1) * .95
    lo = int(at)
    return {"n": len(values), "mean": statistics.mean(values),
            "median": statistics.median(values),
            "p95": ordered[lo] + (ordered[min(lo + 1, len(ordered) - 1)] - ordered[lo]) * (at - lo),
            "min": min(values), "max": max(values)}


def summarize(rows, metrics=METRICS):
    summary = {}
    for n, cadence in sorted({(row["records"], row["cadence"]) for row in rows}):
        by_arm = {arm: sorted([r for r in rows if r["records"] == n and
                              r["cadence"] == cadence and r["implementation"] == arm],
                             key=lambda r: r["repetition"]) for arm in IMPLEMENTATIONS}
        legacy, streaming = (by_arm[arm] for arm in IMPLEMENTATIONS)
        if not legacy or [r["repetition"] for r in legacy] != [r["repetition"] for r in streaming]:
            raise AssertionError("incomplete paired experiment")
        agreement = all(a["final_digest"] == b["final_digest"] and a["verdict"] == b["verdict"]
                        for a, b in zip(legacy, streaming))
        if not agreement:
            raise AssertionError("legacy and streaming digests/verdicts disagree")
        summary[f"{n}:{cadence}"] = {
            "arms": {arm: {metric: stats([row[metric] for row in subset]) for metric in metrics}
                     for arm, subset in by_arm.items()},
            "paired_legacy_minus_streaming": {
                metric: stats([a[metric] - b[metric] for a, b in zip(legacy, streaming)])
                for metric in metrics},
            "ratio_of_means_legacy_over_streaming": {
                metric: statistics.mean([a[metric] for a in legacy]) /
                        statistics.mean([b[metric] for b in streaming])
                for metric in metrics if statistics.mean([b[metric] for b in streaming]) != 0},
            "digest_and_verdict_agreement": agreement,
            "final_digest": legacy[0]["final_digest"], "verdict": legacy[0]["verdict"],
        }
    return summary


def source_manifest():
    sources = [Path(__file__).resolve()]
    for relative in ("packages/commons/aa_commons", "packages/sdk/aa_sdk",
                     "packages/verifier/aa_verifier"):
        sources.extend(sorted((REPO / relative).rglob("*.py")))
    sources.append(REPO / "packages/store/app.py")
    sources.append(REPO / "packages/verifier/pyproject.toml")
    return {str(path.relative_to(REPO)): sha256(path) for path in sources}


def hash_backend_manifest():
    from Crypto.Hash import keccak as legacy_keccak
    from eth_hash.auto import keccak as eth_keccak
    import sha3
    import _pysha3
    eth_keccak(b"")  # Initialize outside every worker and measured interval.
    legacy_files = [Path(legacy_keccak.__file__).resolve()]
    legacy_files.extend(sorted(legacy_files[0].parent.glob("_keccak*.so")))
    return {
        "legacy_backend": "Crypto.Hash.keccak (direct import in aa_commons.ids)",
        "legacy_backend_files": {str(path): sha256(path) for path in legacy_files},
        "auxiliary_eth_hash_backend": type(eth_keccak.hasher.__self__).__module__ + "." +
                                      type(eth_keccak.hasher.__self__).__qualname__,
        "streaming_backend": "sha3.keccak_256 (safe-pysha3)",
        "streaming_backend_files": {str(Path(module.__file__).resolve()): sha256(module.__file__)
                                    for module in (sha3, _pysha3)},
    }


def run(args):
    if args.repetitions < 1 or len(set(args.sizes)) != len(args.sizes):
        raise ValueError("positive repetitions and unique sizes required")
    if any(n not in (100, 1000, 10000) for n in args.sizes):
        raise ValueError("only archived fixture sizes 100, 1000, and 10000 are supported")
    if args.output is None:
        raise ValueError("--output is required")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    api = bootstrap(args.dependency_path)
    assert_legacy_unchanged(args.source_archive, api)
    inputs = args.output / "inputs"
    inputs.mkdir()
    archived_inputs = [args.fixtures / name for name in (
        "native-seed-records.json", "metadata.json", "scaling-raw.jsonl", "scaling-summary.json")]
    archived_inputs += [args.fixtures / f"scaling-fixture-{n}-{cadence}.json"
                        for n in args.sizes for cadence in CADENCES]
    archived_inputs.append(args.source_archive)
    input_hashes = {}
    for path in archived_inputs:
        input_hashes[str(path)] = sha256(path)
        shutil.copyfile(path, inputs / path.name)
    args.fixtures = inputs
    args.source_archive = inputs / args.source_archive.name
    sources_before = source_manifest()
    for relative in sources_before:
        target = args.output / "source-snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    metadata = {
        "command": [sys.executable, *sys.argv], "started_unix": time.time(),
        "platform": platform.platform(), "machine": platform.machine(),
        "python": sys.version, "executable": sys.executable, "repo": str(REPO),
        "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "git_status": subprocess.check_output(["git", "status", "--short"], cwd=REPO, text=True),
        "versions": {p: importlib.metadata.version(p) for p in
                     ("web3", "requests", "uvicorn", "fastapi", "eth-hash", "pycryptodome", "safe-pysha3")},
        "dependency_path": str(args.dependency_path.resolve()) if args.dependency_path else None,
        "hash_backends": hash_backend_manifest(),
        "seed": args.seed, "repetitions_per_cell_per_arm": args.repetitions,
        "source_sha256": sources_before, "archived_input_sha256": input_hashes,
        "original_worker_source_sha256": ORIGINAL_SHA256,
        "design": "shuffled pairs, shuffled arm order within pairs, one fresh sequential process per worker",
        "measurement_boundary": "unchanged archived verify_worker: actual process_challenge, actual HTTP retrieval, fixture chain reads, stub settlement",
        "observation_hook": "capture returned final hash after original phase timer; included in total time; no extra hashing",
        "peak_rss_scope": "fresh worker process lifetime; includes imports and fixture read; excludes separate store process",
        "blockchain_rpc": False, "transactions": False,
        "historical_comparison": "original September 16 results preserved under inputs; new legacy vs streaming is the paired causal comparison",
    }
    dump(args.output / "metadata.json", metadata)
    runs = schedule(args.sizes, args.repetitions, args.seed)
    dump(args.output / "schedule.json", runs)
    rows = []
    with local_store(args.output) as url:
        metadata["prepared_fixtures"] = prepare_fixtures(args, api, url)
        dump(args.output / "metadata.json", metadata)
        with tempfile.TemporaryDirectory(prefix="verifier-workers-") as scratch:
            for cell in runs:
                fixture = args.fixtures / f"scaling-fixture-{cell['records']}-{cell['cadence']}.json"
                worker_output = Path(scratch) / "worker.json"
                command = [sys.executable, str(Path(__file__).resolve()), "--mode", "worker",
                           "--fixture", str(fixture), "--worker-output", str(worker_output),
                           "--store-url", url, "--implementation", cell["implementation"],
                           "--source-archive", str(args.source_archive)]
                if args.dependency_path is not None:
                    command += ["--dependency-path", str(args.dependency_path.resolve())]
                subprocess.run(command, check=True, cwd=REPO)
                row = json.loads(worker_output.read_text())
                row.update(cell, command=command)
                rows.append(row)
                with (args.output / "paired-raw.jsonl").open("a") as output:
                    output.write(json.dumps(row) + "\n")
                print(f"{len(rows)}/{len(runs)} {cell['records']} {cell['cadence']} "
                      f"{cell['implementation']} rep {cell['repetition'] + 1}: "
                      f"total {row['total_seconds']:.6f}s; prefix {row['prefix_validation_seconds']:.6f}s", flush=True)
    summary = summarize(rows)
    dump(args.output / "paired-summary.json", summary)
    if source_manifest() != sources_before:
        raise RuntimeError("source changed during the benchmark; results must not be used")
    if any(sha256(path) != digest for path, digest in input_hashes.items()):
        raise RuntimeError("original archived input changed during the benchmark")
    metadata.update(completed_unix=time.time(), worker_count=len(rows),
                    all_digest_and_verdict_agreement=True, source_unchanged_during_run=True,
                    raw_sha256=sha256(args.output / "paired-raw.jsonl"),
                    summary_sha256=sha256(args.output / "paired-summary.json"))
    dump(args.output / "metadata.json", metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("run", "worker"), default="run")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--source-archive", type=Path,
                        default=FIXTURES / "local-measurement-source-278bd53.tar.gz")
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260921)
    parser.add_argument("--dependency-path", type=Path,
                        help="optional isolated target containing the verifier hash backend")
    parser.add_argument("--implementation", choices=IMPLEMENTATIONS)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--worker-output", type=Path)
    parser.add_argument("--store-url")
    args = parser.parse_args()
    if args.mode == "worker":
        if any(value is None for value in (args.implementation, args.fixture, args.worker_output, args.store_url)):
            parser.error("worker requires implementation, fixture, worker-output, and store-url")
        worker(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
