from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from eval import run


REPO_ROOT = Path(__file__).resolve().parents[3]
EVALUATOR_MODULES = {
    "__init__.py",
    "causal.py",
    "contracts.py",
    "corpus.py",
    "evaluate.py",
    "matching.py",
    "nonharm.py",
    "replay.py",
    "runtime.py",
}


def test_capture_package_module_reaches_help_without_external_calls() -> None:
    environment = os.environ.copy()
    environment["OPENROUTER_API_KEY"] = "offline-test-key"

    completed = subprocess.run(
        [sys.executable, "-m", "eval.agentdojo.capture", "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--provider" in completed.stdout
    assert "--endpoint-revision" in completed.stdout
    assert "--max-cost-usd" in completed.stdout


def test_capture_package_resolves_the_consolidated_eval_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    from eval.agentdojo.capture import run as capture_run

    assert capture_run.EVAL_ROOT == REPO_ROOT / "eval"


def test_official_cli_agentdojo_selection_lazy_imports_no_tau(tmp_path: Path) -> None:
    command = f"""
import sys
import types
from pathlib import Path
from eval import run

selected = types.ModuleType("eval.agentdojo")
selected.evaluate_agentdojo = lambda cohort: {{"selected": "agentdojo"}}
sys.modules["eval.agentdojo"] = selected
run.load_paper_main_v1 = lambda: object()
run._require_unchanged_sources = lambda expected: None
run._write_verified_benchmark_bundle = lambda *args, **kwargs: kwargs["result"]
status = run.main(["evaluate", "--benchmark", "agentdojo", "--output", {str(tmp_path)!r}])
print(status)
print(any(name.startswith(("eval.evaluation.tau", "eval.tau")) for name in sys.modules))
"""

    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.splitlines()[-2:] == ["0", "False"]


def test_agentdojo_source_provenance_uses_only_canonical_package_paths() -> None:
    paths = {row["path"] for row in run._source_manifest("agentdojo")}
    expected = {f"eval/agentdojo/{name}" for name in EVALUATOR_MODULES}

    assert expected <= paths
    assert not any(path.startswith("eval/evaluation/agentdojo/") for path in paths)
    assert not any(path.startswith("eval/coverage/agentdojo/") for path in paths)
