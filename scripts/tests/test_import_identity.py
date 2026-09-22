"""A reused virtual environment must never choose another checkout's protocol code."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_fresh(tmp_path, code):
    foreign = tmp_path / "foreign-checkout"
    package = foreign / "aa_sdk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("MARKER = 'foreign-checkout'\n")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(foreign), str(ROOT / "scripts"))),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                          capture_output=True, text=True)


@pytest.mark.parametrize("entrypoint", ["_config", "verifier_service", "catalog", "check_promises"])
def test_fresh_entrypoint_imports_protocol_from_its_own_checkout(tmp_path, entrypoint):
    result = run_fresh(tmp_path, f"""
import {entrypoint}
import aa_sdk, aa_commons, json
print(json.dumps({{'sdk': aa_sdk.__file__, 'commons': aa_commons.__file__}}))
""")
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(paths["sdk"]).resolve().is_relative_to(ROOT / "packages/sdk/aa_sdk")
    assert Path(paths["commons"]).resolve().is_relative_to(ROOT / "packages/commons/aa_commons")


def test_foreign_protocol_already_loaded_is_rejected_instead_of_mixed(tmp_path):
    result = run_fresh(tmp_path, "import aa_sdk; import _config")
    assert result.returncode != 0
    assert "outside the configured checkout" in result.stderr
