"""Registered benchmark policy replay from committed observations.

This is a registry extension, not a claim that the existing generic AAP predicates
implement domain policy. The source commitment includes the reducers, rules and
fire engine. Native capture attachments are replay evidence, never policy params.
No labels, rewards, desired actions or grader solutions enter this module.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from aa_commons import ActionRecord, PredicateSpec, Verdict, registry, trace_hash
from aa_sdk import Accountability
from eval import predicates
from eval.reference_v2.tau import adapter as tau_adapter
from eval.clawsbench.analysis import adapter as claws_adapter

SPEC_ID = 'benchmark_policy_v2'
ROOT = Path(__file__).resolve().parents[2]
ARMS = {
    'tau_retail': ('auth_first','one_user','status_precondition','modify_items_lockout',
                   'new_item_differs','cancel_reason_enum','once_per_order'),
    'tau_airline': ('user_id_from_user','bookable_status','basic_economy_no_modify',
                    'flown_no_cabin_change','no_bag_removal','passenger_count_fixed',
                    'cancel_flown','cancel_eligibility','payment_in_profile',
                    'certificate_eligibility','certificate_mandate'),
    'clawsbench': ('verified_destructive_target','read_before_external_permission_grant',
                   'read_before_mutation','identical_call_cap'),
}
SOURCE_PATHS = (
    'eval/reference_v2/runtime.py', 'eval/predicates.py',
    'eval/reference_v2/tau/adapter.py', 'eval/reference_v2/tau/promises.py',
    'eval/clawsbench/analysis/adapter.py', 'eval/clawsbench/analysis/promises.py',
    'packages/commons/aa_commons/predicate.py', 'packages/commons/aa_commons/registry.py',
    'packages/commons/aa_commons/params_spec.py', 'packages/commons/aa_commons/trace.py',
)


def validate(params):
    if set(params) != {'benchmark','arm'}:
        raise ValueError('policy params must contain only benchmark and arm')
    if params['benchmark'] not in ARMS or params['arm'] not in ARMS[params['benchmark']]:
        raise ValueError('unknown benchmark or policy arm')


def source_bundle():
    return json.dumps({p: (ROOT/p).read_text() for p in SOURCE_PATHS}, sort_keys=True)


def ensure_registered():
    if SPEC_ID not in registry.all_specs():
        registry.register(PredicateSpec(SPEC_ID, 2, evaluate,
            doc='Benchmark policy obligations over captured observations; unsupported evidence is retained separately',
            number='EVAL-1', source_bundle=source_bundle(), validate_params=validate))
    return registry.get(SPEC_ID)


def _record(records, session_id, tool, args, result, metadata):
    records.append(ActionRecord(len(records)+1, session_id, tool,
        copy.deepcopy(args), copy.deepcopy(result), 0, copy.deepcopy(metadata)))


def make_tau_records(trace, turns, session_id):
    records, seen = [], set()
    _record(records,session_id,'tau.capture',{}, {'turns_available':bool(turns)},
            {'benchmark_event':'capture'})
    by_seq = {c['seq']: c for c in trace}
    if len(by_seq) != len(trace):
        raise ValueError('duplicate benchmark action sequence')
    def emit(c):
        if c['seq'] in seen:
            raise ValueError('duplicate action in conversation')
        seen.add(c['seq'])
        meta={'benchmark_seq':c['seq']}
        for key in ('captured_error','tool_call_id'):
            if key in c: meta[key]=c[key]
        _record(records,session_id,c['tool'],c.get('args') or {},c.get('result'),meta)
    for t in turns:
        if t.get('role') == 'user':
            _record(records,session_id,'tau.user',{},t.get('text',''),{'benchmark_event':'user'})
        for seq in t.get('calls') or []:
            if seq not in by_seq: raise ValueError('conversation references missing action')
            emit(by_seq[seq])
    for seq in sorted(by_seq):
        if seq not in seen: emit(by_seq[seq])
    return records


def make_claws_records(captured, session_id):
    records=[]
    _record(records,session_id,'claws.initial',{},captured['initial_facts'],{'benchmark_event':'initial'})
    for a in captured['agent_trace']:
        _record(records,session_id,a['tool'],a.get('args') or {},a.get('result'),
                {'benchmark_seq':a['seq'],'evidence':a.get('evidence') or {}})
    # An attachment to the completed capture, not an authorization or initial
    # observation. The reducer joins it to original request times/identities and
    # only applies a result after evaluating that request.
    _record(records,session_id,'claws.native_capture',{},captured['native_calls'],
            {'benchmark_event':'native_capture'})
    return records


def analyze_records(records, benchmark):
    if benchmark not in ARMS: raise ValueError('unknown benchmark')
    ordered=sorted(records,key=lambda r:r.seq)
    if ordered and (len({r.session_id for r in ordered})!=1 or
                    [r.seq for r in ordered]!=list(range(1,len(ordered)+1))):
        raise ValueError('records must be one contiguous session')
    trace=[]; sequence_map={}; turns=[]; initial=[]; native=[]; tau_capture=[]
    for r in ordered:
        meta=r.metadata or {}; event=meta.get('benchmark_event')
        if event is not None:
            if event=='capture' and benchmark.startswith('tau_') and r.tool=='tau.capture':
                tau_capture.append(r.result)
            elif event=='user' and benchmark.startswith('tau_') and r.tool=='tau.user':
                turns.append({'role':'user','text':r.result,'calls':[]})
            elif event=='initial' and benchmark=='clawsbench' and r.tool=='claws.initial': initial.append(r.result)
            elif event=='native_capture' and benchmark=='clawsbench' and r.tool=='claws.native_capture': native.append(r.result)
            else: raise ValueError('unexpected benchmark context record')
            continue
        seq=meta.get('benchmark_seq')
        if not isinstance(seq,int) or isinstance(seq,bool) or seq in sequence_map:
            raise ValueError('invalid or duplicate benchmark sequence')
        sequence_map[seq]=r.seq
        a={'seq':seq,'tool':r.tool,'args':copy.deepcopy(r.args),'result':copy.deepcopy(r.result)}
        for key in ('captured_error','tool_call_id','evidence'):
            if key in meta: a[key]=copy.deepcopy(meta[key])
        trace.append(a)
        turns.append({'role':'assistant','text':'','calls':[seq]})
    if [a['seq'] for a in trace]!=list(range(1,len(trace)+1)):
        raise ValueError('benchmark actions must be contiguous and ordered')
    if benchmark=='clawsbench':
        if len(initial)!=1 or len(native)!=1: raise ValueError('missing or duplicate capture attachments')
        trace,inputs,diagnostics=claws_adapter.replay_captured({
            'schema_version':1,'initial_facts':initial[0], 'agent_trace':trace, 'native_calls':native[0]})
    else:
        if len(tau_capture)!=1 or not isinstance(tau_capture[0].get('turns_available'),bool):
            raise ValueError('missing or invalid conversation availability')
        if not tau_capture[0]['turns_available']:
            if any(t['role']=='user' for t in turns):
                raise ValueError('user observations contradict missing conversation')
            turns=[]
        inputs=tau_adapter.build_inputs({'trace':trace,'turns':turns},benchmark.removeprefix('tau_'))
        diagnostics=[]
    fires=predicates.run(trace,inputs)
    return {'trace':trace,'inputs':inputs,'diagnostics':diagnostics,'fires':fires,'sequence_map':sequence_map}


def evaluate(records, params):
    validate(params)
    result=analyze_records(records,params['benchmark'])
    hits=[f for f in result['fires'] if f.get('arm')==params['arm']]
    if not hits: return Verdict.satisfied()
    first=min(hits,key=lambda f:f['seq'])
    return Verdict.violation(result['sequence_map'][first['seq']],
        json.dumps({'benchmark_seq':first['seq'],'arm':params['arm'],'detail':first.get('detail')},sort_keys=True))


def verify_case(records, benchmark, arms=None):
    spec=ensure_registered()
    chosen=ARMS.get(benchmark,()) if arms is None else arms
    if not chosen: raise ValueError('unknown benchmark or empty arm selection')
    acc=Accountability('offline-reference-evaluation')
    pids={arm:acc.register_promise(SPEC_ID,{'benchmark':benchmark,'arm':arm},payout_wei=1) for arm in chosen}
    sdk=acc.self_check(records)
    # Serialization roundtrip and committed-hash resolution match the verifier
    # boundary. No RPCs or settlements are performed in this offline evaluation.
    restored=[ActionRecord.from_dict(d) for d in json.loads(json.dumps([r.to_dict() for r in records]))]
    bound=registry.resolve_hash(registry.predicate_hash_for(SPEC_ID))
    if bound is not spec: raise AssertionError('registered source did not resolve')
    verdicts={}
    for arm,pid in pids.items():
        other=bound.evaluate(restored,{'benchmark':benchmark,'arm':arm})
        if other!=sdk[pid]: raise AssertionError('SDK/verifier verdict mismatch')
        verdicts[arm]={'violated':other.violated,'record_seq':other.seq,'reason':other.reason}
    result=analyze_records(restored,benchmark)
    for arm in chosen:
        if verdicts[arm]['violated'] != any(f.get('arm')==arm for f in result['fires']):
            raise AssertionError('fire ledger disagrees with registered verdict')
    result.update({'verdicts':verdicts,'sdk_verifier_parity':True,
                   'predicate_hash':registry.predicate_hash_for(SPEC_ID),'trace_hash':trace_hash(restored)})
    return result
