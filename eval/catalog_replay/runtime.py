"""Replay captured observations using the shared AAP catalog.

Application profiles bind native capture interpretation and domain policy
expressions. The shared predicates evaluate those expressions; profile hashes in
promise parameters pin the complete domain source and configuration. No labels,
rewards, desired actions or grader solutions enter this module.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from aa_commons import ActionRecord, registry, trace_hash, params_hash, policy_profiles
from aa_sdk import Accountability
from aa_commons import policy_engine as predicates
from eval.catalog_replay.tau import adapter as tau_adapter
from eval.clawsbench.analysis import adapter as claws_adapter

IMPLEMENTATION_ID = 'shared_catalog_v1'
ROOT = Path(__file__).resolve().parents[2]
ARMS = {
    'tau_retail': ('auth_first','one_user','status_precondition','modify_items_lockout',
                   'new_item_differs','cancel_reason_enum','once_per_order',
                   'one_tool_at_a_time','tool_with_response',
                   'transfer_notice_requires_call','transfer_notice_after_success'),
    'tau_airline': ('user_id_from_user','bookable_status','basic_economy_no_modify',
                    'flown_no_cabin_change','no_bag_removal','passenger_count_fixed',
                    'cancel_flown','cancel_eligibility','payment_in_profile',
                    'certificate_eligibility','certificate_mandate',
                    'one_tool_at_a_time','tool_with_response',
                    'payment_composition','modification_payment_type','passenger_cap','baggage_allowance',
                    'transfer_notice_requires_call','transfer_notice_after_success',
                    'required_transfer_after_flown_cancellation',
                    'route_preserved','baggage_allowance_after_cabin_change',
                    'baggage_fee_after_cabin_change'),
    'clawsbench': ('verified_destructive_target','read_before_external_permission_grant',
                   'read_before_mutation','identical_call_cap'),
}
SOURCE_PATHS = (
    'eval/catalog_replay/runtime.py',
    'eval/catalog_replay/tau/adapter.py', 'eval/catalog_replay/tau/promises.py',
    'eval/clawsbench/analysis/adapter.py', 'eval/clawsbench/analysis/promises.py',
    'eval/clawsbench/analysis/environment_contract.py',
)
COUNT_ARMS = {'once_per_order', 'identical_call_cap', 'one_tool_at_a_time', 'passenger_cap'}
MANDATE_ARMS = {'certificate_mandate', 'payment_composition', 'modification_payment_type',
                'payment_in_profile', 'baggage_fee_after_cabin_change'}


def rule_family(arm):
    return 'count' if arm in COUNT_ARMS else 'mandate' if arm in MANDATE_ARMS else 'scope'


def source_bundle():
    return json.dumps({p: (ROOT/p).read_text() for p in SOURCE_PATHS}, sort_keys=True)


def ensure_registered():
    # Old commitments retain the exact frozen code, never the upgraded adapters.
    from . import frozen_evaluator
    frozen_evaluator.ensure_registered()
    bundle = source_bundle()
    return {benchmark: policy_profiles.register(policy_profiles.ObservationProfile(
        profile_id=benchmark, version=1, build=observe_records,
        config={'benchmark': benchmark}, rules={arm: rule_family(arm) for arm in arms},
        source_bundle=bundle, reason_fields=('benchmark_seq', 'arm', 'detail')))
        for benchmark, arms in ARMS.items()}


def promise_definition(benchmark, arm, profiles=None):
    if benchmark not in ARMS or arm not in ARMS[benchmark]:
        raise ValueError('unknown benchmark or policy arm')
    profile = (profiles or ensure_registered())[benchmark]
    return policy_profiles.FAMILIES[rule_family(arm)], {
        'observation_profile': profile.profile_hash, 'rule': arm}


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
            if t.get('calls'):
                raise ValueError('user turn cannot contain assistant actions')
        elif t.get('role') == 'assistant':
            _record(records,session_id,'tau.assistant',{},
                    {'text':t.get('text') or '', 'calls':t.get('calls') or []},
                    {'benchmark_event':'assistant'})
        else:
            raise ValueError('unexpected conversation role')
        for seq in t.get('calls') or []:
            if seq not in by_seq: raise ValueError('conversation references missing action')
            emit(by_seq[seq])
    for seq in sorted(by_seq):
        if seq not in seen: emit(by_seq[seq])
    _record(records,session_id,'tau.end',{}, {}, {'benchmark_event':'end'})
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


def observe_records(records, config):
    benchmark = config['benchmark']
    if benchmark not in ARMS: raise ValueError('unknown benchmark')
    ordered=sorted(records,key=lambda r:r.seq)
    if ordered and (len({r.session_id for r in ordered})!=1 or
                    [r.seq for r in ordered]!=list(range(1,len(ordered)+1))):
        raise ValueError('records must be one contiguous session')
    trace=[]; sequence_map={}; turns=[]; initial=[]; native=[]; tau_capture=[]
    pending_assistant_calls=[]
    message_sequence_map={}; end_record=None
    for r in ordered:
        meta=r.metadata or {}; event=meta.get('benchmark_event')
        if event is not None:
            if pending_assistant_calls:
                raise ValueError('assistant boundary interrupted before its actions')
            if event=='capture' and benchmark.startswith('tau_') and r.tool=='tau.capture':
                tau_capture.append(r.result)
            elif event=='user' and benchmark.startswith('tau_') and r.tool=='tau.user':
                turns.append({'role':'user','text':r.result,'calls':[]})
            elif event=='assistant' and benchmark.startswith('tau_') and r.tool=='tau.assistant':
                payload=r.result
                if (not isinstance(payload,dict) or set(payload)!={'text','calls'} or
                    not isinstance(payload['text'],str) or not isinstance(payload['calls'],list) or
                    any(not isinstance(s,int) or isinstance(s,bool) for s in payload['calls']) or
                    len(set(payload['calls']))!=len(payload['calls'])):
                    raise ValueError('invalid assistant boundary')
                pending_assistant_calls=list(payload['calls'])
                message_sequence_map[-(len(turns)+1)]=r.seq
                turns.append({'role':'assistant','text':payload['text'],'calls':list(payload['calls'])})
            elif event=='end' and benchmark.startswith('tau_') and r.tool=='tau.end':
                if end_record is not None or r.seq!=len(ordered):
                    raise ValueError('capture end must be unique and final')
                end_record=r.seq
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
        if benchmark.startswith('tau_'):
            if pending_assistant_calls:
                if pending_assistant_calls.pop(0)!=seq:
                    raise ValueError('assistant boundary does not match action order')
            else:
                turns.append({'role':'assistant','text':'','calls':[seq], 'boundary_available':False})
    if pending_assistant_calls:
        raise ValueError('assistant boundary references missing actions')
    if [a['seq'] for a in trace]!=list(range(1,len(trace)+1)):
        raise ValueError('benchmark actions must be contiguous and ordered')
    if benchmark=='clawsbench':
        if len(initial)!=1 or len(native)!=1: raise ValueError('missing or duplicate capture attachments')
        trace,inputs,diagnostics=claws_adapter.replay_captured({
            'schema_version':1,'initial_facts':initial[0], 'agent_trace':trace, 'native_calls':native[0]})
    else:
        if end_record is None:
            raise ValueError('missing capture end')
        if len(tau_capture)!=1 or not isinstance(tau_capture[0].get('turns_available'),bool):
            raise ValueError('missing or invalid conversation availability')
        if not tau_capture[0]['turns_available']:
            if any(t['role']=='user' for t in turns):
                raise ValueError('user observations contradict missing conversation')
            turns=[]
        inputs=tau_adapter.build_inputs({'trace':trace,'turns':turns,'episode_complete':True},
                                       benchmark.removeprefix('tau_'))
        message_sequence_map[-(len(turns)+1)]=end_record
        for event in inputs.get('message_events') or []:
            seq=event.get('seq')
            if seq not in message_sequence_map or seq in sequence_map:
                raise ValueError('message obligation has no unique committed boundary')
            sequence_map[seq]=message_sequence_map[seq]
            trace.append(event)
        trace.sort(key=lambda a:sequence_map[a['seq']])
        diagnostics=[]
    return {'trace':trace,'inputs':inputs,'diagnostics':diagnostics,
            'sequence_map':sequence_map,'turns':turns if benchmark.startswith('tau_') else []}


def analyze_records(records, benchmark):
    result = observe_records(records, {'benchmark': benchmark})
    result['fires'] = predicates.run(result['trace'], result['inputs'])
    return result


def verify_case(records, benchmark, arms=None):
    profiles = ensure_registered()
    chosen = ARMS.get(benchmark, ()) if arms is None else arms
    if not chosen:
        raise ValueError('unknown benchmark or empty arm selection')
    definitions = {arm: promise_definition(benchmark, arm, profiles) for arm in chosen}
    acc = Accountability('offline-reference-evaluation')
    pids = {arm: acc.register_promise(sid, params, payout_wei=1)
            for arm, (sid, params) in definitions.items()}
    sdk = acc.self_check(records)
    restored = [ActionRecord.from_dict(d) for d in json.loads(json.dumps([r.to_dict() for r in records]))]
    verdicts, bindings = {}, {}
    for arm, pid in pids.items():
        sid, params = definitions[arm]
        committed = registry.predicate_hash_for(sid)
        bound = registry.resolve_hash(committed)
        if bound is None or bound.spec_id != sid:
            raise AssertionError('registered catalog source did not resolve')
        other = bound.evaluate(restored, params)
        if other != sdk[pid]:
            raise AssertionError('SDK/verifier verdict mismatch')
        verdicts[arm] = {'violated': other.violated, 'record_seq': other.seq, 'reason': other.reason}
        bindings[arm] = {'predicate': sid, 'predicate_hash': committed,
                         'params': params, 'params_hash': params_hash(params)}
    result = analyze_records(restored, benchmark)
    unknown = {f.get('arm') for f in result['fires']} - set(ARMS[benchmark])
    if unknown:
        raise ValueError(f'unregistered firing policy arms: {sorted(unknown)}')
    for arm in chosen:
        if verdicts[arm]['violated'] != any(f.get('arm') == arm for f in result['fires']):
            raise AssertionError('fire ledger disagrees with registered verdict')
    result.update({'verdicts': verdicts, 'sdk_verifier_parity': True,
                   'promise_bindings': bindings,
                   'predicate_hashes': {arm: b['predicate_hash'] for arm, b in bindings.items()},
                   'observation_profile_hash': profiles[benchmark].profile_hash,
                   'trace_hash': trace_hash(restored)})
    return result
