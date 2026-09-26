"""Repeat native recording timings against the authenticated current store.

Runs only local Anvil (31337), never a public RPC or model. Hermes retains the
September 16 runtime workload; OpenClaw uses its September 24 native driver.
Use --smoke for one three-write repetition; formal runs are 30 paired repetitions
of 100 one-KiB writes, plus one warmup per mode. STORE_TOKEN is generated when
absent and passed through environment only; no credential value is archived.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import asyncio
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import random
import resource
import secrets
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
# Set before importing _config; none of its wallet-reading helpers are called.
os.environ["AA_LOCAL"] = "1"
import _config as C
import native_pin  # operating_cost/native_pin.py
C.bootstrap_packages()
from aa_commons import ActionRecord, params_hash, registry, trace_hash
from aa_commons.trace import check_prefixes
from aa_sdk import Accountability, HttpStore
from aa_sdk.chain import EscrowClient
from web3 import Web3
import requests


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, default=str) + "\n")


def elapsed(fn):
    start = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - start


def rss_bytes():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def percentile(values, q):
    values = sorted(values)
    at = (len(values) - 1) * q
    lo = int(at)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (at - lo)


def stats(values):
    return {"n": len(values), "mean": statistics.mean(values),
            "median": statistics.median(values), "p95": percentile(values, .95),
            "min": min(values), "max": max(values)}


def paired_bootstrap(values, seed=20260916, samples=10000):
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(samples)]
    return {"mean": statistics.mean(values), "ci95": [percentile(means, .025), percentile(means, .975)]}


class Services:
    def __init__(self, args):
        self.args = args
        self.children = []
        self.logs = []

    def __enter__(self):
        args = self.args
        for port in (args.store_port, args.anvil_port):
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
        db = args.output / "trace-store.sqlite"
        if db.exists():
            raise RuntimeError("Use a fresh output directory; refusing to mix runs")
        commands = [
            ([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(args.store_port), "--no-access-log"], REPO / "packages/store", {"STORE_DB": str(db)}),
            ([str(Path.home() / ".foundry/bin/anvil"), "--host", "127.0.0.1", "--port", str(args.anvil_port), "--chain-id", "31337", "--silent"], REPO, {}),
        ]
        dump(args.output / "service-commands.json", [x[0] for x in commands])
        try:
            for name, (cmd, cwd, env) in zip(("store", "anvil"), commands):
                log = open(args.output / f"{name}.log", "w")
                self.logs.append(log)
                self.children.append(subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT))
            for _ in range(100):
                try:
                    if requests.get(args.store_url + "/health", timeout=.5).ok and Web3(Web3.HTTPProvider(args.rpc_url)).eth.chain_id == 31337:
                        assert_store_auth(args)
                        return self
                except Exception:
                    pass
                if any(p.poll() is not None for p in self.children):
                    raise RuntimeError("Local service exited; inspect logs")
                time.sleep(.1)
            raise RuntimeError("Local services failed readiness")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        for child in self.children:
            child.terminate()
        for child in self.children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for log in self.logs:
            log.close()


class TimedChain:
    """Instrument the real EscrowClient without replacing submission or reads."""
    def __init__(self, chain):
        self.chain = chain
        self.events = []

    def __getattr__(self, name):
        original = getattr(self.chain, name)
        if name not in {"checkpoint_trace", "commit_trace", "open_session", "register_promise"}:
            return original
        def call(*args, **kwargs):
            start = time.perf_counter()
            result = original(*args, **kwargs)
            end = time.perf_counter()
            event = {"method": name, "start": start, "observed": end, "seconds": end-start,
                     "session_id": args[1] if name != "register_promise" else None}
            if name == "checkpoint_trace":
                event["record_count"] = args[2]
            if name != "register_promise":
                event.update(tx_hash=result.transactionHash.hex(), gas_used=int(result.gasUsed),
                             block=int(result.blockNumber), effective_gas_price=int(result.effectiveGasPrice))
            self.events.append(event)
            return result
        return call


def native_setup(args):
    os.environ["HERMES_HOME"] = str(args.output / "hermes-home")
    os.environ["HERMES_INTERACTIVE"] = "1"
    os.environ["AA_SCOPE_PREFIX"] = str(args.output / "native-files") + "/"
    sys.path.insert(0, str(args.hermes))
    sys.path.insert(0, str(REPO / "integrations/hermes"))
    import aa_hermes
    import model_tools
    from hermes_cli.plugins import get_plugin_manager
    from acp_adapter.edit_approval import make_acp_edit_approval_requester, set_edit_approval_requester, reset_edit_approval_requester
    from acp.schema import AllowedOutcome
    manager = get_plugin_manager()
    manager._hooks, manager._middleware = {}, {}
    class Context:
        def register_hook(self, name, callback):
            manager._hooks.setdefault(name, []).append(callback)
        def register_middleware(self, name, callback):
            manager._middleware.setdefault(name, []).append(callback)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    async def answer(**kwargs):
        return SimpleNamespace(outcome=AllowedOutcome(outcome="selected", option_id="allow_once"))
    return SimpleNamespace(plugin=aa_hermes, dispatch=model_tools.handle_function_call,
        manager=manager, context=Context(), loop=loop, thread=thread,
        requester=lambda sid: make_acp_edit_approval_requester(answer, loop, sid),
        set_requester=set_edit_approval_requester, reset_requester=reset_edit_approval_requester)


def runtime(args, chain, services):
    import psutil
    native = native_setup(args)
    directory = args.output / "native-files"
    directory.mkdir()
    content = ("native Hermes operating_cost fixture\n" * 40)[:1024]
    workload = [{"path": str(directory / f"record-{i:04d}.txt"), "content": content} for i in range(args.actions)]
    dump(args.output / "native-workload.json", workload)
    rng = random.Random(args.seed)
    modes = ["capture_off", "http_store", "http_store_anvil"]
    rows = []
    procs = [psutil.Process(p.pid) for p in services.children]
    def service_cpu():
        return [p.cpu_times().user + p.cpu_times().system for p in procs]
    def one(mode, rep, position, warmup=False):
        for entry in workload:
            Path(entry["path"]).unlink(missing_ok=True)
        native.manager._hooks, native.manager._middleware = {}, {}
        sid = f"native-{'warmup' if warmup else rep}-{mode}"
        token = native.set_requester(native.requester(sid))
        acc = sess = None
        mark = len(chain.events)
        setup_start = time.perf_counter()
        if mode != "capture_off":
            native.plugin.register(native.context)
            acc, sess = native.plugin.begin_session(native_session_id=sid,
                store=HttpStore(args.store_url), chain=chain if mode == "http_store_anvil" else None,
                provider_addr=chain.w3.eth.accounts[1], party=chain.w3.eth.accounts[2], payout_wei=1)
        setup_seconds = time.perf_counter() - setup_start
        latency = []
        cpu_start = time.process_time()
        services_start = service_cpu()
        action_start = time.perf_counter()
        try:
            for i, entry in enumerate(workload):
                result, duration = elapsed(lambda: native.dispatch("write_file", entry, task_id="operating_cost-native", session_id=sid, tool_call_id=f"call-{i}"))
                latency.append(duration)
                parsed = json.loads(result)
                if parsed.get("error"):
                    raise AssertionError(parsed)
            action_seconds = time.perf_counter() - action_start
            action_cpu = time.process_time() - cpu_start
            final_start = time.perf_counter()
            summary = native.plugin.end_session(native_session_id=sid) if sess else None
            final_seconds = time.perf_counter() - final_start
            total_cpu = time.process_time() - cpu_start
            services_end = service_cpu()
            if sess and sess._worker:
                sess._worker.join(timeout=5)
            effects = [hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() for entry in workload]
            expected = hashlib.sha256(content.encode()).hexdigest()
            assert effects == [expected] * args.actions
            stored = HttpStore(args.store_url).get_records(sess.session_id) if sess else []
            if sess:
                assert len(stored) == args.actions * 4, (len(stored), args.actions)
                assert all(not verdict.violated for verdict in acc.self_check(sess.records).values())
                assert trace_hash([ActionRecord.from_dict(r) for r in stored]) == summary["trace_hash"]
                if not (args.output / "native-seed-records.json").exists():
                    dump(args.output / "native-seed-records.json", stored)
            stored_bytes = 0
            if sess:
                with sqlite3.connect(args.output / "trace-store.sqlite") as conn:
                    stored_bytes = conn.execute("SELECT COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM records WHERE session_id=?", (sess.session_id,)).fetchone()[0]
            events = chain.events[mark:]
            row = {"repetition": rep, "order_position": position, "mode": mode, "warmup": warmup,
                   "native_actions": args.actions, "capture_records": len(stored),
                   "setup_seconds": setup_seconds, "action_seconds": action_seconds,
                   "action_latencies_seconds": latency, "finalization_seconds": final_seconds,
                   "total_action_plus_finalization_seconds": action_seconds + final_seconds,
                   "client_action_cpu_seconds": action_cpu, "client_total_cpu_seconds": total_cpu,
                   "store_cpu_seconds": services_end[0] - services_start[0],
                   "anvil_cpu_seconds": services_end[1] - services_start[1],
                   "stored_payload_bytes": stored_bytes, "database_allocated_bytes": (args.output / "trace-store.sqlite").stat().st_size if (args.output / "trace-store.sqlite").exists() else 0,
                   "effects_sha256": hashlib.sha256("".join(effects).encode()).hexdigest(),
                   "effect_bytes": args.actions * len(content.encode()), "session_id": sess.session_id if sess else None,
                   "transactions": events, "summary": summary}
            with open(args.output / "runtime-raw.jsonl", "a") as out:
                out.write(json.dumps(row) + "\n")
            return row
        finally:
            native.reset_requester(token)
    try:
        for position, mode in enumerate(modes):
            one(mode, -1, position, True)
        for rep in range(args.repetitions):
            order = rng.sample(modes, len(modes))
            for position, mode in enumerate(order):
                rows.append(one(mode, rep, position))
            print(f"runtime repetition {rep+1}/{args.repetitions} complete", flush=True)
        assert len({row["effects_sha256"] for row in rows}) == 1
        summary = {}
        for mode in modes:
            subset = [r for r in rows if r["mode"] == mode]
            summary[mode] = {key: stats([r[key] for r in subset]) for key in (
                "setup_seconds", "action_seconds", "finalization_seconds", "total_action_plus_finalization_seconds",
                "client_action_cpu_seconds", "client_total_cpu_seconds", "store_cpu_seconds", "anvil_cpu_seconds", "stored_payload_bytes")}
            summary[mode]["pooled_action_latency_seconds"] = stats([t for r in subset for t in r["action_latencies_seconds"]])
            summary[mode]["checkpoint_counts"] = stats([sum(e["method"] == "checkpoint_trace" for e in r["transactions"]) for r in subset])
        baseline = {r["repetition"]: r for r in rows if r["mode"] == "capture_off"}
        summary["paired_differences_vs_capture_off"] = {mode: {
            key: paired_bootstrap([r[key] - baseline[r["repetition"]][key] for r in rows if r["mode"] == mode], args.seed)
            for key in ("action_seconds", "total_action_plus_finalization_seconds")}
            for mode in modes[1:]}
        dump(args.output / "runtime-summary.json", summary)
    finally:
        native.loop.call_soon_threadsafe(native.loop.stop)
        native.thread.join(timeout=2)
        native.loop.close()


def assert_store_auth(args):
    """Probe real read/write routes outside timing without adding store records."""
    record = {"seq": 1, "session_id": "auth-probe", "tool": "read", "args": {}, "ts": 1}
    routes = [("GET", "/sessions/auth-probe/records", None),
              ("POST", "/sessions/auth-probe/records", record),
              ("GET", "/promises/auth-probe", None),
              ("PUT", "/promises/auth-probe", {"predicate": "probe", "params": {}}),
              ("GET", "/inventory", None)]
    checks = []
    for method, route, payload in routes:
        for credentials, headers in (("missing", {}),
                ("incorrect", {"Authorization": "Bearer " + secrets.token_urlsafe(32)})):
            result = requests.request(method, args.store_url + route, json=payload,
                                      headers=headers, timeout=5)
            if result.status_code != 401:
                raise AssertionError(f"store {method} {route} allowed {credentials} credentials: {result.status_code}")
            checks.append({"method": method, "route": route, "credentials": credentials,
                           "status_code": result.status_code})
    assert HttpStore(args.store_url).get_records("auth-probe") == []
    inventory = requests.get(args.store_url + "/inventory", timeout=5,
        headers={"Authorization": "Bearer " + os.environ["STORE_TOKEN"]})
    inventory.raise_for_status()
    assert inventory.json() == {"promise_count": 0, "record_count": 0}
    dump(args.output / "store-auth-checks.json", {"denied_checks": checks,
         "authenticated_sdk_read": "passed", "initial_inventory": inventory.json(),
         "timing": "before warmups; no records or promises added"})


def source_paths(harness):
    paths = {Path(__file__).resolve(), REPO / "scripts/_config.py", REPO / "packages/store/app.py",
             REPO / "packages/sdk/aa_sdk/__init__.py", REPO / "packages/sdk/aa_sdk/chain.py",
             REPO / "packages/sdk/aa_sdk/evidence.py",
             REPO / "contracts/src/Escrow.sol", Path(C.ARTIFACT)}
    for directory in [REPO / "packages/commons/aa_commons",
                      REPO / f"integrations/{harness}"]:
        paths.update(p for p in directory.rglob("*")
                     if p.is_file() and p.suffix in {".py", ".ts", ".json", ".patch"}
                     and "node_modules" not in p.parts and "__pycache__" not in p.parts)
    if harness == "openclaw":
        paths.update(p for p in (REPO / "operating_cost").glob("*openclaw*") if p.is_file())
    return sorted(paths)


def source_hashes(paths):
    return {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def verify_no_token_in_artifacts(directory):
    token = os.environ["STORE_TOKEN"].encode()
    for path in directory.rglob("*"):
        if path.is_file() and token in path.read_bytes():
            raise AssertionError(f"credential found in artifact: {path.relative_to(directory)}")


def run_openclaw(args):
    import measure_openclaw as measurement
    original_services = measurement.services

    @contextmanager
    def authenticated_services(config):
        with original_services(config):
            assert_store_auth(config)
            yield

    measurement.services = authenticated_services
    args.network, args.keystore_dir = "local", None
    measurement.run(args)
    raw = json.loads((args.output / "native-results.json").read_text())
    import analyze_openclaw
    for row in raw["jobs"]:
        analyze_openclaw.validate_job(row)
    if not args.smoke:
        analyze_openclaw.local(args.output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=["hermes", "openclaw"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--store-port", type=int, default=18848)
    parser.add_argument("--anvil-port", type=int, default=18847)
    parser.add_argument("--helper-port", type=int, default=18849)
    args = parser.parse_args()
    outer = args.output.resolve()
    if outer.exists() and any(outer.iterdir()):
        raise ValueError("use a fresh output directory")
    if sys.version_info[:3] != (3, 12, 13):
        raise ValueError("repeat the measurements with the original Python 3.12.13 runtime")
    token_origin = "environment" if os.environ.get("STORE_TOKEN") else "generated for this local run"
    os.environ.setdefault("STORE_TOKEN", secrets.token_urlsafe(32))
    if len(os.environ["STORE_TOKEN"]) < 24:
        raise ValueError("measurement STORE_TOKEN must contain at least 24 characters")
    args.output = outer / "measurements"
    args.hermes = REPO.parent / "hermes-agent"
    args.seed = 20260916 if args.harness == "hermes" else 20260924
    args.actions, args.repetitions = (3, 1) if args.smoke else (100, 30)
    args.store_url = f"http://127.0.0.1:{args.store_port}"
    args.rpc_url = f"http://127.0.0.1:{args.anvil_port}"
    args.helper_url = f"http://127.0.0.1:{args.helper_port}"
    native_root = REPO.parent / ("hermes-agent" if args.harness == "hermes" else "openclaw")
    if not native_pin.matches(native_root, args.harness):
        raise ValueError("native harness must be its upstream.json commit with the release patch applied")
    native_revision = native_pin.describe(args.harness)
    paths = source_paths(args.harness)
    hashes = source_hashes(paths)
    outer.mkdir(parents=True, exist_ok=True)
    for source in paths:
        target = outer / "source" / source.relative_to(REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    native_files = {p["path"] for p in json.loads((REPO / f"integrations/{args.harness}/upstream.json").read_text())["observed_paths"]}
    native_files.add("tools/file_tools.py" if args.harness == "hermes" else "src/agents/sessions/tools/write.ts")
    native_hashes = {}
    for name in sorted(native_files):
        source = native_root / name
        if source.is_file():
            native_hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
            target = outer / "native-source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    metadata = {"command": [sys.executable, *sys.argv], "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python": sys.version, "harness": args.harness,
        "native_revision": native_revision,
        "prototype_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "protocol_source_sha256": hashes, "native_source_sha256": native_hashes,
        "store_authentication": {"enabled": True, "token_source": token_origin,
                                 "transport": "STORE_TOKEN environment; Bearer HTTP header", "token_archived": False},
        "mode": "smoke" if args.smoke else "formal", "seed": args.seed,
        "paired_repetitions": args.repetitions, "warmups": 3, "writes_per_session": args.actions,
        "payload_bytes": 1024, "module_origins": C.verify_package_origins(),
        "timing_scope": "native write loop plus session finalization; setup, model, human wait, and post-run validation excluded",
        "network": "loopback Anvil only; chain 31337", "status": "started",
        "versions": {p: importlib.metadata.version(p) for p in ("web3", "requests", "uvicorn", "fastapi", "psutil", "agent-client-protocol", "safe-pysha3")},
        "original_hermes_runner_sha256": "4445b92268c814c9f494eff10e302b7069fb556287ee0187dcfdc3ab3de00bb6"}
    dump(outer / "refresh-manifest.json", metadata)
    try:
        if args.harness == "hermes":
            args.output.mkdir()
            with Services(args) as services:
                w3 = Web3(Web3.HTTPProvider(args.rpc_url))
                assert w3.eth.chain_id == 31337
                chain = TimedChain(EscrowClient.deploy(w3, C.ARTIFACT, w3.eth.accounts[0], w3.eth.accounts[3]))
                runtime(args, chain, services)
                dump(args.output / "chain-events.json", chain.events)
        else:
            run_openclaw(args)
        if source_hashes(paths) != hashes:
            raise AssertionError("source files changed during measurement; results cannot be attributed to the initial snapshot")
        metadata.update(status="completed", module_origins=C.verify_package_origins())
    except BaseException as exc:
        metadata.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        metadata["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        dump(outer / "refresh-manifest.json", metadata)
        verify_no_token_in_artifacts(outer)
    print(f"completed {args.harness} {metadata['mode']}: {outer}", flush=True)


if __name__ == "__main__":
    main()
