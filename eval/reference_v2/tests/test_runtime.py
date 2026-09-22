import pytest
from aa_commons import ActionRecord, registry
from aa_commons.predicate import PredicateSpec, Verdict


def _dummy(trace, params):
    return Verdict.satisfied()


def test_full_source_bundle_changes_hash_without_changing_historical_hashes():
    old = registry.predicate_hash_for('aggregate_within_cap')
    a = PredicateSpec('bundle_test_a', 1, _dummy, source_bundle='evaluator + reducer version one')
    b = PredicateSpec('bundle_test_b', 1, _dummy, source_bundle='evaluator + reducer version two')
    registry.register(a); registry.register(b)
    try:
        assert registry.predicate_hash_for(a.spec_id) != registry.predicate_hash_for(b.spec_id)
        assert registry.resolve_hash(registry.predicate_hash_for(a.spec_id)) is a
        assert registry.predicate_hash_for('aggregate_within_cap') == old
    finally:
        registry._REGISTRY.pop(a.spec_id); registry._REGISTRY.pop(b.spec_id)


def test_registered_runtime_rejects_precomputed_policy_decisions():
    from eval.reference_v2.runtime import make_tau_records, verify_case
    trace = [{'seq': 1, 'tool': 'send_certificate', 'args': {'user_id': 'u', 'amount': 75}, 'result': 'ok',
              'scope_checks': [{'allowed': ['u']}]}]
    records = make_tau_records(trace, [], 'session')
    # Decision caches in imported call dictionaries are not propagated into canonical records.
    assert 'scope_checks' not in str([r.to_dict() for r in records])
    with pytest.raises(ValueError, match='arm'):
        verify_case(records, 'tau_airline', arms=['invented_arm'])


def test_registered_exact_amount_and_sdk_hash_resolved_verifier_agree():
    from eval.reference_v2.runtime import make_tau_records, verify_case
    trace = [
        {'seq': 1, 'tool': 'get_user_details', 'args': {'user_id': 'u'}, 'result': '{"user_id":"u","membership":"gold","payment_methods":{},"reservations":["R"]}'},
        {'seq': 2, 'tool': 'get_reservation_details', 'args': {'reservation_id': 'R'}, 'result': '{"reservation_id":"R","user_id":"u","passengers":[{"name":"a"}],"cabin":"business"}'},
        {'seq': 3, 'tool': 'send_certificate', 'args': {'user_id': 'u', 'amount': 75}, 'result': 'ok'},
    ]
    result = verify_case(make_tau_records(trace, [], 'session'), 'tau_airline', arms=['certificate_mandate'])
    assert [(x['seq'], x['arm']) for x in result['fires']] == [(3, 'certificate_mandate')]
    assert result['sdk_verifier_parity'] is True
    assert result['verdicts']['certificate_mandate']['violated'] is True


def test_partitioned_counter_and_rejected_effects():
    from eval.reference_v2.runtime import make_tau_records, verify_case
    trace = [
        {'seq': 1, 'tool': 'modify_pending_order_items', 'args': {'order_id':'A'}, 'result':'ok'},
        {'seq': 2, 'tool': 'modify_pending_order_items', 'args': {'order_id':'B'}, 'result':'ok'},
    ]
    result=verify_case(make_tau_records(trace, [], 'session'), 'tau_retail', arms=['once_per_order'])
    assert result['verdicts']['once_per_order']['violated'] is False
    trace.append({'seq':3,'tool':'modify_pending_order_items','args':{'order_id':'A'},
                  'result':'not performed','captured_error':True})
    result=verify_case(make_tau_records(trace, [], 'session'), 'tau_retail', arms=['once_per_order'])
    assert result['verdicts']['once_per_order']['violated'] is False
    assert result['inputs']['rejected']=={3}
    assert 3 not in result['inputs']['scope_checks']
    assert not [f for f in result['fires'] if f['seq']==3]
    trace.append({'seq':4,'tool':'modify_pending_order_items','args':{'order_id':'A'},'result':'ok'})
    result=verify_case(make_tau_records(trace, [], 'session'), 'tau_retail', arms=['once_per_order'])
    assert result['verdicts']['once_per_order']['violated'] is True
    assert [f['seq'] for f in result['fires'] if f['arm']=='once_per_order']==[4]


def test_missing_conversation_does_not_become_proven_missing_user_id():
    from eval.reference_v2.runtime import make_tau_records, verify_case
    call={'seq':1,'tool':'book_reservation','args':{'user_id':'first_last_123','flights':[]},'result':'ok'}
    result=verify_case(make_tau_records([call],[],'session'),'tau_airline',arms=['user_id_from_user'])
    assert not result['verdicts']['user_id_from_user']['violated']
    assert any(c['arm']=='user_id_from_user' for c in result['inputs']['unsupported_checks'][1])


def test_actual_verifier_process_uses_catalog_and_bound_profile():
    import importlib.util
    from pathlib import Path
    from aa_commons import params_hash, trace_hash
    from aa_verifier import process_challenge
    from eval.reference_v2.runtime import ensure_registered, make_tau_records, promise_definition
    path=Path(__file__).resolve().parents[3]/'packages/verifier/tests/test_binding.py'
    module_spec=importlib.util.spec_from_file_location('binding_stubs',path)
    module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
    records=make_tau_records([
        {'seq':1,'tool':'modify_pending_order_items','args':{'order_id':'A'},'result':'ok'},
        {'seq':2,'tool':'modify_pending_order_items','args':{'order_id':'A'},'result':'ok'},
    ],[],module.SID)
    ensure_registered()
    spec_id, params=promise_definition('tau_retail','once_per_order')
    escrow=module.StubEscrow(trace_hash(records),registry.predicate_hash_for(spec_id),params_hash(params))
    store=module.StubStore([r.to_dict() for r in records],{'predicate':'untrusted hint','params':params})
    result=process_challenge(escrow,store,1,'0xVER')
    assert result['predicate']==spec_id
    assert result['violated'] is True
    assert result['seq']==3  # capture metadata then two real calls
    assert escrow.verdicts==[(1,True)]


def test_original_assistant_boundaries_and_public_text_survive_commitment(monkeypatch):
    from eval.reference_v2 import runtime
    calls=[{'seq':i,'tool':'search_direct_flight','args':{},'result':'[]'} for i in (1,2,3)]
    turns=[{'role':'user','text':'Find flights','calls':[]},
           {'role':'assistant','text':'Here is what I found.','calls':[1,2]},
           {'role':'assistant','text':'','calls':[3]}]
    observed=[]
    def capture(execution, domain):
        observed.append(execution['turns'])
        return {}
    monkeypatch.setattr(runtime.tau_adapter,'build_inputs',capture)
    records=runtime.make_tau_records(calls,turns,'session')
    runtime.analyze_records([ActionRecord.from_dict(r.to_dict()) for r in records],'tau_airline')
    assert observed==[turns]


def test_assistant_boundary_cannot_reassign_actions_to_another_turn():
    from eval.reference_v2 import runtime
    calls=[{'seq':i,'tool':'search_direct_flight','args':{},'result':'[]'} for i in (1,2)]
    turns=[{'role':'assistant','text':'','calls':[1]},
           {'role':'assistant','text':'','calls':[2]}]
    records=runtime.make_tau_records(calls,turns,'session')
    boundaries=[r for r in records if r.tool=='tau.assistant']
    assert len(boundaries)==2
    boundaries[0].result['calls']=[2]
    with pytest.raises(ValueError,match='assistant'):
        runtime.analyze_records(records,'tau_airline')


def test_message_obligation_witness_is_a_committed_message_record(monkeypatch):
    from eval.reference_v2 import runtime
    calls=[{'seq':1,'tool':'transfer_to_human_agents','args':{},'result':'ok'}]
    turns=[{'role':'assistant','text':'','calls':[1]},
           {'role':'assistant','text':'Incorrect transfer notice','calls':[]}]
    def message_check(execution, domain):
        return {'message_events':[{'seq':-2,'tool':'assistant_message','args':{},'result':turns[1]['text']}],
                'scope_checks':{-2:[{'arm':'tool_with_response','target':'incorrect','allowed':[], 'condition':{'op':'eq','args':[{'lit':'wrong'},{'lit':'required'}]}}]}}
    monkeypatch.setattr(runtime.tau_adapter,'build_inputs',message_check)
    records=runtime.make_tau_records(calls,turns,'session')
    result=runtime.verify_case(records,'tau_airline',arms=['tool_with_response'])
    assert result['verdicts']['tool_with_response']['violated']
    witness=records[result['verdicts']['tool_with_response']['record_seq']-1]
    assert witness.tool=='tau.assistant'
    assert witness.result['text']=='Incorrect transfer notice'


def test_every_firing_arm_must_have_a_registered_verdict(monkeypatch):
    from eval.reference_v2 import runtime
    monkeypatch.setattr(runtime.tau_adapter,'build_inputs',lambda execution,domain:
                        {'scope_checks':{1:[{'arm':'not_registered','target':'x','allowed':[], 'condition':{'op':'eq','args':[{'lit':'x'},{'lit':'y'}]}}]}})
    records=runtime.make_tau_records([{'seq':1,'tool':'example','args':{},'result':'ok'}],[],'session')
    with pytest.raises(ValueError,match='unregistered'):
        runtime.verify_case(records,'tau_airline')
