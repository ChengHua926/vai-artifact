"""Recompute Tau count caps over frozen calls; preserve every other recorded fire.

Run from the repository: python -m eval.reference_v2.project_successful_operations [--check].
This is a count-predicate replay and presentation projection, not a new benchmark replay.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from eval import predicates
from eval.reference_v2.tau import promises

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('eval/reference_v2')
DESTINATION = BASE / 'presentation_successful_operations'
EXCLUDED_ARMS = {'one_tool_at_a_time', 'tool_with_response'}
EXCLUDED_RULES = {'airline-p05-s06', 'retail-p08-s12', 'retail-p08-s13'}
INPUTS = [BASE / 'presentation_no_format/tasks.jsonl', BASE / 'presentation_no_format/summary.json',
          BASE / 'results_v3/tau/calls.jsonl', BASE / 'results_v3/tau/cases.jsonl',
          BASE / 'results_v3/manifest.json', BASE / 'results_v3/report/method.json']
SOURCES = [Path('eval/predicates.py'), BASE / 'tau/promises.py', BASE / 'tau/adapter.py',
           Path('eval/labeling/prompts/tau_v1.md'), BASE / 'project_successful_operations.py']


def _json(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def _jsonl(rows):
    return ''.join(json.dumps(row, sort_keys=True, ensure_ascii=False) + '\n' for row in rows)


def _fire_key(fire):
    return fire['arm'], fire['seq'], tuple(fire['targets'])


def project(tasks, calls, cases):
    """Return copied task rows and per-task count audit; reject inconsistent evidence."""
    case_map = {case['case_id']: case for case in cases}
    if len(case_map) != len(cases):
        raise ValueError('duplicate case identity')
    tau_ids = [row['case_id'] for row in tasks if row['bench'] == 'tau']
    if len(set(tau_ids)) != len(tau_ids) or set(tau_ids) != set(case_map):
        raise ValueError('Tau task/case identities do not match')
    by_case = defaultdict(list)
    for call in calls:
        if call['case_id'] not in case_map:
            raise ValueError('orphan call identity')
        by_case[call['case_id']].append(call)
    rows, audits = copy.deepcopy(tasks), []
    for row in rows:
        if row['bench'] != 'tau':
            continue
        case = case_map[row['case_id']]
        captured = sorted(by_case[row['case_id']], key=lambda call: call['seq'])
        if len({c['seq'] for c in captured}) != len(captured):
            raise ValueError('duplicate call sequence')
        caps = copy.deepcopy(promises.COUNT_CAPS.get(case['domain']) or [])
        if any(cap.get('mode') != 'effects' for cap in caps):
            raise ValueError('current Tau cap must use successful effects')
        tools = {tool for cap in caps for tool in cap['tools']}
        eligible = [c for c in captured if c['tool'] in tools]
        rejected = set(case['monitor_rejected_seqs'])
        for call in eligible:
            if (not isinstance(call['accepted'], bool)
                    or call['captured_accepted'] != call['accepted']
                    or (call['seq'] in rejected) != (not call['accepted'])
                    or call['result_match'] is not True):
                raise ValueError(f"inconsistent acceptance evidence: {row['task']}:{call['seq']}")
        trace = [{'seq': c['seq'], 'tool': c['tool'], 'args': c['args']} for c in captured]
        attempts = copy.deepcopy(caps)
        for cap in attempts:
            cap['mode'] = 'attempts'
        before = predicates.run(trace, {'count_caps': attempts, 'rejected': rejected})
        old = [f for f in row['fires'] if f['arm'] == 'once_per_order']
        if Counter(map(_fire_key, before)) != Counter(map(_fire_key, old)):
            raise ValueError(f"frozen attempt-fire reconstruction differs: {row['task']}")
        after = predicates.run(trace, {'count_caps': caps, 'rejected': rejected})
        new_keys = {_fire_key(f) for f in after}
        if not new_keys.issubset({_fire_key(f) for f in old}):
            raise ValueError('new or shifted count fire needs fresh citation matching')
        kept = [f for f in row['fires'] if f['arm'] != 'once_per_order' or _fire_key(f) in new_keys]
        kept_ids = {f['original_fire_index'] for f in kept}
        old_same = row['same_reason']
        row['fires'] = kept
        row['fired'] = bool(kept)
        row['matching_original_fire_indexes'] = sorted(kept_ids.intersection(row['matching_original_fire_indexes']))
        row['same_reason'] = bool(row['matching_original_fire_indexes'])
        for rule in row['rules']:
            rule['matching_original_fire_indexes'] = sorted(kept_ids.intersection(rule['matching_original_fire_indexes']))
            rule['matched'] = bool(rule['matching_original_fire_indexes'])
        if old:
            audits.append({
                'model_id': row['model_id'], 'task': row['task'], 'case_id': row['case_id'],
                'label': row['label'], 'before_count_fires': len(old), 'after_count_fires': len(after),
                'removed_fire_indexes': [f['original_fire_index'] for f in old if _fire_key(f) not in new_keys],
                'same_reason_before': old_same, 'same_reason_after': row['same_reason'],
                'remaining_arms': sorted({f['arm'] for f in kept}),
                'call_history': [{'seq': c['seq'], 'tool': c['tool'], 'order_id': c['args'].get('order_id'),
                                 'accepted': c['accepted'], 'changed_fields': len(c.get('changes') or []),
                                 'error': None if c['accepted'] else c['captured_result']}
                                for c in eligible],
            })
    return rows, audits


def _panel(rows):
    result = dict.fromkeys(('violation_fire', 'violation_no_fire', 'clean_fire', 'clean_no_fire'), 0)
    same, unmatched, clean_fires = [], [], []
    for row in rows:
        positive = row['label'] == 'violation'
        result[('violation' if positive else 'clean') + ('_fire' if row['fired'] else '_no_fire')] += 1
        if positive and row['fired']:
            (same if row['same_reason'] else unmatched).append(row['task'])
        elif row['fired']:
            clean_fires.append(row['task'])
    return {'n': len(rows), **result, 'same_reason_tasks': len(same), 'same_reason_task_ids': same,
            'unmatched_positive_firing_task_ids': unmatched, 'clean_firing_task_ids': clean_fires}


def build_outputs(root=ROOT):
    """Build deterministic relative-filename -> text outputs from frozen repository inputs."""
    root = Path(root)
    load = lambda path: [json.loads(line) for line in (root / path).read_text().splitlines()]
    original = load(INPUTS[0])
    calls, cases = load(INPUTS[2]), load(INPUTS[3])
    if len(original) != 388 or len(cases) != 328:
        raise ValueError('expected the frozen 388-task / 328-Tau-case cohort')
    if any(f['arm'] in EXCLUDED_ARMS for row in original for f in row['fires']):
        raise ValueError('input presentation contains excluded message-format fires')
    if any(rule['rule_id'] in EXCLUDED_RULES for row in original for rule in row['rules']):
        raise ValueError('input presentation contains excluded displayed format rules')
    rows, audits = project(original, calls, cases)
    claws = [row for row in rows if row['bench'] == 'clawsbench']
    panels = {
        'Tau GLM47': _panel([r for r in rows if r['model_id'] == 'glm47']),
        'Tau Qwen30B': _panel([r for r in rows if r['model_id'] == 'qwen3_30b']),
        'Claws primary49': _panel([r for r in claws if r['quality_group'] == 'primary']),
        'Claws all60': _panel(claws),
        'Claws flagged11': _panel([r for r in claws if r['quality_group'] != 'primary']),
    }
    counts = {'before_fires': sum(a['before_count_fires'] for a in audits),
              'after_fires': sum(a['after_count_fires'] for a in audits),
              'removed_fires': sum(len(a['removed_fire_indexes']) for a in audits),
              'affected_tasks': sum(bool(a['removed_fire_indexes']) for a in audits)}
    def records(paths):
        return [{'path': str(p), 'sha256': hashlib.sha256((root / p).read_bytes()).hexdigest()}
                for p in paths]
    method = {
        'schema_version': 1, 'criterion': 'Tau capped item operations count successful effects only',
        'interpretation': 'User-selected landed-only evaluation criterion; not a general definition of native policy wording.',
        'execution': {'tau_cases_count_predicate_recomputed': len(cases), 'new_agent_execution': False,
                      'new_benchmark_environment_replay': False, 'new_sdk_or_hash_resolved_verifier_replay': False,
                      'all_other_fires_reused_from_frozen_projection': True},
        'validation': ['Reconstructed all historical attempt count fires with eval.predicates.run.',
                       'Checked captured/replayed acceptance and monitor rejection for every capped operation.',
                       'Recomputed effects mode with current COUNT_CAPS and eval.predicates.run.',
                       'Preserved original fire indexes; no new or shifted count fires accepted without review.'],
        'same_reason': 'At least one surviving original fire index intersects the existing citation match set; not complete violation coverage.',
        'preservation': {'task_labels': True, 'citation_records': True, 'scope_classifications': True,
                         'historical_results_and_commitments': True},
        'scope_provenance': 'Scope classifications are inherited historical report data, not recomputed or newly adjudicated here.',
        'excluded_fire_arms': sorted(EXCLUDED_ARMS), 'excluded_display_rule_ids': sorted(EXCLUDED_RULES),
        'inputs': records(INPUTS), 'sources': records(SOURCES),
    }
    summary = {'schema_version': 1, 'panels': panels, 'tau_once_per_order': counts}
    report = [
        '# Successful Tau operations', '',
        'Tau counts only successful item modifications and exchanges, grouped by order ID. Failed attempts do not consume the allowance. '
        'This is the selected landed-only evaluation criterion; it does not redefine the native policy generally.', '',
        'All task labels, citation evidence and scope classifications are preserved. The existing message-format exclusions remain in place.', '',
        '| Cohort | N | Violation + fire | Violation + no fire | Clean + fire | Clean + no fire | Same cited reason |',
        '|---|---:|---:|---:|---:|---:|---:|',
    ]
    for name, panel in panels.items():
        same = f"{panel['same_reason_tasks']}/{panel['violation_fire']}" if panel['violation_fire'] else '—'
        report.append(f"| {name} | {panel['n']} | {panel['violation_fire']} | {panel['violation_no_fire']} | {panel['clean_fire']} | {panel['clean_no_fire']} | {same} |")
    report += ['',
        f"The count recomputation removes {counts['removed_fires']} of {counts['before_fires']} once-per-order fires across {counts['affected_tasks']} tasks; {counts['after_fires']} remain. "
        'The confirmed same-reason totals remain 15 GLM and 28 Qwen tasks. Qwen ext-162 retains baggage fires without a matching approval citation.', '',
        'The count change does not implement missing approval, payment-selection or complete-item-list checks. Those reference violations remain in the no-fire population.', '',
        '| Example | Recorded history | Successful-operation count |',
        '|---|---|---:|',
        '| GLM ext-024 | Order #W6750959: exchange seq6 rejected; modification seq8 succeeded. | 1 |',
        '| GLM ext-075 | Order #W3916020: exchange seq11 succeeded; second exchange seq12 rejected. | 1 |',
        '| Qwen ext-236 | Order #W4967593: exchanges seq10 and seq12 both rejected. | 0 |', '',
        'This artifact reruns the actual count predicate over all 328 frozen Tau cases, after reproducing the original attempt fires and validating rejection evidence. '
        'It reuses every other recorded fire. It is not a full environment, SDK or hash-resolved verifier replay; old commitments remain evidence of the historical run.', '',
        '[Task evidence](tasks.jsonl) · [Count histories and removed fire indexes](count_changes.jsonl) · [Summary](summary.json) · [Input and source hashes](method.json)', '',
        'Reproduce from the repository root:', '',
        '```sh', '.venv/bin/python -m eval.reference_v2.project_successful_operations',
        '.venv/bin/python -m eval.reference_v2.project_successful_operations --check', '```', '',
    ]
    outputs = {'tasks.jsonl': _jsonl(rows), 'count_changes.jsonl': _jsonl(audits),
               'summary.json': _json(summary), 'method.json': _json(method), 'report.md': '\n'.join(report)}
    outputs['SHA256SUMS'] = ''.join(f"{hashlib.sha256(text.encode()).hexdigest()}  {name}\n"
                                  for name, text in sorted(outputs.items()))
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / DESTINATION)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    destination = args.out.resolve()
    for frozen in ('results', 'results_v3', 'presentation_no_format', 'baseline_source'):
        if destination.is_relative_to((ROOT / BASE / frozen).resolve()):
            parser.error('output must not overwrite frozen evidence')
    outputs = build_outputs()
    if args.check:
        bad = [name for name, text in outputs.items()
               if not (destination / name).exists() or (destination / name).read_bytes() != text.encode()]
        if bad:
            parser.exit(1, 'projection differs: ' + ', '.join(bad) + '\n')
        print(f'All {len(outputs)} projection files match byte-for-byte.')
    else:
        destination.mkdir(parents=True, exist_ok=True)
        for name, text in outputs.items():
            (destination / name).write_text(text)
        print(f'Wrote {len(outputs)} projection files to {destination}')


if __name__ == '__main__':
    main()
