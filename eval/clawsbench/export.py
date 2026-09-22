#!/usr/bin/env python3
"""Export the local ClawsBench corpus for the evidence viewer.

No labeling or predicates here — just extraction.

Run from the repository root:
    python -m eval.clawsbench.export

Writes local ClawsBench manifests and raw-artifact references.

The contract: dump the shared per-run context, the per-task definition + ground truth, and the
per-execution trace + every label. Nothing interpreted, nothing dropped.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path

from .analysis import viewer_export


CLAWS_DIR = Path(__file__).resolve().parent
EVAL_ROOT = CLAWS_DIR.parent


def export_clawsbench(run_root: Path, output: Path) -> dict | None:
    if not run_root.is_dir():
        print(f"  skip ClawsBench (local corpus unavailable): {run_root}")
        return None
    report = viewer_export.build_manifests(
        run_root,
        output,
        secret_values=viewer_export.configured_secrets(
            EVAL_ROOT / ".env",
            EVAL_ROOT.parent / ".env",
        ),
    )
    print(
        "  clawsbench: "
        f"{report['canonical_count']} canonical tasks, "
        f"{report['attempt_count']} attempts, "
        f"{report['artifact_count']} artifacts"
    )
    print(f"wrote {output}")
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the local ClawsBench corpus for the evidence viewer."
    )
    parser.add_argument(
        "--only",
        choices=("all", "clawsbench"),
        default="all",
    )
    parser.add_argument(
        "--claws-run-root",
        type=Path,
        default=Path(
            os.environ.get(
                "CLAWSBENCH_CORPUS_ROOT", viewer_export.DEFAULT_RUN_ROOT
            )
        ),
    )
    parser.add_argument(
        "--claws-output",
        type=Path,
        default=Path(
            os.environ.get(
                "CLAWSBENCH_VIEWER_DATA", viewer_export.DEFAULT_OUT
            )
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.only in ("all", "clawsbench"):
        print("exporting ClawsBench ...")
        export_clawsbench(args.claws_run_root, args.claws_output)
