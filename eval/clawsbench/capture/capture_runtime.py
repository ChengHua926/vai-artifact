#!/usr/bin/env python3
"""In-container evidence capture for the ClawsBench GLM-5.2 corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ARTIFACT_ROOT = Path("/logs/artifacts")
OPENCLAW_BIN = Path("/opt/benchflow/bin/openclaw")
OPENCLAW_HOME = Path("/home/agent/.openclaw")
ENDPOINTS = ("state", "diff", "action_log")


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def fetch_endpoint(url: str, output: Path) -> dict[str, Any]:
    started_at = now()
    metadata: dict[str, Any] = {
        "url": url,
        "started_at": started_at,
        "finished_at": None,
        "http_status": None,
        "bytes": 0,
        "sha256": None,
        "error": None,
    }
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            metadata["http_status"] = response.status
        json.loads(body)
        atomic_write(output, body)
        metadata["bytes"] = len(body)
        metadata["sha256"] = sha256_bytes(body)
    except Exception as exc:
        metadata["error"] = f"{type(exc).__name__}: {exc}"
    metadata["finished_at"] = now()
    atomic_json(output.with_suffix(".meta.json"), metadata)
    return metadata


def capture_env0(phase: str, services: dict[str, int]) -> int:
    root = ARTIFACT_ROOT / "env0" / phase
    records: list[dict[str, Any]] = []
    for service, port in sorted(services.items()):
        for endpoint in ENDPOINTS:
            output = root / service / f"{endpoint}.json"
            record = fetch_endpoint(
                f"http://127.0.0.1:{port}/_admin/{endpoint}", output
            )
            records.append(
                {"service": service, "endpoint": endpoint, **record}
            )
    manifest = {
        "schema_version": 1,
        "phase": phase,
        "captured_at": now(),
        "services": services,
        "records": records,
        "ok": bool(records) and all(record["error"] is None for record in records),
    }
    atomic_json(root / "capture-manifest.json", manifest)
    print(json.dumps({"phase": phase, "ok": manifest["ok"], "records": len(records)}))
    return 0 if manifest["ok"] else 1


def _run_as_agent(argv: list[str]) -> subprocess.CompletedProcess[str]:
    path = f"/opt/benchflow/bin:/opt/benchflow/node/bin:{os.environ.get('PATH', '')}"
    return subprocess.run(
        [
            "runuser",
            "-u",
            "agent",
            "--",
            "env",
            "HOME=/home/agent",
            f"PATH={path}",
            *argv,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _copy_native_sessions(destination: Path) -> list[dict[str, Any]]:
    source = OPENCLAW_HOME / "agents/main/sessions"
    copied: list[dict[str, Any]] = []
    if not source.is_dir():
        return copied
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if not (
            path.name == "sessions.json"
            or path.name.endswith(".jsonl")
            or path.name.endswith(".trajectory-path.json")
        ):
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        data = target.read_bytes()
        copied.append(
            {
                "path": str(relative),
                "bytes": len(data),
                "sha256": sha256_bytes(data),
            }
        )
    return copied


def _parse_sessions(stdout: str) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start < 0 or end < start:
            raise
        parsed = json.loads(stdout[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("openclaw sessions --json returned a non-object")
    return parsed


def capture_openclaw(task_name: str) -> int:
    root = ARTIFACT_ROOT / "openclaw"
    raw_root = root / "raw"
    bundle_root = root / "bundle"
    errors: list[str] = []

    copied = _copy_native_sessions(raw_root)
    if not any(entry["path"].endswith(".jsonl") for entry in copied):
        errors.append("no native OpenClaw session JSONL found")

    version_result = _run_as_agent([str(OPENCLAW_BIN), "--version"])
    runtime = {
        "captured_at": now(),
        "task_name": task_name,
        "openclaw_version_stdout": version_result.stdout.strip(),
        "openclaw_version_stderr": version_result.stderr.strip(),
        "openclaw_version_rc": version_result.returncode,
        "node_version": subprocess.run(
            ["/opt/benchflow/node/bin/node", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip(),
        "native_files": copied,
    }

    sessions_result = _run_as_agent(
        [str(OPENCLAW_BIN), "sessions", "--all-agents", "--json"]
    )
    atomic_write(root / "sessions-list.stdout", sessions_result.stdout.encode())
    atomic_write(root / "sessions-list.stderr", sessions_result.stderr.encode())
    session_key: str | None = None
    if sessions_result.returncode != 0:
        errors.append(f"openclaw sessions failed with rc={sessions_result.returncode}")
    else:
        try:
            sessions_payload = _parse_sessions(sessions_result.stdout)
            atomic_json(root / "sessions-list.json", sessions_payload)
            sessions = sessions_payload.get("sessions")
            if isinstance(sessions, list) and sessions:
                newest = max(
                    (item for item in sessions if isinstance(item, dict)),
                    key=lambda item: (
                        item.get("updatedAt")
                        or item.get("updated_at")
                        or item.get("createdAt")
                        or ""
                    ),
                )
                candidate = newest.get("key")
                if isinstance(candidate, str) and candidate:
                    session_key = candidate
            if session_key is None:
                errors.append("sessions JSON contained no session key")
        except Exception as exc:
            errors.append(f"could not parse sessions JSON: {type(exc).__name__}: {exc}")

    if session_key is not None:
        export_result = _run_as_agent(
            [
                str(OPENCLAW_BIN),
                "sessions",
                "export-trajectory",
                "--session-key",
                session_key,
                "--workspace",
                "/app",
                "--output",
                "benchflow-capture",
                "--json",
            ]
        )
        atomic_write(root / "export.stdout", export_result.stdout.encode())
        atomic_write(root / "export.stderr", export_result.stderr.encode())
        if export_result.returncode != 0:
            errors.append(
                f"openclaw trajectory export failed with rc={export_result.returncode}"
            )
        else:
            source_bundle = (
                Path("/app/.openclaw/trajectory-exports/benchflow-capture")
            )
            if source_bundle.is_dir():
                shutil.copytree(source_bundle, bundle_root, dirs_exist_ok=True)
            else:
                errors.append(f"trajectory bundle missing at {source_bundle}")

    for source, destination in (
        (Path("/app/AGENTS.md"), root / "bootstrap/AGENTS.md"),
        (Path("/instruction.md"), root / "bootstrap/instruction.md"),
        (OPENCLAW_HOME / "openclaw.json", root / "runtime/openclaw.json"),
        (
            OPENCLAW_HOME / "exec-approvals.json",
            root / "runtime/exec-approvals.json",
        ),
    ):
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    runtime["session_key"] = session_key
    runtime["errors"] = errors
    runtime["ok"] = not errors
    atomic_json(root / "capture-manifest.json", runtime)
    print(json.dumps({"phase": "openclaw", "ok": not errors, "errors": errors}))
    return 0 if not errors else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    env_parser = subparsers.add_parser("env")
    env_parser.add_argument("--phase", required=True)
    env_parser.add_argument("--services-json", required=True)
    openclaw_parser = subparsers.add_parser("openclaw")
    openclaw_parser.add_argument("--task-name", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "env":
        services = json.loads(args.services_json)
        if not isinstance(services, dict):
            raise SystemExit("--services-json must decode to an object")
        return capture_env0(
            args.phase, {str(name): int(port) for name, port in services.items()}
        )
    return capture_openclaw(args.task_name)


if __name__ == "__main__":
    sys.exit(main())
