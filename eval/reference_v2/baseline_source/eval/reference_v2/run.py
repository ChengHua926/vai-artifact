"""Replay frozen observations through registered policy v2; preserve original artifacts."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile

from eval.tau.corpus import load_tau_cases
from eval.clawsbench.analysis import adapter as claws_adapter
from eval.clawsbench.analysis.run import build_row
from eval.clawsbench.sealed_corpus import attempt_directories, resolve_canonical_rollout
from . import runtime

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
GENERATION_SOURCE_PATHS=(
    'eval/reference_v2/run.py', 'eval/tau/corpus.py',
    'eval/clawsbench/analysis/run.py', 'eval/clawsbench/sealed_corpus.py',
    'eval/labeling/crosstab_v2.py', 'eval/labeling/build_disagreements.py',
    'eval/labeling/committee.py',
)


def dumps(value): return json.dumps(value,sort_keys=True,indent=2,ensure_ascii=False)+'\n'
def lines(value): return ''.join(json.dumps(r,sort_keys=True,ensure_ascii=False)+'\n' for r in value)
def read_rows(path): return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
def digest(data): return hashlib.sha256(data).hexdigest()


def validate_destination(path):
    dest=Path(path).resolve()
    for rel in ('eval/paper_main_v1','eval/labeling/consensus','eval/labeling/ledgers',
                'eval/labeling/extension','eval/clawsbench/analysis','eval/labeling/crosstab'):
        original=(ROOT/rel).resolve()
        if dest==original or dest.is_relative_to(original):
            raise ValueError('destination would overwrite original evidence/results')
    frozen=json.loads((HERE/'frozen_inputs.json').read_text())
    if any((ROOT/p).resolve().is_relative_to(dest) for p in frozen):
        raise ValueError('destination contains original evidence/results')


def check_frozen():
    frozen=json.loads((HERE/'frozen_inputs.json').read_text())
    for rel,expected in frozen.items():
        if digest((ROOT/rel).read_bytes())!=expected:
            raise ValueError(f'frozen input changed: {rel}')
    return frozen


def validate_output_paths(destination, names):
    validate_destination(destination)
    if Path(destination).is_symlink():
        raise ValueError('output directory is a symlink')
    root=Path(destination).resolve()
    for name in names:
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('invalid output path')
        path=root
        for part in relative.parts:
            path=path/part
            if path.is_symlink():
                raise ValueError(f'output component is a symlink: {path}')


def check_claws_sources(corpus):
    expected=json.loads((HERE/'frozen_claws_sources.json').read_text())
    for rel,checksum in expected.items():
        path=Path(corpus)/rel
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes())!=checksum:
            raise ValueError(f'ClawsBench captured source changed: {rel}')
    return expected


def validate_claws_seal(seal, baseline):
    if set(seal['tasks'])!=set(baseline):
        raise ValueError('ClawsBench task set differs from frozen baseline')
    for task,entry in seal['tasks'].items():
        old=baseline[task]
        if any(entry.get(k)!=old.get(k) for k in ('source','reward')) or old['model']!=seal['model']:
            raise ValueError(f'ClawsBench seal differs from frozen baseline: {task}')


def _fire_key(f): return f['seq'],f.get('arm'),json.dumps(f.get('targets') or [],sort_keys=True)


def changed_fires(benchmark,task,old,result):
    previous={_fire_key(f):f for f in old}; current={_fire_key(f):f for f in result['fires']}
    changes=[]
    for key in sorted(previous.keys()^current.keys()):
        removed=key in previous; fire=(previous if removed else current)[key]
        reason=fire.get('detail') or 'Newly detected by corrected observation replay'
        if removed:
            unsupported=result['inputs'].get('unsupported_checks') or {}
            matches=[x for x in unsupported.get(fire['seq'],[]) if x.get('arm')==fire.get('arm')]
            if matches: reason='; '.join(x['reason'] for x in matches)
            elif fire.get('arm')=='read_before_write':
                reason='Removed unconditional reservation lookup requirement; the native policy does not require this particular RPC.'
            else:
                checks=(result['inputs'].get('scope_checks') or {}).get(fire['seq'],[])
                matches=[x for x in checks if x.get('arm')==fire.get('arm')]
                reason=('Corrected state satisfies the policy check: '+str(matches[0]) if matches else
                        'No supported violation under the corrected evidence/policy requirements.')
        changes.append({'benchmark':benchmark,'task':task,'change':'removed' if removed else 'added',
                        'seq':fire['seq'],'arm':fire.get('arm'),'explanation':reason,
                        'old_fire':previous.get(key),'new_fire':current.get(key)})
    return changes


def generate(claws_corpus:Path, progress=print):
    frozen=check_frozen(); outputs={}; changes=[]; commitments=[]; captures=[]
    source_hashes={p:digest((ROOT/p).read_bytes()) for p in runtime.SOURCE_PATHS+GENERATION_SOURCE_PATHS}
    claws_sources=check_claws_sources(claws_corpus)
    baseline=ROOT/'eval/paper_main_v1/tau'
    old_cases={r['case_id']:r for r in read_rows(baseline/'cases.jsonl')}
    old_fires=defaultdict(list)
    for f in read_rows(baseline/'fires.jsonl'): old_fires[f['case_id']].append(f)
    cases=[]; fires=[]
    cohort=load_tau_cases()
    if len(cohort)!=328 or Counter(c['model_id'] for c in cohort)!=Counter({'glm47':164,'qwen3_30b':164}):
        raise ValueError('unexpected Tau cohort')
    for i,c in enumerate(cohort,1):
        cid=c['case_id']; benchmark='tau_'+c['domain']
        records=runtime.make_tau_records(c['trace'],c['turns'],cid)
        result=runtime.verify_case(records,benchmark)
        inputs=result['inputs']; new=dict(old_cases[cid]); seq_arms=defaultdict(set)
        for group in ('scope_checks','mandate_checks'):
            for seq,checks in (inputs.get(group) or {}).items():
                seq_arms[seq].update(x['arm'] for x in checks)
        for cap in inputs.get('count_caps') or []:
            for action in c['trace']:
                if action['tool'] in cap['tools'] and (cap.get('mode')=='attempts' or action['seq'] not in inputs['rejected']):
                    seq_arms[action['seq']].add(cap['arm'])
        new.update({'any_fire':bool(result['fires']),'fire_arms':sorted({f['arm'] for f in result['fires']}),
                    'monitor_fire_count':len(result['fires']),'monitor_implementation':runtime.SPEC_ID,
                    'monitor_parity':'sdk_and_hash_resolved_verifier',
                    'monitor_supplied_checks_by_seq':{str(k):sorted(v) for k,v in seq_arms.items()},
                    'monitor_rejected_seqs':sorted(inputs['rejected']),
                    'unsupported_checks_by_seq':inputs.get('unsupported_checks') or {},
                    'registered_verdicts':result['verdicts'],
                    'no_fire_reason':None if result['fires'] else 'No supported breach found; inspect unsupported checks.',
                    'predicate_hash':result['predicate_hash'],'canonical_trace_hash':result['trace_hash'],
                    'exact_links':[],'fire_writer_relations':{},'pass_fire_reasons':[],
                    'failure_relation':'not_recomputed_for_reference_label_evaluation'})
        cases.append(new)
        for f in result['fires']:
            fires.append({**f,'case_id':cid,'benchmark':'tau','model_id':c['model_id'],
                          'domain':c['domain'],'task_id':c['task_id'],
                          'implementation':runtime.SPEC_ID})
        changes.extend(changed_fires('tau',cid,old_fires[cid],result))
        commitments.append({'benchmark':benchmark,'task':cid,'predicate_hash':result['predicate_hash'],
                            'trace_hash':result['trace_hash'],'sdk_verifier_parity':True,'verdicts':result['verdicts']})
        captures.append({'benchmark':benchmark,'task':cid,'records':[r.to_dict() for r in records]})
        if i%82==0: progress(f'Tau verified {i}/328')
    outputs['tau/cases.jsonl']=lines(cases).encode(); outputs['tau/fires.jsonl']=lines(fires).encode()
    outputs['tau/calls.jsonl']=(baseline/'calls.jsonl').read_bytes()

    seal_path=claws_corpus/'sealed-corpus.json'; seal=json.loads(seal_path.read_text())
    if len(seal['tasks'])!=60 or seal.get('canonical_count')!=60 or seal.get('task_count')!=60:
        raise ValueError('unexpected ClawsBench cohort')
    roots={p.resolve() for p in attempt_directories(claws_corpus)}
    old_claws={r['task']:r for r in json.loads((ROOT/'eval/clawsbench/analysis/coverage.json').read_text())}
    validate_claws_seal(seal,old_claws)
    rows=[]
    for i,task in enumerate(sorted(seal['tasks']),1):
        entry=seal['tasks'][task]
        rollout=resolve_canonical_rollout(claws_corpus,task,entry,roots)
        if rollout.name!=old_claws[task]['canonical_attempt_id']:
            raise ValueError(f'ClawsBench canonical attempt changed: {task}')
        captured=claws_adapter.capture_replay(rollout)
        records=runtime.make_claws_records(captured,task)
        result=runtime.verify_case(records,'clawsbench')
        row=build_row(task=task,family=task.split('-')[0],model=seal['model'],
            attempt=rollout.name,source=entry['source'],rollout=rollout.relative_to(claws_corpus).as_posix(),
            reward=entry['reward'],trace=result['trace'],diagnostics=result['diagnostics'],fires=result['fires'])
        row.update({'implementation':runtime.SPEC_ID,'predicate_hash':result['predicate_hash'],
                    'canonical_trace_hash':result['trace_hash'],'registered_verdicts':result['verdicts']})
        rows.append(row)
        changes.extend(changed_fires('clawsbench',task,old_claws[task]['fires'],result))
        commitments.append({'benchmark':'clawsbench','task':task,'predicate_hash':result['predicate_hash'],
                            'trace_hash':result['trace_hash'],'sdk_verifier_parity':True,'verdicts':result['verdicts']})
        captures.append({'benchmark':'clawsbench','task':task,'records':[r.to_dict() for r in records]})
        if i%15==0: progress(f'ClawsBench verified {i}/60')
    outputs['clawsbench/coverage.json']=dumps(rows).encode()
    outputs['changed_fires.jsonl']=lines(changes).encode()
    outputs['commitments.jsonl']=lines(commitments).encode()
    outputs['captured_records.jsonl.gz']=gzip.compress(lines(captures).encode(),mtime=0)
    manifest={'schema_version':2,'implementation':runtime.SPEC_ID,
              'scope':'offline registered benchmark-specific policy evaluation; no live prevention or on-chain settlement measurement',
              'registered_source_hash':registry_hash(),
              'sources':{p:source_hashes[p] for p in runtime.SOURCE_PATHS},
              'generation_sources':{p:source_hashes[p] for p in GENERATION_SOURCE_PATHS},
              'frozen_inputs':frozen,'claws_seal_sha256':digest(seal_path.read_bytes()),
              'claws_sources_manifest_sha256':digest((HERE/'frozen_claws_sources.json').read_bytes()),
              'claws_source_file_count':len(claws_sources),
              'tau_runs':len(cases),'claws_tasks':len(rows),'sdk_verifier_parity_cases':len(commitments),
              'change_counts':dict(Counter((c['change'] for c in changes)))}
    outputs['manifest.json']=dumps(manifest).encode()
    from eval.labeling.crosstab_v2 import build_outputs
    with tempfile.TemporaryDirectory(prefix='reference-v2-') as tmp:
        scratch=Path(tmp)
        for name,data in outputs.items():
            p=scratch/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        report=build_outputs(scratch/'tau',scratch/'clawsbench/coverage.json')
    for name,data in report.items(): outputs['report/'+name]=data.encode()
    outputs['SHA256SUMS']=''.join(f'{digest(data)}  {name}\n' for name,data in sorted(outputs.items())).encode()
    check_frozen()
    check_claws_sources(claws_corpus)
    for path,expected in source_hashes.items():
        if digest((ROOT/path).read_bytes())!=expected:
            raise ValueError(f'evaluation source changed during generation: {path}')
    return outputs


def registry_hash():
    from aa_commons import registry
    runtime.ensure_registered()
    return registry.predicate_hash_for(runtime.SPEC_ID)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claws-corpus',type=Path,default=os.environ.get('CLAWSBENCH_CORPUS_ROOT'))
    parser.add_argument('--out',type=Path,default=HERE/'results')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if args.claws_corpus is None: parser.error('provide --claws-corpus or CLAWSBENCH_CORPUS_ROOT')
    validate_destination(args.out)
    outputs=generate(args.claws_corpus.resolve())
    validate_output_paths(args.out,outputs)
    for name,data in outputs.items():
        path=args.out/name
        if args.check:
            if not path.exists() or path.read_bytes()!=data: raise ValueError(f'output differs: {name}')
        else:
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    check_frozen()
    print(('Verified' if args.check else 'Wrote')+f' {len(outputs)} artifacts; frozen inputs unchanged.')

if __name__=='__main__': main()
