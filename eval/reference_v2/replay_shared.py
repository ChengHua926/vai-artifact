"""Reproduce the shared-catalog migration against frozen v4 observations.

Evaluation uses captured ActionRecords only. Frozen labels and reason-match
annotations are loaded after all original/shared evaluations have completed.
The report carries those annotations forward only when the selected fire ledger
is unchanged. This command never collects new traces or changes labels.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path

from aa_commons import ActionRecord, policy_engine, registry, trace_hash
from . import legacy_v4, run, runtime


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PINNED_INPUTS = {
    'results_v4/captured_records.jsonl.gz': '7545131853ca44a0a0975011e91b18e22f73db7a87c95a02789dffa4bb14f371',
    'results_v4/commitments.jsonl': '46500651377765817b058db7fc94eaadcd129a942d87aa70b018d2c00a981223',
    'presentation_v4/tasks.jsonl': '4384e627964df8201969a28cabf2d817f629fffd5839abeb4916d31b65af55e3',
}
PANEL_NAMES = {('tau', 'glm47'): 'Tau GLM', ('tau', 'qwen3_30b'): 'Tau Qwen',
               ('clawsbench', 'openrouter/z-ai/glm-5.2'): 'ClawsBench'}
OUTPUT_NAMES = ('cases.jsonl.gz', 'summary.json', 'manifest.json', 'report.md')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def normalized(value):
    # JSON turns integer object keys into strings. Compare decoded values, not
    # sort order of numeric keys before versus after that conversion.
    return json.loads(json.dumps(value, allow_nan=False))


def encode_cases(rows):
    payload = ''.join(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                                 allow_nan=False) + '\n' for row in rows).encode()
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0) as stream:
        stream.write(payload)
    return output.getvalue()


def compare_cases(expected, actual):
    differences = []
    for task in sorted(set(expected) | set(actual)):
        if task not in expected or task not in actual:
            fields = ['task_membership']
        else:
            before, after = normalized(expected[task]), normalized(actual[task])
            fields = [key for key in sorted(set(before) | set(after))
                      if key not in before or key not in after or before[key] != after[key]]
        if fields:
            differences.append({'task': task, 'fields': fields})
    return differences


def remove_legacy_answers(inputs):
    result = copy.deepcopy(inputs)
    checked = 0
    for group in ('scope_checks', 'attempt_scope_checks', 'mandate_checks'):
        for checks in (result.get(group) or {}).values():
            for check in checks:
                check.pop('allowed', None)
                if 'amount_condition' in check:
                    for key in ('allowed_amounts', 'max_amount', 'amount'):
                        check.pop(key, None)
                checked += 1
    return result, checked


def behavior(result):
    return normalized({
        'fires': result['fires'], 'verdicts': result['verdicts'],
        'diagnostics': result['diagnostics'],
        'unsupported': result['inputs'].get('unsupported_checks', {}),
        'trace_hash': result['trace_hash'], 'sdk_verifier_parity': result['sdk_verifier_parity'],
    })


def pinned_bytes(relative):
    payload = (HERE / relative).read_bytes()
    if digest(payload) != PINNED_INPUTS[relative]:
        raise ValueError(f'frozen replay input changed: {relative}')
    return payload


def source_hashes():
    paths = {ROOT / path for path in runtime.SOURCE_PATHS}
    paths.update(HERE / name for name in ('replay_shared.py', 'legacy_v4.py', 'baseline_v4_sources.json',
                                        'run.py', 'present_v4.py'))
    for directory in ('packages/commons/aa_commons', 'packages/sdk/aa_sdk', 'packages/verifier/aa_verifier'):
        paths.update((ROOT / directory).rglob('*.py'))
    return {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in sorted(paths)}


def fire_core(fire):
    return {key: fire.get(key) for key in ('arm', 'seq', 'predicate', 'targets', 'detail')}


def fire_multiset(fires):
    return Counter(json.dumps(fire_core(fire), sort_keys=True) for fire in fires)


def report_panels(cases, references):
    # Import reporting configuration only after all predicate runs. Neither it
    # nor the annotations can influence an evaluator's input or verdict.
    from .present_v4 import EXCLUDED_ARMS
    by_key = {row['task']: row for row in cases}
    refs = {(row.get('case_id') or row['task']): row for row in references}
    if len(refs) != len(references) or set(refs) != set(by_key):
        raise ValueError('presentation membership differs from evaluated captures')
    panels = {name: {'tasks': 0, 'violation_fire': 0, 'violation_no_fire': 0,
                     'clean_fire': 0, 'clean_no_fire': 0, 'same_reason_tasks': 0,
                     'selected_fire_events': 0, 'clean_firing_tasks': [],
                     'unmatched_firing_tasks': []} for name in PANEL_NAMES.values()}
    differences = []
    for key in sorted(refs):
        ref, result = refs[key], by_key[key]
        selected = [fire for fire in result['shared']['fires'] if fire['arm'] not in EXCLUDED_ARMS]
        same_fires = fire_multiset(selected) == fire_multiset(ref['fires'])
        if not same_fires or bool(selected) != ref['fired']:
            differences.append({'task': key, 'fields': ['selected_fire_membership']})
        matches = set(ref['matching_original_fire_indexes'])
        selected_indexes = {fire['original_fire_index'] for fire in ref['fires']}
        if not matches <= selected_indexes or bool(matches) != ref['same_reason']:
            raise ValueError(f'inconsistent frozen reason-match annotations: {key}')
        positive = ref['label'] == 'violation'
        if ref['label'] not in ('violation', 'no_violation'):
            raise ValueError(f'unknown frozen label: {key}')
        panel = panels[PANEL_NAMES[(ref['bench'], ref['model_id'])]]
        fired = bool(selected)
        panel['tasks'] += 1
        panel[('violation' if positive else 'clean') + ('_fire' if fired else '_no_fire')] += 1
        panel['selected_fire_events'] += len(selected)
        same_reason = same_fires and bool(matches)
        if positive and fired:
            panel['same_reason_tasks'] += int(same_reason)
            if not same_reason:
                panel['unmatched_firing_tasks'].append(ref['task'])
        if not positive and fired:
            panel['clean_firing_tasks'].append(ref['task'])
        result['reported'] = {'panel': PANEL_NAMES[(ref['bench'], ref['model_id'])],
                              'task': ref['task'], 'label': ref['label'], 'detected': fired,
                              'same_reason': same_reason, 'selected_fire_count': len(selected),
                              'selected_fire_membership_unchanged': same_fires,
                              'matching_original_fire_indexes': sorted(matches)}
    return panels, differences, sorted(EXCLUDED_ARMS)


def report_text(summary):
    rows = ['# Shared catalog replay', '',
            'The shared AAP catalog was replayed on the same captured observations as v4. '
            'No new agent runs or labels were produced.', '',
            '| Benchmark | Runs | Violation + detected | Violation + not detected | Clean + detected | Clean + not detected |',
            '|---|---:|---:|---:|---:|---:|']
    for name, panel in summary['panels'].items():
        values = [panel[key] for key in ('tasks', 'violation_fire', 'violation_no_fire', 'clean_fire', 'clean_no_fire')]
        rows.append('| ' + name + ' | ' + ' | '.join(map(str, values)) + ' |')
    rows += ['', f"Compared {summary['cases']} runs, {summary['action_records']} committed records and "
             f"{summary['rule_verdicts']} rule verdicts. Differences: {summary['difference_count']}.", '',
             'Comparison includes every verdict, exact reason, first offending record, complete fire ledger, '
             'diagnostics, unsupported-evidence output and trace hash. The original evaluator is loaded from '
             'its frozen source bundle and checked against the original saved commitments. The new evaluator '
             'also checks SDK versus hash-resolved replay agreement.', '',
             f"Removing legacy precomputed allowed/amount answers across {summary['independence']['checks']} "
             f"checks changed {summary['independence']['differing_cases']} cases.", '',
             'Labels and same-reason annotations are read only after all evaluations. Selected fires must '
             'match the frozen presentation exactly before its reason-match annotations are carried forward. '
             'The existing exclusions of message-format checks remain unchanged.', '']
    for name, panel in summary['panels'].items():
        if panel['violation_fire']:
            rows.append(f"- {name}: {panel['same_reason_tasks']}/{panel['violation_fire']} detected violation tasks match a cited reason.")
    rows += ['', 'Current promises use AAP-2, AAP-3 or AAP-5. Their parameters commit the exact observation '
             'profile, including domain policy definitions and state reconstruction. Shared code evaluates '
             'the conditions. Domain-specific policy code remains necessary; this is not automatic policy '
             'translation or use of the five unchanged historical functions.', '',
             'This verifies replay on existing traces. It does not establish live integration into either '
             'benchmark agent or public-network settlement.', '',
             'Regenerate with `.venv/bin/python -m eval.reference_v2.replay_shared`; verify the saved '
             'artifacts byte-for-byte with the same command plus `--check`. Historical results and labels '
             'are never overwritten.', '']
    return '\n'.join(rows).encode()


def _progress(message):
    print(message, flush=True)


def generate(progress=_progress):
    frozen_inputs = run.check_frozen()
    frozen_baseline = run.check_baseline()
    sources_before = source_hashes()
    captures = [json.loads(line) for line in gzip.decompress(
        pinned_bytes('results_v4/captured_records.jsonl.gz')).splitlines() if line]
    commitments = [json.loads(line) for line in pinned_bytes('results_v4/commitments.jsonl').splitlines() if line]
    original = {row['task']: row for row in commitments}
    if len(original) != len(commitments) or len({row['task'] for row in captures}) != len(captures):
        raise ValueError('duplicate capture or original commitment')
    if len(captures) != 388 or {row['task'] for row in captures} != set(original):
        raise ValueError('frozen capture cohort changed')
    old_runtime, old_spec = legacy_v4.runtime(), legacy_v4.ensure_registered()
    profiles = runtime.ensure_registered()
    catalog = registry.catalog()
    if len(catalog) != 5 or {entry['number'] for entry in catalog} != {f'AAP-{i}' for i in range(1, 6)}:
        raise ValueError('current catalog must contain exactly AAP-1 through AAP-5')
    cases, differences = [], []
    for index, row in enumerate(captures, 1):
        key, benchmark = row['task'], row['benchmark']
        records = [ActionRecord.from_dict(record) for record in row['records']]
        prior = original[key]
        if prior['benchmark'] != benchmark or prior['predicate_hash'] != legacy_v4.PREDICATE_HASH:
            raise ValueError(f'original commitment identity changed: {key}')
        old = old_runtime.analyze_records(records, benchmark)
        old['verdicts'] = {}
        for arm in old_runtime.ARMS[benchmark]:
            verdict = old_spec.evaluate(records, {'benchmark': benchmark, 'arm': arm})
            old['verdicts'][arm] = {'violated': verdict.violated, 'record_seq': verdict.seq, 'reason': verdict.reason}
        old.update(trace_hash=trace_hash(records), sdk_verifier_parity=prior['sdk_verifier_parity'])
        saved_fields = ('verdicts', 'trace_hash', 'sdk_verifier_parity')
        original_changes = compare_cases({key: {field: prior[field] for field in saved_fields}},
                                         {key: {field: old[field] for field in saved_fields}})
        current = runtime.verify_case(records, benchmark)
        old_behavior, new_behavior = behavior(old), behavior(current)
        changes = compare_cases({key: old_behavior}, {key: new_behavior})
        stripped, count = remove_legacy_answers(current['inputs'])
        independent = policy_engine.run(current['trace'], stripped) == current['fires']
        if not independent:
            changes.append({'task': key, 'fields': ['legacy_answer_independence']})
        if not current['sdk_verifier_parity']:
            changes.append({'task': key, 'fields': ['sdk_verifier_parity']})
        differences.extend([{**item, 'comparison': 'original_saved_commitment'} for item in original_changes])
        differences.extend([{**item, 'comparison': 'shared_vs_frozen'} for item in changes])
        cases.append({'task': key, 'benchmark': benchmark, 'record_count': len(records),
                      'baseline_predicate_hash': legacy_v4.PREDICATE_HASH,
                      'baseline': old_behavior, 'shared': new_behavior,
                      'promise_bindings': current['promise_bindings'],
                      'predicate_hashes': current['predicate_hashes'],
                      'observation_profile_hash': current['observation_profile_hash'],
                      'differences': changes, 'original_commitment_differences': original_changes,
                      'independence': {'checks': count, 'unchanged': independent}})
        if index == 1 or index % 50 == 0 or index == len(captures):
            progress(f'evaluated {index}/{len(captures)} captures; {len(differences)} differences')
    # No reference label or reason-match annotation has been loaded above.
    references = [json.loads(line) for line in pinned_bytes('presentation_v4/tasks.jsonl').splitlines() if line]
    panels, presentation_changes, excluded = report_panels(cases, references)
    differences.extend([{**item, 'comparison': 'selected_presentation'} for item in presentation_changes])
    summary = {'schema_version': 1, 'implementation': runtime.IMPLEMENTATION_ID,
               'passed': not differences, 'cases': len(cases),
               'action_records': sum(row['record_count'] for row in cases),
               'rule_verdicts': sum(len(row['shared']['verdicts']) for row in cases),
               'raw_fire_events': sum(len(row['shared']['fires']) for row in cases),
               'selected_fire_events': sum(panel['selected_fire_events'] for panel in panels.values()),
               'difference_count': len(differences), 'differences': differences, 'panels': panels,
               'independence': {'checks': sum(row['independence']['checks'] for row in cases),
                                'differing_cases': sum(not row['independence']['unchanged'] for row in cases)},
               'sdk_verifier_parity_cases': sum(row['shared']['sdk_verifier_parity'] for row in cases),
               'excluded_report_arms': excluded,
               'same_reason_method': 'Frozen annotation retained only after exact selected-fire membership comparison',
               'live_benchmark_integration_tested': False}
    if source_hashes() != sources_before:
        raise ValueError('implementation sources changed during replay; rerun against a stable checkout')
    run.check_frozen()
    run.check_baseline()
    artifacts = {'cases.jsonl.gz': encode_cases(sorted(cases, key=lambda row: row['task'])),
                 'summary.json': json_bytes(summary), 'report.md': report_text(summary)}
    profile_descriptors = {}
    for benchmark, profile in profiles.items():
        descriptor = profile.descriptor
        profile_descriptors[benchmark] = {'profile_hash': profile.profile_hash,
            'profile_id': descriptor['profile_id'], 'version': descriptor['version'],
            'config': descriptor['config'], 'rules': descriptor['rules'],
            'reason_fields': descriptor['reason_fields'],
            'source_bundle_sha256': digest(descriptor['source_bundle'].encode()),
            'build_source_sha256': digest(descriptor['build_source'].encode())}
    manifest = {'schema_version': 1, 'implementation': runtime.IMPLEMENTATION_ID,
                'inputs_sha256': {'eval/reference_v2/' + name: checksum for name, checksum in PINNED_INPUTS.items()},
                'sources_sha256': sources_before,
                'original_predicate_hash': legacy_v4.PREDICATE_HASH,
                'original_source_bundle_sha256': digest(legacy_v4.SOURCE_FILE.read_bytes()),
                'current_catalog': catalog, 'observation_profiles': profile_descriptors,
                'original_frozen_inputs_checked': len(frozen_inputs),
                'original_v2_baseline_files_checked': len(frozen_baseline),
                'outputs_sha256': {name: digest(payload) for name, payload in sorted(artifacts.items())}}
    artifacts['manifest.json'] = json_bytes(manifest)
    return artifacts, summary


def write_artifacts(destination, artifacts, check=False):
    destination = Path(destination)
    run.validate_output_paths(destination, artifacts)
    if check:
        mismatches = [name for name, payload in artifacts.items()
                      if not (destination / name).is_file() or (destination / name).read_bytes() != payload]
        if mismatches:
            raise ValueError('saved shared replay differs: ' + ', '.join(sorted(mismatches)))
        return
    destination.mkdir(parents=True, exist_ok=True)
    for name, payload in artifacts.items():
        (destination / name).write_bytes(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=HERE / 'results_shared')
    parser.add_argument('--check', action='store_true', help='recompute and require identical saved artifact bytes')
    args = parser.parse_args()
    run.validate_output_paths(args.destination, OUTPUT_NAMES)
    artifacts, summary = generate()
    write_artifacts(args.destination, artifacts, check=args.check)
    print(json.dumps({'passed': summary['passed'], 'differences': summary['difference_count'],
                      'panels': summary['panels']}, sort_keys=True))
    if not summary['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
