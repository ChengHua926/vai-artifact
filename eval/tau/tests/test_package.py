from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

from eval import run


CORE_EVALUATOR_MODULES = {
    "__init__.py",
    "contracts.py",
    "corpus.py",
    "effects.py",
    "evaluate.py",
    "monitor.py",
    "replay.py",
}
MONITOR_ADAPTER_MODULES = {"adapter.py", "promises.py", "trace_adapter.py"}


def test_tau_runtime_modules_import_from_canonical_package() -> None:
    tau_root = Path(__file__).resolve().parents[2] / "tau"

    for name in (
        "eval.tau.adapter",
        "eval.tau.contracts",
        "eval.tau.corpus",
        "eval.tau.effects",
        "eval.tau.evaluate",
        "eval.tau.monitor",
        "eval.tau.promises",
        "eval.tau.replay",
        "eval.tau.trace_adapter",
    ):
        module = importlib.import_module(name)
        assert Path(module.__file__).resolve().is_relative_to(tau_root.resolve())


def test_tau_source_provenance_uses_the_complete_canonical_package() -> None:
    paths = {row["path"] for row in run._source_manifest("tau")}
    canonical = {
        path for path in paths if path.startswith("eval/tau/")
    }

    assert canonical == {
        f"eval/tau/{name}"
        for name in CORE_EVALUATOR_MODULES | MONITOR_ADAPTER_MODULES
    }
    assert not any(path.startswith("eval/evaluation/") for path in paths)


def test_tau_capture_runs_as_a_package_module() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "eval.tau.capture", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
