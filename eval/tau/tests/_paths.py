from __future__ import annotations

import os
from pathlib import Path


def tau_root() -> Path:
    configured = os.environ.get("TAU2_BENCH_ROOT")
    if configured:
        return Path(configured)

    repo_root = Path(__file__).resolve().parents[3]
    candidates = (
        repo_root.parent / "tau2-explore",
        repo_root.parent.parent / "tau2-explore",
    )
    return next(
        (candidate for candidate in candidates if candidate.is_dir()),
        candidates[0],
    )
