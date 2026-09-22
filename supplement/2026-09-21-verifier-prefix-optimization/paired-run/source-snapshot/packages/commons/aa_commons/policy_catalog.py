"""Source bundles for catalog versions that share observation-profile machinery."""
import json
from pathlib import Path


def source_bundle(predicate_file, legacy_file):
    root = Path(__file__).parent
    paths = [Path(predicate_file), Path(legacy_file)] + [root / name for name in (
        'constraints.py', 'sequence_checks.py', 'policy_engine.py', 'policy_profiles.py',
        'policy_catalog.py', 'predicate.py', 'registry.py', 'params_spec.py', 'trace.py', 'ids.py')]
    return json.dumps({str(p.relative_to(root)): p.read_text() for p in paths}, sort_keys=True)
