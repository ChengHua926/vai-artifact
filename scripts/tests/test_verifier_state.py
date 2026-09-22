"""A restarted verifier must still revisit claims blocked by a depleted reserve."""
import importlib.util
from pathlib import Path
import sys


def test_restart_preserves_retry_challenges(tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[1]))
    spec = importlib.util.spec_from_file_location("test_service", Path(__file__).parents[1] / "verifier_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CHECKPOINT = str(tmp_path / "chain" / "deployment.cursor")
    assert module._load_state(10) == (10, set())
    module._save_state(20, {5, 9})
    assert module._load_state(10) == (20, {5, 9})
    module._save_state(21, {9})
    assert module._load_state(10) == (21, {9})
