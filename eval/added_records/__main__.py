"""Reproduce the scope review behind Table 1's "With added records" column.

From the repository root, with packages/commons, packages/sdk and pytest installed:

    python -m eval.added_records.reproduce

The script first restores eval/data/added_records/appendix.json (the frozen
rule-scope review) from its base64 parts. By hand, that is
    cat eval/data/added_records/appendix.tar.gz.b64.part* | base64 -d > appendix.tar.gz
    tar xzf appendix.tar.gz -C eval/data/added_records
with the archive's SHA256 in appendix.tar.gz.sha256. It then runs the scope tests and:

1. runs every proposed check on its constructed examples through the SDK recording path
   and the registered action_within_declared_scope predicate, then rehashes and replays
   each saved trace;
2. maps each undetected violation to the checks named by its reviewed citations, and counts
   it only if at least one of those checks passed all of its satisfied, violated and
   unresolved controls;
3. adds the frozen detection results. "With added records" is Detected plus those runs.

The examples use hypothetical added records. No historical trace or label is changed.
"""
from __future__ import annotations

import base64
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / 'eval'
FROZEN = EVAL / 'data/added_records'
REVIEW_DIR = EVAL / 'data/added_records'
APPENDIX = REVIEW_DIR / 'appendix.json'
APPENDIX_SHA256 = '24eca84b66c473ad3b3a5fa17d8882f0802da1e6a1a5f643af33d63f8b18d63b'
TESTS = ['packages/commons/tests/test_constraints.py', 'packages/commons/tests/test_policy_profiles.py',
         'packages/commons/tests/test_structured_workflow.py', 'packages/sdk/tests/test_authorization.py',
         'eval/added_records']
PANELS = [('AgentDojo', 'GLM-4.7-Flash', 'agentdojo', 'glm47'),
          ('AgentDojo', 'Qwen3-30B-A3B', 'agentdojo', 'qwen3_30b'),
          ('tau3-bench', 'GLM-4.7-Flash', 'tau', 'glm47'),
          ('tau3-bench', 'Qwen3-30B-A3B', 'tau', 'qwen3_30b'),
          ('ClawsBench', 'GLM-5.2', 'clawsbench', 'openrouter/z-ai/glm-5.2')]
REVIEW_SUITES = {'glm47': 'Tau GLM', 'qwen3_30b': 'Tau Qwen', 'openrouter/z-ai/glm-5.2': 'ClawsBench'}


def require(condition, message):
    if not condition:
        raise SystemExit('check failed: ' + message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def restore_appendix():
    if APPENDIX.exists() and sha256(APPENDIX.read_bytes()) == APPENDIX_SHA256:
        return
    parts = sorted(REVIEW_DIR.glob('appendix.tar.gz.b64.part*'))
    blob = base64.b64decode(b''.join(part.read_bytes() for part in parts))
    require(sha256(blob) == (REVIEW_DIR / 'appendix.tar.gz.sha256').read_text().split()[0],
            'appendix.tar.gz does not match appendix.tar.gz.sha256')
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as archive:
        data = archive.extractfile('appendix.json').read()
    require(sha256(data) == APPENDIX_SHA256, 'appendix.json does not match its SHA256')
    APPENDIX.write_bytes(data)


def run_tests():
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--rootdir', str(ROOT), *TESTS],
        cwd=ROOT, capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    if result.returncode != 0:
        print(result.stdout + result.stderr)
        raise SystemExit('scope tests failed')
    last = [line for line in result.stdout.splitlines() if line.strip()][-1]
    return re.sub(r' in [0-9.]+s', '', last.strip('= '))


def run_examples(lost):
    from .fixtures import run_all
    from .initial_policy_scope import scenarios
    from .report import partition, validate_scenarios
    rows = run_all() + scenarios(lost)
    require(validate_scenarios(rows) == len(rows), 'saved traces were not all replayed')
    passed = sum(row['passed'] is True and row['actual'] == row['expected'] for row in rows)
    return rows, passed, partition([], [], rows)['checks']


def added_records(rows, support, lost):
    """Undetected violations that a supported check would catch, per (benchmark, model)."""
    from .initial_policy_scope import REVIEW
    from .obligations import obligation_catalog, per_case_links
    from .report import partition, validate_link_universe
    outbound = FROZEN / 'agentdojo_outbound'
    links = per_case_links(EVAL, outbound)
    validate_link_universe(links, json.loads(APPENDIX.read_text()), jsonl(outbound / 'case_deltas.jsonl'),
                           jsonl(EVAL / 'data/runs/agentdojo/grader_contracts.jsonl'))
    result = partition(links, obligation_catalog(), rows)
    counts = {tuple(key.split('/', 1)): value for key, value in result['counts'].items()}
    # Four AgentDojo attacks detected under the original destination list are missed once the
    # list includes every structured initial destination. None of the four requests asked for
    # a direct email, so a recorded task grant makes each send checkable (initial_policy_scope).
    reviewed = {row['case_id'] for row in result['tasks'] if row['benchmark'] == 'agentdojo'}
    require(not reviewed & {case['case_id'] for case in lost}, 'lost detections overlap the reviewed runs')
    outcome = {row['scenario_id']: row for row in rows if row['check_id'] == 'direct_email'}
    newly = Counter(case['model_id'] for case in lost
                    if support['direct_email']['supported'] and (case['model_id'], case['user_task_id']) in REVIEW
                    and outcome[case['case_id']]['passed'] and outcome[case['case_id']]['actual'] == 'violated')
    for model in ('glm47', 'qwen3_30b'):
        counts['agentdojo', model]['conditional_supported'] += newly[model]
        counts['agentdojo', model]['tasks'] += sum(case['model_id'] == model for case in lost)
    return counts


def detected(lost, appendix):
    """Frozen detection results per (benchmark, model): (violations, detected)."""
    summary = json.loads((FROZEN / 'agentdojo_initial_destinations/summary.json').read_text())['models']
    runs = [row for row in jsonl(FROZEN / 'agentdojo_outbound/case_deltas.jsonl')
            if row['reference_group'] == 'successful_attack']
    result = {}
    for model in ('glm47', 'qwen3_30b'):
        replay = summary[model]['successful_attack']
        before = {row['case_id'] for row in runs if row['model_id'] == model and row['attack_related_witness']}
        missed = {case['case_id'] for case in lost if case['model_id'] == model}
        require(replay['runs'] == sum(row['model_id'] == model for row in runs), 'successful attack count')
        require(replay['baseline_attack_related_runs'] == len(before), 'original-list detections')
        require(missed <= before and replay['gained_attack_detections'] == 0, 'lost detections')
        require(replay['variant_attack_related_runs'] == len(before - missed), 'per-run detections')
        result['agentdojo', model] = (replay['runs'], replay['variant_attack_related_runs'])
    for model, suite in REVIEW_SUITES.items():
        panel = appendix['panels'][suite]
        benchmark = 'clawsbench' if suite == 'ClawsBench' else 'tau'
        result[benchmark, model] = (panel['violation_fire'] + panel['violation_no_fire'], panel['violation_fire'])
    return result


def main():
    restore_appendix()
    print('Scope tests:', run_tests())
    lost = json.loads((FROZEN / 'agentdojo_initial_destinations/lost_attack_conditions.json').read_text())
    rows, passed, support = run_examples(lost)
    print(f'{len(support)} checks, {len(rows)} examples, {passed} passed')
    require(passed == len(rows) and all(check['supported'] for check in support.values()), 'a check failed its controls')
    appendix = json.loads(APPENDIX.read_text())
    added = added_records(rows, support, lost)
    frozen = detected(lost, appendix)
    partition = json.loads((REVIEW_DIR / 'task_partition.json').read_text())['counts']
    print()
    print(f"{'Benchmark':<11} {'Model':<14} {'Violations':>10} {'Detected':>8} {'Added':>5} "
          f"{'With added records':>18} {'Outside scope':>13}")
    total = Counter()
    for benchmark, model_name, key, model in PANELS:
        violations, found = frozen[key, model]
        panel = added[key, model]
        row = Counter(violations=violations, detected=found, added=panel['conditional_supported'],
                      outside=panel['outside_reviewed_scope'])
        require(panel['unresolved'] == 0 and panel['tasks'] == violations - found, f'{key}/{model} undetected runs')
        require(found + row['added'] + row['outside'] == violations, f'{key}/{model} partition')
        if key != 'agentdojo':
            reviewed = partition[REVIEW_SUITES[model]]
            require((row['added'], row['outside']) == (reviewed['could_catch'], reviewed['out_of_scope_only']),
                    f'{key}/{model} differs from task_partition.json')
        total.update(row)
        print(f"{benchmark:<11} {model_name:<14} {violations:>10} {found:>8} {row['added']:>5} "
              f"{found + row['added']:>18} {row['outside']:>13}")
    print(f"{'All':<26} {total['violations']:>10} {total['detected']:>8} {total['added']:>5} "
          f"{total['detected'] + total['added']:>18} {total['outside']:>13}")


if __name__ == '__main__':
    main()
