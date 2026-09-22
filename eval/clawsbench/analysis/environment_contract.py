"""Capture pinned service configuration, never task prompts or labels.

The pure replay only consumes the returned facts. This capture-time helper
verifies each source file against the Env0 Git commit in the immutable run lock.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess


CONTRACT_FILES = (
    "docker/Dockerfile.base", "docker/gws-wrapper.sh",
    "tasks/_manifests/env-0.toml",
    "packages/environments/mock-gdoc/mock_gdoc/api/app.py",
    "packages/environments/mock-gdoc/mock_gdoc/api/deps.py",
    "packages/environments/mock-gmail/mock_gmail/api/app.py",
    "packages/environments/mock-gmail/mock_gmail/api/deps.py",
    "packages/environments/mock-gcal/mock_gcal/api/app.py",
    "packages/environments/mock-gcal/mock_gcal/api/deps.py",
)
SUPPORTED_ENV0_COMMIT = "d12ebf517ca7ff9126710dafef113d53df1bb163"


def _env_value(text: str, key: str, default: str = "") -> str:
    value = default
    for line in text.replace("\\\n", " ").splitlines():
        if not line.lstrip().startswith("ENV "):
            continue
        tokens = shlex.split(line.lstrip()[4:], comments=True)
        if tokens and tokens[0] == key and len(tokens) == 2:
            value = tokens[1]
        for token in tokens:
            if token.startswith(key + "="):
                value = token.split("=", 1)[1]
    return value


def build_environment_contract(task: str, env0_root: Path, run_lock_path: Path) -> dict:
    """Return evidence-backed Docs auth mode for the locked task image."""
    lock_bytes = run_lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    if task not in lock["tasks"] or not re.fullmatch(r"[a-z0-9-]+", task):
        raise ValueError("task is not part of the locked environment")
    repo = lock["repositories"]["env0"]
    commit = repo["commit"]
    if repo.get("dirty") or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("environment source must be a clean pinned commit")
    if commit != SUPPORTED_ENV0_COMMIT:
        raise ValueError("environment semantics have not been audited for this commit")
    task_config = f"tasks/{task}/task.md"
    task_dockerfile = f"tasks/{task}/environment/Dockerfile"
    files = (*CONTRACT_FILES, task_config, task_dockerfile)
    texts, hashes = {}, {}
    for name in files:
        pinned = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=env0_root,
                                check=True, capture_output=True).stdout
        local = (env0_root / name).read_bytes()
        if local != pinned:
            raise ValueError(f"environment source differs from run lock: {name}")
        hashes[name] = hashlib.sha256(pinned).hexdigest()
        texts[name] = pinned.decode()
    # The recorded task configuration has no environment overrides. Reject
    # unhandled overrides rather than silently treating Dockerfile defaults as
    # runtime facts. Only frontmatter is examined; task prompt text is unused.
    frontmatter = texts[task_config].split("---", 2)[1]
    environment = re.search(r"(?ms)^environment:\n(.*?)(?=^[^\s]|\Z)", frontmatter)
    if not environment or not re.search(r"(?m)^  env: \{\}$", environment.group(1)):
        raise ValueError("task runtime environment overrides are not audited")
    if "AUTH_ENABLED" in texts["tasks/_manifests/env-0.toml"]:
        raise ValueError("service manifest auth overrides are not audited")
    dockerfile = texts[task_dockerfile]
    reference = lock["base_image"]["reference"]
    if not re.search(r"(?m)^FROM\s+" + re.escape(reference) + r"\s*$", dockerfile):
        raise ValueError("task image base differs from immutable run lock")
    base_auth = _env_value(texts["docker/Dockerfile.base"], "AUTH_ENABLED")
    mode = _env_value(dockerfile, "AUTH_ENABLED", base_auth)
    return {
        "schema_version": 1,
        "source_commit": commit,
        "run_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "base_image": lock["base_image"],
        "source_files": hashes,
        "services": {
            service: {
                "oauth_enabled": mode.strip().lower() in {"1", "true", "yes"},
                "implicit_principal": "first_local_user",
                "identity_headers": [f"X-Env-0-{header}-User", f"X-Mock-{header}-User"],
                "gws_wrapper": "routes_base_url_only",
            }
            for service, header in (("docs", "Gdoc"), ("gmail", "Gmail"), ("calendar", "Gcal"))
        },
    }
