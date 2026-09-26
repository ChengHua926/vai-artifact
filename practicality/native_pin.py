"""Native harness revision used by the run scripts, as the release documents it: the upstream
commit in integrations/<harness>/upstream.json with integrations/<harness>/patches/*.patch applied."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def pin(harness):
    """Upstream commit and patch file for 'hermes' or 'openclaw'."""
    spec = json.loads((ROOT / "integrations" / harness / "upstream.json").read_text())
    return spec["commit"], ROOT / "integrations" / harness / spec["patch"]["path"]


def matches(repo, harness):
    """True when the checkout's HEAD is the upstream commit and the release patch is applied."""
    commit, patch = pin(harness)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    applied = subprocess.run(["git", "apply", "--check", "-R", str(patch)], cwd=repo,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    return head == commit and applied


def describe(harness):
    """Manifest entry for the pinned native revision."""
    commit, patch = pin(harness)
    return {"upstream_commit": commit, "patch": str(patch.relative_to(ROOT)),
            "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}
