#!/usr/bin/env python3
"""Clean, resumable ClawsBench runner for OpenClaw + GLM-5.2."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


if __package__:
    from .paths import resolve_research_root
else:
    from paths import resolve_research_root


CAPTURE_DIR = Path(__file__).resolve().parent
CLAWS_DIR = CAPTURE_DIR.parent
PROTOTYPE_ROOT = CLAWS_DIR.parents[1]
RESEARCH_ROOT = resolve_research_root(PROTOTYPE_ROOT)
BENCHFLOW_ROOT = RESEARCH_ROOT / "benchflow"
ENV0_ROOT = RESEARCH_ROOT / "env0"
META_PATH = CAPTURE_DIR / "meta/assistant_v1.md"
CAPTURE_SCRIPT = CAPTURE_DIR / "capture_runtime.py"
STANDARD60_MANIFEST = ENV0_ROOT / "tasks/STANDARD60_MANIFEST.txt"
ENVIRONMENT_MANIFEST = ENV0_ROOT / "tasks/_manifests/env-0.toml"
ENV_FILE = PROTOTYPE_ROOT / "eval/.env"

MODEL = "openrouter/z-ai/glm-5.2"
OPENROUTER_MODEL = "z-ai/glm-5.2"
NODE_VERSION = "24.15.0"
OPENCLAW_VERSION = "2026.7.1"
OPENCLAW_NPM_INTEGRITY = (
    "sha512-ge/Xss99CHAjPL/ikmH/UFoiOrjcxDB4sW3y9mhyCD+dYW3wzV7TKbAVdkrX"
    "FgAG2d2BjpJofP97zUZ+umxo8g=="
)
META_SHA256 = "279141b795d557ecdad7c69ae29a4ede0ad827b1fb909288c6485d78928e286a"
STANDARD60_SHA256 = "69fd9e22a53aa90bf2fe74be822d98685a9816a279d23042e24296fe5238f7f7"
SMOKE_TASKS = (
    "gdoc-edit-append-status",
    "multi-misread-approval-scope",
)
SERVICE_PORTS = {
    "mock-auth": 9000,
    "mock-gmail": 9001,
    "mock-gcal": 9002,
    "mock-gdrive": 9003,
    "mock-gdoc": 9004,
    "mock-slack": 9005,
    "mock-discord": 9006,
    "mock-stripe": 9007,
}
REQUIRED_BUNDLE_FILES = (
    "manifest.json",
    "events.jsonl",
    "session-branch.json",
)
CAPTURE_PHASES = ("initial", "terminal", "post_verifier")
MIN_BALANCE_USD = 2.0
CONTAINER_CAPTURE_SCRIPT = "/opt/benchflow/clawsbench_capture.py"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def git_state(root: Path) -> dict[str, Any]:
    def run_git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()

    return {
        "path": str(root),
        "commit": run_git("rev-parse", "HEAD"),
        "describe": run_git("describe", "--tags", "--always", "--dirty"),
        "dirty": bool(run_git("status", "--porcelain")),
    }


def load_standard60(env0_root: Path = ENV0_ROOT) -> list[str]:
    manifest = env0_root / "tasks/STANDARD60_MANIFEST.txt"
    tasks = [
        line.strip()
        for line in manifest.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(tasks) != 60 or len(set(tasks)) != 60 or tasks != sorted(tasks):
        raise RuntimeError("Standard60 manifest must contain 60 sorted unique tasks")
    missing = [name for name in tasks if not (env0_root / "tasks" / name).is_dir()]
    if missing:
        raise RuntimeError(f"Standard60 task directories missing: {missing}")
    if sha256_file(manifest) != STANDARD60_SHA256:
        raise RuntimeError("Standard60 manifest hash does not match the pinned snapshot")
    return tasks


def select_tasks(mode: str, env0_root: Path = ENV0_ROOT) -> list[str]:
    standard = load_standard60(env0_root)
    if mode == "standard60":
        return standard
    if mode == "smoke":
        missing = [name for name in SMOKE_TASKS if name not in standard]
        if missing:
            raise RuntimeError(f"smoke tasks are not in Standard60: {missing}")
        return list(SMOKE_TASKS)
    raise ValueError(f"unknown mode: {mode}")


def _task_frontmatter(task_dir: Path) -> dict[str, Any]:
    text = (task_dir / "task.md").read_text()
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if match is None:
        raise RuntimeError(f"{task_dir}/task.md has no YAML frontmatter")
    value = yaml.safe_load(match.group(1))
    if not isinstance(value, dict):
        raise RuntimeError(f"{task_dir}/task.md frontmatter is not an object")
    return value


def task_prompt(task_dir: Path) -> str:
    text = (task_dir / "task.md").read_text()
    match = re.search(r"(?im)^##\s+prompt\s*$\n(.*)\Z", text, flags=re.DOTALL)
    if match is None:
        raise RuntimeError(f"{task_dir}/task.md has no prompt section")
    return match.group(1).strip()


def active_services(task_dir: Path) -> dict[str, int]:
    frontmatter = _task_frontmatter(task_dir)
    try:
        names = frontmatter["benchflow"]["env0"]["services"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"{task_dir.name} does not declare benchflow.env0.services") from exc
    if not isinstance(names, list) or not names:
        raise RuntimeError(f"{task_dir.name} has an empty env0 service list")
    unknown = sorted(set(names) - set(SERVICE_PORTS))
    if unknown:
        raise RuntimeError(f"{task_dir.name} declares unknown env0 services: {unknown}")
    return {name: SERVICE_PORTS[name] for name in sorted(set(names))}


def pin_openclaw_install_command(command: str) -> str:
    replacements = (
        ("BF_NODE_VERSION=22.20.0", f"BF_NODE_VERSION={NODE_VERSION}"),
        ("openclaw@latest", f"openclaw@{OPENCLAW_VERSION}"),
    )
    pinned = command
    for old, new in replacements:
        count = pinned.count(old)
        if count != 1:
            raise RuntimeError(
                f"clean OpenClaw installer must contain {old!r} exactly once; found {count}"
            )
        pinned = pinned.replace(old, new)
    return pinned


def register_pinned_openclaw() -> dict[str, Any]:
    from benchflow.agents.registry import (
        AGENT_INSTALLERS,
        AGENT_LAUNCH,
        AGENTS,
    )

    core = AGENTS["openclaw"]
    pinned_command = pin_openclaw_install_command(core.install_cmd)
    pinned = replace(
        core,
        install_cmd=pinned_command,
        description=(
            "OpenClaw via the stock BenchFlow ACP shim; runtime-pinned for "
            "the ClawsBench capture corpus"
        ),
    )
    AGENTS["openclaw"] = pinned
    AGENT_INSTALLERS["openclaw"] = pinned.install_cmd
    AGENT_LAUNCH["openclaw"] = pinned.launch_cmd
    return {
        "node_version": NODE_VERSION,
        "openclaw_version": OPENCLAW_VERSION,
        "install_command_sha256": sha256_bytes(pinned.install_cmd.encode()),
        "launch_command_sha256": sha256_bytes(pinned.launch_cmd.encode()),
    }


def ensure_run_lock(path: Path, spec: dict[str, Any]) -> str:
    canonical = json.dumps(spec, indent=2, sort_keys=True) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"run lock is corrupt: {path}") from exc
        if existing != spec:
            raise RuntimeError(
                f"configuration drift detected for {path}; use a new run root"
            )
        return "matched"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(canonical)
    temporary.replace(path)
    return "created"


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if (
            "__pycache__" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
        ):
            continue
        digest.update(str(relative).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def docker_image_identity(reference: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{json .RepoDigests}}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    digests: list[str] = []
    if completed.returncode == 0:
        try:
            parsed = json.loads(completed.stdout)
            if isinstance(parsed, list):
                digests = [str(value) for value in parsed]
        except json.JSONDecodeError:
            pass
    return {"reference": reference, "repo_digests": digests}


def build_run_spec(
    mode: str,
    tasks: list[str],
    *,
    concurrency: int,
    build_concurrency: int,
    image_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompts = {}
    task_hashes = {}
    services = {}
    for name in tasks:
        task_dir = ENV0_ROOT / "tasks" / name
        prompts[name] = sha256_bytes(task_prompt(task_dir).encode())
        task_hashes[name] = _tree_hash(task_dir)
        services[name] = active_services(task_dir)
    return {
        "schema_version": 1,
        "mode": mode,
        "task_count": len(tasks),
        "tasks": tasks,
        "task_tree_sha256": task_hashes,
        "task_prompt_sha256": prompts,
        "task_services": services,
        "model": MODEL,
        "provider_model": OPENROUTER_MODEL,
        "agent": "openclaw",
        "agent_runtime": {
            "node_version": NODE_VERSION,
            "openclaw_version": OPENCLAW_VERSION,
            "openclaw_npm_integrity": OPENCLAW_NPM_INTEGRITY,
        },
        "harness": {
            "environment": "docker",
            "concurrency": concurrency,
            "build_concurrency": build_concurrency,
            "sandbox_user": "agent",
            "skill_mode": "no-skill",
            "skills_dir": None,
            "meta_injection": "/app/AGENTS.md",
            "automatic_retries": 0,
            "loop_strategy": None,
        },
        "meta_sha256": sha256_file(META_PATH),
        "standard60_manifest_sha256": sha256_file(STANDARD60_MANIFEST),
        "environment_manifest_sha256": sha256_file(ENVIRONMENT_MANIFEST),
        "capture_runtime_sha256": sha256_file(CAPTURE_SCRIPT),
        "runner_sha256": sha256_file(Path(__file__)),
        "repositories": {
            "benchflow": git_state(BENCHFLOW_ROOT),
            "env0": git_state(ENV0_ROOT),
        },
        "base_image": image_identity
        or {"reference": "ghcr.io/benchflow-ai/env0:0.2.0", "repo_digests": []},
    }


def _first_jsonl(path: Path) -> bool:
    try:
        lines = [line for line in path.read_text(errors="replace").splitlines() if line]
        if not lines:
            return False
        for line in lines:
            json.loads(line)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def _stable_env_payload(path: Path) -> Any:
    value = json.loads(path.read_bytes())
    if isinstance(value, dict):
        value = dict(value)
        value.pop("timestamp", None)
    return value


def validate_rollout_capture(
    rollout: Path,
    *,
    expected_task: str,
    services: dict[str, int],
    expected_prompt: str,
) -> dict[str, Any]:
    errors: list[str] = []

    def require(relative: str) -> Path:
        path = rollout / relative
        if not path.is_file():
            errors.append(f"missing {relative}")
        return path

    result_path = require("result.json")
    prompts_path = require("prompts.json")
    acp_path = require("trajectory/acp_trajectory.jsonl")
    llm_path = require("trajectory/llm_trajectory.jsonl")
    generation_stats_path = require("artifacts/openrouter-generations.json")
    require("verifier/reward.json")
    capture_status_path = require("artifacts/capture-status.json")
    for name in REQUIRED_BUNDLE_FILES:
        require(f"artifacts/openclaw/bundle/{name}")

    raw_jsonls = sorted((rollout / "artifacts/openclaw/raw").rglob("*.jsonl"))
    if not raw_jsonls:
        errors.append("missing native OpenClaw session JSONL")
    for path in raw_jsonls:
        if not _first_jsonl(path):
            errors.append(f"invalid native JSONL: {path.relative_to(rollout)}")

    for phase in CAPTURE_PHASES:
        manifest = require(f"artifacts/env0/{phase}/capture-manifest.json")
        if manifest.is_file():
            try:
                value = json.loads(manifest.read_text())
                if value.get("ok") is not True:
                    errors.append(f"env0 {phase} capture manifest is not ok")
            except json.JSONDecodeError:
                errors.append(f"invalid env0 {phase} capture manifest")
        for service in services:
            for endpoint in ("state", "diff", "action_log"):
                data = require(
                    f"artifacts/env0/{phase}/{service}/{endpoint}.json"
                )
                require(
                    f"artifacts/env0/{phase}/{service}/{endpoint}.meta.json"
                )
                if data.is_file():
                    try:
                        json.loads(data.read_bytes())
                    except json.JSONDecodeError:
                        errors.append(
                            f"invalid JSON: {data.relative_to(rollout)}"
                        )
    for service in services:
        for endpoint in ("state", "diff", "action_log"):
            terminal = (
                rollout
                / "artifacts/env0/terminal"
                / service
                / f"{endpoint}.json"
            )
            post_verifier = (
                rollout
                / "artifacts/env0/post_verifier"
                / service
                / f"{endpoint}.json"
            )
            if (
                terminal.is_file()
                and post_verifier.is_file()
            ):
                try:
                    if _stable_env_payload(terminal) != _stable_env_payload(
                        post_verifier
                    ):
                        errors.append(f"verifier mutated {service} {endpoint}")
                except json.JSONDecodeError:
                    pass

    result: dict[str, Any] = {}
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
            if result.get("task_name") != expected_task:
                errors.append("result task_name mismatch")
            if result.get("model") != MODEL:
                errors.append("result model mismatch")
            if result.get("skill_mode") != "no-skill":
                errors.append("result skill_mode is not no-skill")
            if result.get("n_skill_invocations") != 0:
                errors.append("result records skill invocations")
            if result.get("rewards") is None:
                errors.append("result has no verifier reward")
            if result.get("verifier_error") is not None:
                errors.append("result has verifier_error")
        except json.JSONDecodeError:
            errors.append("invalid result.json")

    if prompts_path.is_file():
        try:
            prompts = json.loads(prompts_path.read_text())
            if prompts != [expected_prompt]:
                errors.append("prompts.json does not equal the canonical task prompt")
            if any(META_PATH.read_text() in str(prompt) for prompt in prompts):
                errors.append("meta was concatenated into a user prompt")
        except json.JSONDecodeError:
            errors.append("invalid prompts.json")

    if capture_status_path.is_file():
        try:
            status = json.loads(capture_status_path.read_text())
            for phase in ("bootstrap", *CAPTURE_PHASES, "openclaw"):
                if (status.get(phase) or {}).get("ok") is not True:
                    errors.append(f"capture status is not ok for {phase}")
        except json.JSONDecodeError:
            errors.append("invalid capture-status.json")

    if acp_path.is_file() and not _first_jsonl(acp_path):
        errors.append("invalid or empty ACP trajectory JSONL")
    if llm_path.is_file():
        request_count = 0
        try:
            for line in llm_path.read_text(errors="replace").splitlines():
                if not line.strip():
                    continue
                exchange = json.loads(line)
                body = ((exchange.get("request") or {}).get("body") or {})
                if not isinstance(body, dict) or "model" not in body:
                    continue
                request_count += 1
                if body.get("model") != OPENROUTER_MODEL:
                    errors.append(
                        f"provider request model mismatch: {body.get('model')!r}"
                    )
                messages = body.get("messages") or []
                system_text = "\n".join(
                    str(message.get("content") or "")
                    for message in messages
                    if isinstance(message, dict) and message.get("role") == "system"
                )
                user_text = "\n".join(
                    str(message.get("content") or "")
                    for message in messages
                    if isinstance(message, dict) and message.get("role") == "user"
                )
                if META_PATH.read_text() not in system_text:
                    errors.append("exact meta is absent from provider system message")
                if META_PATH.read_text() in user_text:
                    errors.append("meta appears in a provider user message")
                if "<available_skills>" in system_text or "# Agent Skill" in system_text:
                    errors.append("provider system message exposes skills")
            if request_count == 0:
                errors.append("provider trajectory contains no model requests")
        except json.JSONDecodeError:
            errors.append("invalid provider LLM trajectory JSONL")

    reconciled_cost: float | None = None
    cost_complete: bool | None = None
    cost_coverage: str | None = None
    if generation_stats_path.is_file():
        try:
            generation_stats = json.loads(generation_stats_path.read_text())
            if generation_stats.get("ok") is not True:
                errors.append("OpenRouter generation-stat reconciliation is not ok")
            value = generation_stats.get("total_cost_usd")
            if isinstance(value, (int, float)):
                reconciled_cost = float(value)
            else:
                errors.append("OpenRouter generation stats have no total cost")
            if isinstance(generation_stats.get("cost_complete"), bool):
                cost_complete = generation_stats["cost_complete"]
            coverage = generation_stats.get("cost_coverage")
            if isinstance(coverage, str):
                cost_coverage = coverage
        except json.JSONDecodeError:
            errors.append("invalid OpenRouter generation stats")

    return {
        "ok": not errors,
        "task": expected_task,
        "rollout": str(rollout),
        "errors": errors,
        "result_error": result.get("error"),
        "reward": (result.get("rewards") or {}).get("reward"),
        "cost_usd": reconciled_cost
        if reconciled_cost is not None
        else (result.get("agent_result") or {}).get("cost_usd"),
        "cost_complete": cost_complete,
        "cost_coverage": cost_coverage,
        "total_tokens": (result.get("agent_result") or {}).get("total_tokens"),
    }


def _read_dotenv_key(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("\"'")
    return None


def load_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY") or _read_dotenv_key(
        ENV_FILE, "OPENROUTER_API_KEY"
    )
    if not key:
        raise RuntimeError(f"OPENROUTER_API_KEY is absent from the environment and {ENV_FILE}")
    return key


def _openrouter_request(
    url: str,
    key: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://clawsbench.benchflow.ai/",
            "X-Title": "Agent Accountability ClawsBench",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"OpenRouter preflight failed with HTTP {exc.code}: {body[:500]}") from exc
    value = json.loads(body)
    if not isinstance(value, dict):
        raise RuntimeError("OpenRouter returned a non-object response")
    return value


def openrouter_preflight(key: str) -> dict[str, Any]:
    key_info = _openrouter_request("https://openrouter.ai/api/v1/key", key)
    credits_info = _openrouter_request("https://openrouter.ai/api/v1/credits", key)
    completion = _openrouter_request(
        "https://openrouter.ai/api/v1/chat/completions",
        key,
        payload={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": "Reply OK"}],
            "temperature": 0,
            "max_tokens": 1,
        },
    )
    key_data = key_info.get("data") or {}
    credits_data = credits_info.get("data") or {}
    remaining = key_data.get("limit_remaining")
    if remaining is None:
        total = credits_data.get("total_credits")
        used = credits_data.get("total_usage")
        if isinstance(total, (int, float)) and isinstance(used, (int, float)):
            remaining = float(total) - float(used)
    if not isinstance(remaining, (int, float)):
        raise RuntimeError("OpenRouter did not expose a numeric remaining balance")
    if float(remaining) < MIN_BALANCE_USD:
        raise RuntimeError(
            f"OpenRouter remaining balance ${float(remaining):.2f} is below "
            f"the ${MIN_BALANCE_USD:.2f} reserve"
        )
    if not completion.get("choices"):
        raise RuntimeError("exact-model OpenRouter preflight returned no choices")
    usage = completion.get("usage") or {}
    return {
        "checked_at": utc_now(),
        "requested_model": OPENROUTER_MODEL,
        "response_model": completion.get("model"),
        "provider": completion.get("provider"),
        "response_id": completion.get("id"),
        "remaining_usd": round(float(remaining), 6),
        "preflight_usage": {
            key: usage.get(key)
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cost",
            )
            if usage.get(key) is not None
        },
    }


def reconcile_openrouter_generations(rollout: Path, key: str) -> dict[str, Any]:
    trajectory = rollout / "trajectory/llm_trajectory.jsonl"
    output = rollout / "artifacts/openrouter-generations.json"
    response_ids: list[str] = []
    if trajectory.is_file():
        for line in trajectory.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                exchange = json.loads(line)
            except json.JSONDecodeError:
                continue
            response_id = ((exchange.get("response") or {}).get("body") or {}).get("id")
            if isinstance(response_id, str) and response_id not in response_ids:
                response_ids.append(response_id)
    if output.is_file():
        try:
            existing = json.loads(output.read_text())
            if (
                existing.get("ok") is True
                and existing.get("cost_complete") is True
                and existing.get("request_count") == len(response_ids)
                and existing.get("generation_count") == len(response_ids)
            ):
                return existing
        except json.JSONDecodeError:
            pass
    generations: list[dict[str, Any]] = []
    errors: list[str] = []
    for response_id in response_ids:
        try:
            encoded_id = urllib.parse.quote(response_id, safe="")
            payload = _openrouter_request(
                f"https://openrouter.ai/api/v1/generation?id={encoded_id}", key
            )
            data = payload.get("data") or {}
            generations.append(
                {
                    field: data.get(field)
                    for field in (
                        "id",
                        "model",
                        "provider_name",
                        "total_cost",
                        "tokens_prompt",
                        "tokens_completion",
                        "native_tokens_prompt",
                        "native_tokens_completion",
                        "cache_discount",
                        "latency",
                        "generation_time",
                    )
                }
            )
        except Exception as exc:
            errors.append(f"{response_id}: {type(exc).__name__}: {exc}")
    costs = [
        float(item["total_cost"])
        for item in generations
        if isinstance(item.get("total_cost"), (int, float))
    ]
    failed_ids = [
        error.split(":", 1)[0]
        for error in errors
        if error.split(":", 1)[0] in response_ids
    ]
    captured_count = len(generations) + len(set(failed_ids))
    cost_complete = len(costs) == len(response_ids)
    report = {
        "schema_version": 1,
        "captured_at": utc_now(),
        "request_count": len(response_ids),
        "generation_count": len(generations),
        "total_cost_usd": round(sum(costs), 10) if costs else None,
        "cost_complete": cost_complete,
        "cost_coverage": f"{len(costs)}/{len(response_ids)}",
        "unpriced_request_ids": [
            response_id
            for response_id in response_ids
            if response_id not in {
                str(item.get("id")) for item in generations
            }
        ],
        "providers": sorted(
            {
                str(item["provider_name"])
                for item in generations
                if item.get("provider_name")
            }
        ),
        "models": sorted(
            {str(item["model"]) for item in generations if item.get("model")}
        ),
        "generations": generations,
        "errors": errors,
        "ok": bool(response_ids) and captured_count == len(response_ids),
    }
    atomic_json(output, report)
    return report


def _run_root(mode: str) -> Path:
    return CLAWS_DIR / ("corpus-smoke" if mode == "smoke" else "corpus")


def _docker_preflight() -> None:
    completed = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Docker daemon is not available")


def _ensure_base_image_identity(lock_path: Path) -> dict[str, Any]:
    reference = "ghcr.io/benchflow-ai/env0:0.2.0"
    if lock_path.is_file():
        locked = json.loads(lock_path.read_text()).get("base_image")
        if not isinstance(locked, dict):
            raise RuntimeError("run lock has no base_image identity")
        current = docker_image_identity(reference)
        locked_digests = set(locked.get("repo_digests") or [])
        current_digests = set(current.get("repo_digests") or [])
        if locked_digests and not locked_digests.issubset(current_digests):
            raise RuntimeError(
                "local env0 image no longer matches the immutable run lock"
            )
        return locked
    identity = docker_image_identity(reference)
    if identity["repo_digests"]:
        return identity
    pull = subprocess.run(
        ["docker", "pull", reference],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if pull.returncode != 0:
        raise RuntimeError(f"could not pull {reference}: {pull.stderr[-2000:]}")
    identity = docker_image_identity(reference)
    if not identity["repo_digests"]:
        raise RuntimeError(f"could not resolve a digest for {reference}")
    return identity


def _assert_local_benchflow() -> None:
    import benchflow

    source = Path(benchflow.__file__).resolve()
    expected = (BENCHFLOW_ROOT / "src/benchflow").resolve()
    if expected not in source.parents:
        raise RuntimeError(
            f"runner imported BenchFlow from {source}, expected local source under {expected}"
        )


def make_recorded_rollout_class():
    from benchflow.rollout import Rollout

    class RecordedRollout(Rollout):
        def __init__(self, config):
            super().__init__(config)
            self._recording_services = active_services(config.task_path)
            self._capture_status: dict[str, Any] = {}
            self._recording_env_ready = False

        def _write_capture_status(self) -> None:
            if self._rollout_paths is None:
                return
            atomic_json(
                self._rollout_paths.artifacts_dir / "capture-status.json",
                self._capture_status,
            )

        async def _capture_env0(self, phase: str) -> bool:
            command = (
                f"python3 {CONTAINER_CAPTURE_SCRIPT} env "
                f"--phase {shlex.quote(phase)} "
                f"--services-json {shlex.quote(json.dumps(self._recording_services))}"
            )
            result = await self._env.exec(command, user="root", timeout_sec=90)
            ok = result.return_code == 0
            self._capture_status[phase] = {
                "ok": ok,
                "captured_at": utc_now(),
                "return_code": result.return_code,
                "stdout": (result.stdout or "")[-2000:],
                "stderr": (result.stderr or "")[-2000:],
            }
            self._write_capture_status()
            return ok

        async def _capture_openclaw(self) -> bool:
            command = (
                f"python3 {CONTAINER_CAPTURE_SCRIPT} openclaw "
                f"--task-name {shlex.quote(self._config.task_path.name)}"
            )
            result = await self._env.exec(command, user="root", timeout_sec=120)
            ok = result.return_code == 0
            self._capture_status["openclaw"] = {
                "ok": ok,
                "captured_at": utc_now(),
                "return_code": result.return_code,
                "stdout": (result.stdout or "")[-3000:],
                "stderr": (result.stderr or "")[-3000:],
            }
            self._write_capture_status()
            return ok

        async def start(self) -> None:
            await super().start()
            self._recording_env_ready = True
            await self._env.upload_file(CAPTURE_SCRIPT, CONTAINER_CAPTURE_SCRIPT)
            if not await self._capture_env0("initial"):
                raise RuntimeError("required initial env0 capture failed")

        async def install_agent(self) -> None:
            await super().install_agent()
            agents_path = f"{self._agent_cwd.rstrip('/')}/AGENTS.md"
            await self._env.upload_file(META_PATH, agents_path)
            q_agents = shlex.quote(agents_path)
            setup = await self._env.exec(
                f"chown agent:agent {q_agents} && "
                "runuser -u agent -- env HOME=/home/agent "
                "PATH=/opt/benchflow/bin:/opt/benchflow/node/bin:/usr/local/bin:/usr/bin:/bin "
                "/opt/benchflow/bin/openclaw config set "
                "agents.defaults.skills '[]' --strict-json >/dev/null && "
                "runuser -u agent -- env HOME=/home/agent "
                "PATH=/opt/benchflow/bin:/opt/benchflow/node/bin:/usr/local/bin:/usr/bin:/bin "
                "/opt/benchflow/bin/openclaw config set "
                "skills.allowBundled '[]' --strict-json >/dev/null && "
                "{ find /app/skills /app/.agents/skills /app/.claude/skills "
                "/home/agent/.agents/skills /home/agent/.openclaw/skills "
                "-name SKILL.md -type f -print 2>/dev/null || true; }",
                user="root",
                timeout_sec=45,
            )
            if setup.return_code != 0:
                raise RuntimeError(
                    "OpenClaw skills-off configuration failed: "
                    f"{(setup.stderr or setup.stdout or '')[-2000:]}"
                )
            if (setup.stdout or "").strip():
                raise RuntimeError(
                    "skills-off invariant failed; SKILL.md files were visible: "
                    f"{setup.stdout[-2000:]}"
                )
            config_result = await self._env.exec(
                "cat /home/agent/.openclaw/openclaw.json",
                user="root",
                timeout_sec=10,
            )
            try:
                config = json.loads(config_result.stdout)
                if config["agents"]["defaults"]["skills"] != []:
                    raise ValueError("agents.defaults.skills is not empty")
                if config["skills"]["allowBundled"] != []:
                    raise ValueError("skills.allowBundled is not empty")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(f"could not prove OpenClaw skills are off: {exc}") from exc
            self._capture_status["bootstrap"] = {
                "ok": True,
                "agents_path": agents_path,
                "meta_sha256": sha256_file(META_PATH),
                "skills": "off",
            }
            self._write_capture_status()

        async def verify(self):
            if "terminal" not in self._capture_status:
                await self._capture_env0("terminal")
            if "openclaw" not in self._capture_status:
                await self._capture_openclaw()
            rewards = await super().verify()
            await self._capture_env0("post_verifier")
            return rewards

        async def cleanup(self) -> None:
            if self._recording_env_ready:
                if "terminal" not in self._capture_status:
                    await self._capture_env0("terminal")
                if "openclaw" not in self._capture_status:
                    try:
                        await self.disconnect()
                    except Exception:
                        pass
                    await self._capture_openclaw()
            await super().cleanup()

    return RecordedRollout


def _valid_rollouts(
    run_root: Path,
    job_name: str,
    task_name: str,
    *,
    openrouter_key: str,
) -> list[Path]:
    task_dir = ENV0_ROOT / "tasks" / task_name
    services = active_services(task_dir)
    prompt = task_prompt(task_dir)
    valid: list[Path] = []
    job_dir = run_root / job_name
    if not job_dir.is_dir():
        return valid
    for rollout in sorted(job_dir.glob(f"{task_name}__*")):
        reconcile_openrouter_generations(rollout, openrouter_key)
        report = validate_rollout_capture(
            rollout,
            expected_task=task_name,
            services=services,
            expected_prompt=prompt,
        )
        atomic_json(rollout / "capture-validation.json", report)
        if report["ok"]:
            valid.append(rollout)
    if len(valid) > 1:
        raise RuntimeError(
            f"multiple valid canonical rollouts exist for {task_name}: {valid}"
        )
    return valid


async def _run_task(
    *,
    task_name: str,
    run_root: Path,
    job_name: str,
    environment_manifest: Any,
    RecordedRollout: Any,
    openrouter_key: str,
    concurrency: int,
) -> dict[str, Any]:
    from benchflow._utils.benchmark_repos import task_file_hashes
    from benchflow._utils.task_authoring import task_digest
    from benchflow.rollout import RolloutConfig
    from benchflow.usage_tracking import UsageTrackingConfig

    task_dir = ENV0_ROOT / "tasks" / task_name
    env0_state = git_state(ENV0_ROOT)
    rollout_name = f"{task_name}__{uuid.uuid4().hex[:10]}"
    config = RolloutConfig.from_legacy(
        task_path=task_dir,
        agent="openclaw",
        model=MODEL,
        agent_env={"OPENROUTER_API_KEY": openrouter_key},
        job_name=job_name,
        rollout_name=rollout_name,
        jobs_dir=run_root,
        concurrency=concurrency,
        environment="docker",
        environment_manifest=environment_manifest,
        context_root=ENV0_ROOT,
        skill_mode="no-skill",
        skills_dir=None,
        sandbox_user="agent",
        agent_idle_timeout=600,
        usage_tracking=UsageTrackingConfig(mode="required"),
        task_digest=task_digest(task_dir),
        source_provenance={
            "type": "github",
            "repo": "benchflow-ai/env0",
            "requested_ref": env0_state["commit"],
            "resolved_sha": env0_state["commit"],
            "path": f"tasks/{task_name}",
            "local_path": str(task_dir),
            "dirty": env0_state["dirty"],
            "file_hashes": task_file_hashes(task_dir),
        },
        loop_strategy="single-shot",
    )
    rollout = await RecordedRollout.create(config)
    started = time.monotonic()
    result = await rollout.run()
    rollout_dir = run_root / job_name / result.rollout_name
    reconcile_openrouter_generations(rollout_dir, openrouter_key)
    report = validate_rollout_capture(
        rollout_dir,
        expected_task=task_name,
        services=active_services(task_dir),
        expected_prompt=task_prompt(task_dir),
    )
    report["elapsed_sec"] = round(time.monotonic() - started, 3)
    atomic_json(rollout_dir / "capture-validation.json", report)
    return report


async def run_batch(
    *,
    mode: str,
    tasks: list[str],
    run_root: Path,
    concurrency: int,
    build_concurrency: int,
    openrouter_key: str,
) -> dict[str, Any]:
    from benchflow.environment.manifest import load_manifest
    from benchflow.sandbox.docker import DockerSandbox

    register_pinned_openclaw()
    RecordedRollout = make_recorded_rollout_class()
    DockerSandbox.set_build_concurrency(build_concurrency)
    environment_manifest = load_manifest(ENVIRONMENT_MANIFEST)
    job_name = f"{mode}_v1"

    completed: dict[str, Path] = {}
    for task_name in tasks:
        valid = _valid_rollouts(
            run_root,
            job_name,
            task_name,
            openrouter_key=openrouter_key,
        )
        if valid:
            completed[task_name] = valid[0]

    pending = [name for name in tasks if name not in completed]
    progress = {
        "schema_version": 1,
        "mode": mode,
        "total": len(tasks),
        "completed": len(completed),
        "pending": len(pending),
        "running": [],
        "invalid": [],
        "updated_at": utc_now(),
    }
    atomic_json(run_root / "progress.json", progress)
    print(
        f"Plan: {len(tasks)} tasks, {len(completed)} valid on disk, "
        f"{len(pending)} to run; concurrency={concurrency}",
        flush=True,
    )

    semaphore = asyncio.Semaphore(concurrency)
    reports: dict[str, dict[str, Any]] = {}
    running: set[str] = set()
    progress_lock = asyncio.Lock()

    async def one(task_name: str) -> None:
        async with semaphore:
            async with progress_lock:
                running.add(task_name)
                progress["running"] = sorted(running)
                progress["updated_at"] = utc_now()
                atomic_json(run_root / "progress.json", progress)
            print(f"[START] {task_name}", flush=True)
            try:
                report = await _run_task(
                    task_name=task_name,
                    run_root=run_root,
                    job_name=job_name,
                    environment_manifest=environment_manifest,
                    RecordedRollout=RecordedRollout,
                    openrouter_key=openrouter_key,
                    concurrency=concurrency,
                )
            except Exception as exc:
                logging.exception("task driver crashed: %s", task_name)
                report = {
                    "ok": False,
                    "task": task_name,
                    "errors": [f"driver exception: {type(exc).__name__}: {exc}"],
                    "cost_usd": None,
                    "total_tokens": None,
                }
            reports[task_name] = report
            async with progress_lock:
                running.remove(task_name)
                progress["running"] = sorted(running)
                progress["completed"] = len(completed) + sum(
                    1 for value in reports.values() if value.get("ok")
                )
                progress["invalid"] = sorted(
                    name for name, value in reports.items() if not value.get("ok")
                )
                progress["pending"] = len(tasks) - progress["completed"]
                progress["updated_at"] = utc_now()
                atomic_json(run_root / "progress.json", progress)
            status = "OK" if report.get("ok") else "INVALID"
            cost = report.get("cost_usd")
            cost_text = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
            if cost_text != "n/a" and report.get("cost_complete") is False:
                cost_text += f"+ ({report.get('cost_coverage', '?')} priced)"
            print(
                f"[{status}] {task_name} · cost={cost_text} · "
                f"valid={progress['completed']}/{len(tasks)}",
                flush=True,
            )

    await asyncio.gather(*(one(task_name) for task_name in pending))

    canonical: dict[str, dict[str, Any]] = {}
    for task_name in tasks:
        valid = _valid_rollouts(
            run_root,
            job_name,
            task_name,
            openrouter_key=openrouter_key,
        )
        if valid:
            canonical[task_name] = json.loads(
                (valid[0] / "capture-validation.json").read_text()
            )
    total_cost = round(
        sum(
            float(report["cost_usd"])
            for report in canonical.values()
            if isinstance(report.get("cost_usd"), (int, float))
        ),
        8,
    )
    total_tokens = sum(
        int(report["total_tokens"])
        for report in canonical.values()
        if isinstance(report.get("total_tokens"), int)
    )
    cost_complete = bool(canonical) and all(
        report.get("cost_complete") is True for report in canonical.values()
    )
    priced_generations = 0
    requested_generations = 0
    for report in canonical.values():
        coverage = report.get("cost_coverage")
        if isinstance(coverage, str) and re.fullmatch(r"\d+/\d+", coverage):
            priced, requested = coverage.split("/", 1)
            priced_generations += int(priced)
            requested_generations += int(requested)
    summary = {
        "schema_version": 1,
        "mode": mode,
        "model": MODEL,
        "task_count": len(tasks),
        "valid_count": len(canonical),
        "invalid_or_missing": sorted(set(tasks) - set(canonical)),
        "total_cost_usd": total_cost,
        "total_cost_is_lower_bound": not cost_complete,
        "cost_coverage": f"{priced_generations}/{requested_generations}",
        "total_tokens": total_tokens,
        "mean_cost_usd": round(total_cost / len(canonical), 8) if canonical else None,
        "projected_standard60_cost_usd": (
            round(total_cost / len(canonical) * 60, 4)
            if canonical and cost_complete
            else None
        ),
        "projected_standard60_cost_lower_bound_usd": (
            round(total_cost / len(canonical) * 60, 4) if canonical else None
        ),
        "tasks": canonical,
        "finished_at": utc_now(),
    }
    atomic_json(run_root / job_name / "corpus-summary.json", summary)
    atomic_json(run_root / "summary.json", summary)
    return summary


def estimate_standard60_cost(smoke_summary: dict[str, Any]) -> dict[str, Any]:
    if smoke_summary.get("valid_count") != len(SMOKE_TASKS):
        raise RuntimeError("the two-task smoke corpus is not capture-valid")
    observed = smoke_summary.get("total_cost_usd")
    if not isinstance(observed, (int, float)) or observed <= 0:
        raise RuntimeError("the smoke corpus has no observed OpenRouter cost")
    priced = 0
    requested = 0
    for report in (smoke_summary.get("tasks") or {}).values():
        coverage = report.get("cost_coverage")
        if not isinstance(coverage, str) or not re.fullmatch(r"\d+/\d+", coverage):
            raise RuntimeError("the smoke corpus has incomplete cost provenance")
        task_priced, task_requested = coverage.split("/", 1)
        priced += int(task_priced)
        requested += int(task_requested)
    if priced < 1 or requested < priced:
        raise RuntimeError("the smoke corpus has invalid cost coverage")
    adjusted_smoke = float(observed) * requested / priced
    return {
        "observed_cost_lower_bound_usd": round(float(observed), 8),
        "priced_generations": priced,
        "requested_generations": requested,
        "estimated_two_task_cost_usd": round(adjusted_smoke, 6),
        "estimated_standard60_cost_usd": round(
            adjusted_smoke / len(SMOKE_TASKS) * 60, 4
        ),
        "method": (
            "Scale observed priced-generation cost by generation coverage, "
            "then multiply the two-task mean by 60."
        ),
    }


def dry_run_plan(
    mode: str, tasks: list[str], run_root: Path, concurrency: int, build_concurrency: int
) -> dict[str, Any]:
    return {
        "mode": mode,
        "task_count": len(tasks),
        "tasks": tasks,
        "model": MODEL,
        "agent": "openclaw",
        "skills": "off",
        "meta": "system",
        "meta_path": str(META_PATH),
        "run_root": str(run_root),
        "concurrency": concurrency,
        "build_concurrency": build_concurrency,
        "automatic_retries": 0,
        "would_start_docker": False,
        "would_call_openrouter": False,
    }


def configure_logging(run_root: Path) -> None:
    run_root.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(run_root / "runner.log")
    file_handler.setFormatter(formatter)
    root.handlers[:] = [stream, file_handler]


def find_secret_occurrences(root: Path, secret: str) -> list[str]:
    needle = secret.encode()
    matches: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                tail = b""
                while chunk := handle.read(1024 * 1024):
                    data = tail + chunk
                    if needle in data:
                        matches.append(str(path.relative_to(root)))
                        break
                    tail = data[-max(0, len(needle) - 1) :]
        except OSError:
            continue
    return matches


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "standard60"), required=True)
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--build-concurrency", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--skip-provider-preflight", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tasks = select_tasks(args.mode)
    concurrency = args.concurrency or (1 if args.mode == "smoke" else 2)
    build_concurrency = args.build_concurrency or (
        1 if args.mode == "smoke" else 2
    )
    if concurrency < 1 or build_concurrency < 1:
        raise SystemExit("concurrency values must be positive")
    run_root = (args.run_root or _run_root(args.mode)).resolve()

    if args.dry_run:
        print(
            json.dumps(
                dry_run_plan(
                    args.mode, tasks, run_root, concurrency, build_concurrency
                ),
                indent=2,
            )
        )
        return 0

    if sha256_file(META_PATH) != META_SHA256:
        raise RuntimeError("local meta is not byte-identical to the official artifact")
    _assert_local_benchflow()
    _docker_preflight()
    configure_logging(run_root)

    key = load_openrouter_key()
    lock_path = run_root / "run-lock.json"
    image_identity = _ensure_base_image_identity(lock_path)
    spec = build_run_spec(
        args.mode,
        tasks,
        concurrency=concurrency,
        build_concurrency=build_concurrency,
        image_identity=image_identity,
    )
    lock_status = ensure_run_lock(lock_path, spec)
    logging.info("run lock %s: %s", lock_status, lock_path)

    provider: dict[str, Any] | None = None
    if not args.skip_provider_preflight:
        provider = openrouter_preflight(key)
        atomic_json(run_root / "openrouter-preflight.json", provider)
        print(
            f"OpenRouter: exact model reachable; remaining "
            f"${provider['remaining_usd']:.2f}",
            flush=True,
        )

    cost_estimate: dict[str, Any] | None = None
    if args.mode == "standard60":
        smoke_summary_path = _run_root("smoke") / "summary.json"
        if not smoke_summary_path.is_file():
            raise RuntimeError(
                f"capture-valid smoke summary is missing: {smoke_summary_path}"
            )
        cost_estimate = estimate_standard60_cost(
            json.loads(smoke_summary_path.read_text())
        )
        estimate = cost_estimate["estimated_standard60_cost_usd"]
        if (
            provider is not None
            and provider["remaining_usd"] < estimate * 1.5 + MIN_BALANCE_USD
        ):
            raise RuntimeError(
                f"OpenRouter balance ${provider['remaining_usd']:.2f} is below "
                f"the estimated Standard60 need plus reserve"
            )
        atomic_json(run_root / "cost-estimate.json", cost_estimate)
        print(
            f"Estimated Standard60 cost: ${estimate:.2f} "
            f"(smoke generation coverage "
            f"{cost_estimate['priced_generations']}/"
            f"{cost_estimate['requested_generations']})",
            flush=True,
        )

    if args.preflight_only:
        preflight = {
            "ok": True,
            "mode": args.mode,
            "task_count": len(tasks),
            "model": MODEL,
            "run_root": str(run_root),
            "run_lock": lock_status,
            "tasks_started": 0,
            "remaining_usd": (
                provider.get("remaining_usd") if provider is not None else None
            ),
            "cost_estimate": cost_estimate,
            "checked_at": utc_now(),
        }
        atomic_json(run_root / "preflight.json", preflight)
        print(json.dumps(preflight, indent=2), flush=True)
        return 0

    summary = asyncio.run(
        run_batch(
            mode=args.mode,
            tasks=tasks,
            run_root=run_root,
            concurrency=concurrency,
            build_concurrency=build_concurrency,
            openrouter_key=key,
        )
    )
    leaked = find_secret_occurrences(run_root, key)
    if leaked:
        raise RuntimeError(
            f"OpenRouter key leaked into run artifacts: {leaked[:10]}"
        )
    print(json.dumps(summary, indent=2), flush=True)
    if summary["valid_count"] != summary["task_count"]:
        print(
            "Run incomplete or invalid. Re-run the same command to resume only "
            "missing/invalid tasks.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(
            "\nInterrupted safely. Re-run the same command to resume from "
            "capture-valid task results.",
            file=sys.stderr,
        )
        sys.exit(130)
