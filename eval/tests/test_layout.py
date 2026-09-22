from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PREFIXES = (
    "eval/archive/",
    "eval/cohorts/",
    "eval/corpus/",
    "eval/coverage/",
    "eval/data/",
    "eval/evaluation/",
    "eval/results/",
    "eval/smoke/",
)


def test_no_legacy_evaluation_sources_remain_tracked() -> None:
    completed = subprocess.run(
        ["git", "ls-files", "--", "eval"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = completed.stdout.splitlines()
    forbidden = sorted(
        path
        for path in tracked
        if (REPO_ROOT / path).exists()
        and (
            path == "eval/normalize.py"
            or path.startswith(LEGACY_PREFIXES)
        )
    )

    assert forbidden == []
