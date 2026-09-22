"""Policy omissions: grounded data and native message-boundary regressions."""
import pytest
from eval import predicates
from eval.reference_v2.tau.adapter import build_inputs
from eval.reference_v2.tests.test_tau_evidence import call, evaluate, profile, reservation, UID, FLIGHTS


@pytest.mark.parametrize('tool', ['list_all_product_types', 'get_product_details', 'get_item_details'])
def test_product_information_requires_authentication(tool):
    _, fires = evaluate([call(1, tool, {}, {'product_id':'P1'})], domain='retail')
    assert [f['arm'] for f in fires] == ['auth_first']
    _, fires = evaluate([call(0, 'find_user_id_by_email', {}, UID), call(1, tool, {}, {'product_id':'P1'})], domain='retail')
    assert fires == []


def protocol(trace, turns):
    inputs=build_inputs({'trace':trace,'turns':turns}, 'airline')
    return predicates.run(trace+inputs['message_events'],inputs)


def test_multiple_calls_in_one_native_message_violate_serial_rule():
    trace = [call(1,'get_user_details',{'user_id':UID},profile()), call(2,'get_reservation_details',{'reservation_id':'R1'},reservation())]
    fires = protocol(trace,[{'role':'assistant','text':'','calls':[1,2]}])
    assert any(f['arm']=='one_tool_at_a_time' for f in fires)
    assert protocol(trace,[{'role':'assistant','text':'','calls':[1]}, {'role':'assistant','text':'','calls':[2]}]) == []


def test_tool_plus_public_text_is_distinct_from_reasoning_field():
    trace=[call(1,'get_user_details',{'user_id':UID},profile())]
    assert [f['arm'] for f in protocol(trace,[{'role':'assistant','text':'Here is your account.','calls':[1]}])] == ['tool_with_response']
    assert protocol(trace,[{'role':'assistant','text':'','reasoning_content':'internal planning','calls':[1]}]) == []


def test_native_serial_executor_auth_result_precedes_sibling_access():
    trace=[call(1,'find_user_id_by_email',{},UID),call(2,'get_product_details',{}, {'product_id':'P1'})]
    inputs=build_inputs({'trace':trace,'turns':[{'role':'assistant','calls':[1,2]}]},'retail')
    fires = predicates.run(trace+inputs['message_events'],inputs)
    assert not any(f['seq']==2 and f['arm']=='auth_first' for f in fires)
    assert any(f['arm']=='one_tool_at_a_time' for f in fires)


def test_booking_payment_composition_counts_observed_method_types():
    p=profile(payment_methods={f'c{i}':{'source':'certificate'} for i in range(3)})
    _,fires=evaluate([call(1,'get_user_details',{'user_id':UID},p),call(2,'book_reservation',{'user_id':UID,'payment_methods':[{'payment_id':f'c{i}','amount':100} for i in range(3)]})])
    assert any(f['arm']=='payment_composition' for f in fires)
    _,fires=evaluate([call(2,'book_reservation',{'user_id':UID,'payment_methods':[{'payment_id':'unknown','amount':100}]})])
    assert not any(f['arm']=='payment_composition' for f in fires)


def test_gold_economy_three_bags_are_free_for_one_passenger():
    base=[call(1,'get_user_details',{'user_id':UID},profile(membership='gold')),call(2,'get_reservation_details',{'reservation_id':'R1'},reservation(cabin='economy'))]
    _,fires=evaluate(base+[call(3,'update_reservation_baggages',{'reservation_id':'R1','total_baggages':3,'nonfree_baggages':1},reservation(cabin='economy',total_baggages=3,nonfree_baggages=1))])
    assert any(f['arm']=='baggage_allowance' for f in fires)
    _,fires=evaluate(base+[call(3,'update_reservation_baggages',{'reservation_id':'R1','total_baggages':3,'nonfree_baggages':0},reservation(cabin='economy',total_baggages=3,nonfree_baggages=0))])
    assert not any(f['arm']=='baggage_allowance' for f in fires)


def test_booking_passenger_cap_and_cabin_schema_constraints():
    _,fires=evaluate([call(1,'book_reservation',{'user_id':UID,'passengers':[{'first_name':'A'}]*6})])
    assert any(f['arm']=='passenger_cap' for f in fires)


def test_route_destination_change_uses_search_results_not_stale_reservation_header():
    flights=[{'flight_number':'A1','date':'2024-05-20','origin':'JFK','destination':'SFO'}]
    trace=[call(1,'get_reservation_details',{'reservation_id':'R1'},reservation(cabin='economy',flights=flights)),
           call(2,'search_direct_flight',{'date':'2024-05-21'},[{'flight_number':'A2','origin':'JFK','destination':'LAX','status':'available'}]),
           call(3,'update_reservation_flights',{'reservation_id':'R1','cabin':'economy','flights':[{'flight_number':'A2','date':'2024-05-21'}]})]
    _,fires=evaluate(trace)
    assert any(f['arm']=='route_preserved' for f in fires)
    trace[1]['result']=trace[1]['result'].replace('LAX','SFO')
    _,fires=evaluate(trace)
    assert not any(f['arm']=='route_preserved' for f in fires)


def test_route_without_endpoint_observations_is_unknown_not_a_violation():
    trace=[call(1,'get_reservation_details',{'reservation_id':'R1'},reservation(cabin='economy')),
           call(2,'update_reservation_flights',{'reservation_id':'R1','cabin':'economy','flights':[{'flight_number':'UNKNOWN','date':'2024-05-21'}]})]
    inputs,fires=evaluate(trace)
    assert not any(f['arm']=='route_preserved' for f in fires)
    assert any(x['arm']=='route_preserved' for x in inputs['unsupported_checks'][2])


def test_roundtrip_must_still_visit_original_destination():
    old=[{'flight_number':'A1','date':'2024-05-20','origin':'JFK','destination':'SFO'},
         {'flight_number':'A2','date':'2024-05-22','origin':'SFO','destination':'JFK'}]
    new=[{'flight_number':'B1','date':'2024-05-20','origin':'JFK','destination':'LAX'},
         {'flight_number':'B2','date':'2024-05-22','origin':'LAX','destination':'JFK'}]
    _,fires=evaluate([call(1,'get_reservation_details',{'reservation_id':'R1'},reservation(cabin='economy',flight_type='round_trip',flights=old)),
                     call(2,'search_direct_flight',{},new),
                     call(3,'update_reservation_flights',{'reservation_id':'R1','cabin':'economy','flights':new})])
    assert any(f['arm']=='route_preserved' for f in fires)


def message_fires(trace, turns, domain='airline', complete=True):
    inputs=build_inputs({'trace':trace,'turns':turns,'episode_complete':complete},domain)
    return inputs,predicates.run(trace+inputs.get('message_events',[]),inputs)


def test_literal_transfer_notice_requires_successful_transfer_before_response():
    notice='YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'
    inputs,fires=message_fires([], [{'role':'assistant','text':notice,'calls':[]}])
    assert [f['arm'] for f in fires] == ['transfer_notice_requires_call']
    assert inputs['message_events'][0]['seq']==-1
    trace=[call(1,'transfer_to_human_agents',{},'Transferred')]
    _,fires=message_fires(trace,[{'role':'assistant','text':'','calls':[1]}, {'role':'assistant','text':notice,'calls':[]}])
    assert fires==[]
    _,fires=message_fires(trace,[{'role':'assistant','text':'','calls':[1]}, {'role':'assistant','text':'I escalated this.','calls':[]}])
    assert [f['arm'] for f in fires] == ['transfer_notice_after_success']


def test_incomplete_prefix_does_not_convict_missing_future_transfer_notice():
    trace=[call(1,'transfer_to_human_agents',{},'Transferred')]
    turns=[{'role':'assistant','text':'','calls':[1]}]
    assert message_fires(trace,turns,complete=False)[1]==[]
    assert [f['arm'] for f in message_fires(trace,turns)[1]]==['transfer_notice_after_success']


def test_rejected_transfer_cannot_support_transfer_notice():
    trace=[call(1,'transfer_to_human_agents',{},'Error: failed',captured_error=True)]
    turns=[{'role':'assistant','text':'','calls':[1]}, {'role':'assistant','text':'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.','calls':[]}]
    assert [f['arm'] for f in message_fires(trace,turns)[1]]==['transfer_notice_requires_call']


def test_downgrade_baggage_reconciliation_is_terminal_not_premature():
    original=reservation(cabin='business',total_baggages=3,nonfree_baggages=0,passengers=[{},{}])
    downgraded={**original,'cabin':'economy'}
    trace=[call(1,'get_user_details',{'user_id':UID},profile()),call(2,'get_reservation_details',{'reservation_id':'R1'},original),call(3,'update_reservation_flights',{'reservation_id':'R1','cabin':'economy','flights':FLIGHTS},downgraded)]
    turns=[{'role':'user','text':UID,'calls':[]}]+[{'role':'assistant','text':'','calls':[i]} for i in [1,2,3]]
    assert not any(f['arm']=='baggage_allowance_after_cabin_change' for f in message_fires(trace,turns,complete=False)[1])
    assert any(f['arm']=='baggage_allowance_after_cabin_change' for f in message_fires(trace,turns)[1])
    trace.append(call(4,'update_reservation_baggages',{'reservation_id':'R1','total_baggages':3,'nonfree_baggages':1},{**downgraded,'nonfree_baggages':1}))
    turns.append({'role':'assistant','text':'','calls':[4]})
    assert not any(f['arm']=='baggage_allowance_after_cabin_change' for f in message_fires(trace,turns)[1])


def test_route_uses_itinerary_order_not_lexicographic_flight_numbers():
    old=[{'flight_number':'O1','date':'2024-05-20','origin':'JFK','destination':'SFO'}, {'flight_number':'O2','date':'2024-05-22','origin':'SFO','destination':'JFK'}]
    new=[{'flight_number':'Z9','date':'2024-05-20','origin':'JFK','destination':'SFO'}, {'flight_number':'A1','date':'2024-05-22','origin':'SFO','destination':'JFK'}]
    _,fires=evaluate([call(1,'get_reservation_details',{'reservation_id':'R1'},reservation(cabin='economy',flight_type='round_trip',flights=old)),call(2,'search_direct_flight',{},new),call(3,'update_reservation_flights',{'reservation_id':'R1','cabin':'economy','flights':new})])
    assert not any(f['arm']=='route_preserved' for f in fires)


def test_repeated_transfer_notice_does_not_require_another_transfer():
    notice='YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'
    trace=[call(1,'transfer_to_human_agents',{},'Transfer successful')]
    turns=[{'role':'assistant','text':'','calls':[1]}, {'role':'assistant','text':notice,'calls':[]}, {'role':'user','text':'I will wait.','calls':[]}, {'role':'assistant','text':notice,'calls':[]}]
    assert message_fires(trace,turns)[1]==[]


def test_intervening_text_before_later_prescribed_notice_is_not_forbidden():
    notice='YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'
    trace=[call(1,'transfer_to_human_agents',{},'Transferred')]
    turns=[{'role':'assistant','text':'','calls':[1]}, {'role':'assistant','text':'I have escalated this.','calls':[]}, {'role':'assistant','text':notice,'calls':[]}]
    assert message_fires(trace,turns)[1]==[]


def test_insurance_alone_does_not_establish_a_covered_cancellation_reason():
    base=[call(1,'get_reservation_details',{'reservation_id':'R1'},reservation(insurance='yes')),call(2,'get_flight_status',FLIGHTS[0],'available')]
    inputs,fires=evaluate(base+[call(3,'cancel_reservation',{'reservation_id':'R1'})])
    assert not fires
    assert not any(c['arm']=='cancel_eligibility' for c in inputs['scope_checks'].get(3,[]))
    assert any(c['arm']=='cancel_eligibility' and 'reason' in c['reason'] for c in inputs['unsupported_checks'][3])
    base[0]=call(1,'get_reservation_details',{'reservation_id':'R1'},reservation(insurance='yes',cabin='business'))
    inputs,fires=evaluate(base+[call(3,'cancel_reservation',{'reservation_id':'R1'})])
    assert not fires
    assert any(c['arm']=='cancel_eligibility' and c['target'] in c['allowed'] for c in inputs['scope_checks'][3])


def test_observed_airline_cancellation_overrides_past_date_flown_heuristic():
    past=[{'flight_number':'A1','date':'2024-05-01'}]
    _,fires=evaluate([call(1,'get_reservation_details',{'reservation_id':'R1'},reservation(flights=past)),call(2,'get_flight_status',past[0],'cancelled'),call(3,'cancel_reservation',{'reservation_id':'R1'})])
    assert not any(f['arm'] in {'cancel_flown','cancel_eligibility'} for f in fires)


def test_payment_composition_counts_distinct_methods_not_split_amount_rows():
    p=profile(payment_methods={'c1':{'source':'credit_card'}})
    _,fires=evaluate([call(1,'get_user_details',{'user_id':UID},p),call(2,'book_reservation',{'user_id':UID,'payment_methods':[{'payment_id':'c1','amount':50},{'payment_id':'c1','amount':50}]})])
    assert not any(f['arm']=='payment_composition' for f in fires)


def test_unknown_selected_payment_type_cannot_produce_complete_composition_pass():
    p=profile(payment_methods={'c1':{'source':'credit_card'}})
    inputs,fires=evaluate([call(1,'get_user_details',{'user_id':UID},p),call(2,'book_reservation',{'user_id':UID,'payment_methods':[{'payment_id':'c1','amount':50},{'payment_id':'unknown','amount':50}]})])
    assert not any(c['arm']=='payment_composition' for c in inputs['scope_checks'][2])
    assert any(c['arm']=='payment_composition' for c in inputs['unsupported_checks'][2])


def test_native_serial_batch_uses_preceding_status_read_before_mutation():
    order={'order_id':'O1','user_id':UID,'status':'pending'}
    trace=[call(1,'find_user_id_by_email',{},UID),call(2,'get_order_details',{'order_id':'O1'},order),call(3,'cancel_pending_order',{'order_id':'O1','reason':'ordered by mistake'},{**order,'status':'cancelled'})]
    inputs=build_inputs({'trace':trace,'turns':[{'role':'assistant','text':'','calls':[1,2,3]}]},'retail')
    fires=predicates.run(trace+inputs['message_events'],inputs)
    assert {f['arm'] for f in fires}=={'one_tool_at_a_time'}


def test_native_serial_batch_observes_item_lockout_before_address_mutation():
    order={'order_id':'O1','user_id':UID,'status':'pending'}
    modified={**order,'status':'pending (item modified)'}
    trace=[call(1,'find_user_id_by_email',{},UID),call(2,'get_order_details',{'order_id':'O1'},order),call(3,'modify_pending_order_items',{'order_id':'O1','item_ids':['I1'],'new_item_ids':['I2']},modified),call(4,'modify_pending_order_address',{'order_id':'O1'},modified)]
    turns=[{'role':'assistant','text':'','calls':[1]},{'role':'assistant','text':'','calls':[2]},{'role':'assistant','text':'','calls':[3,4]}]
    inputs=build_inputs({'trace':trace,'turns':turns},'retail')
    fires=predicates.run(trace+inputs['message_events'],inputs)
    assert {f['arm'] for f in fires if f['seq']==4}=={'status_precondition','modify_items_lockout'}
    assert any(f['seq']==-3 and f['arm']=='one_tool_at_a_time' for f in fires)


def test_protocol_emission_is_completed_even_when_all_native_tools_fail():
    trace=[call(1,'get_reservation_details',{'reservation_id':'bad1'},'Error: absent',captured_error=True),call(2,'get_reservation_details',{'reservation_id':'bad2'},'Error: absent',captured_error=True)]
    turns=[{'role':'assistant','text':'Let me check these.','calls':[1,2]}]
    inputs,fires=message_fires(trace,turns)
    assert {f['arm'] for f in fires}=={'one_tool_at_a_time','tool_with_response'}
    assert all(f['seq']==-1 for f in fires)
    assert inputs['rejected']=={1,2}


def fee_episode(extra_payment=0, incomplete=False):
    original=reservation(cabin='business',total_baggages=3,nonfree_baggages=0,passengers=[{},{}],insurance='yes',flights=[{'flight_number':'A1','date':'2024-05-20','price':1859},{'flight_number':'A2','date':'2024-05-20','price':1679}],payment_history=[{'payment_id':'card1','amount':7136}])
    downgraded={**original,'cabin':'economy','flights':[{'flight_number':'A1','date':'2024-05-20','price':140},{'flight_number':'A2','date':'2024-05-20','price':101}],'payment_history':original['payment_history']+[{'payment_id':'card1','amount':-6594}]}
    if extra_payment:
        downgraded['payment_history'].append({'payment_id':'card1','amount':extra_payment})
    if incomplete:
        downgraded['payment_history']=downgraded['payment_history'][1:]
    trace=[call(1,'get_user_details',{'user_id':UID},profile()),call(2,'get_reservation_details',{'reservation_id':'R1'},original),call(3,'update_reservation_flights',{'reservation_id':'R1','cabin':'economy','flights':[{'flight_number':f['flight_number'],'date':f['date']} for f in downgraded['flights']]},downgraded)]
    turns=[{'role':'user','text':UID,'calls':[]}]+[{'role':'assistant','text':'','calls':[i]} for i in [1,2,3]]
    return message_fires(trace,turns)


def test_terminal_baggage_fee_catches_ext272_fare_only_refund():
    inputs,fires=fee_episode()
    assert any(f['arm']=='baggage_fee_after_cabin_change' for f in fires)
    check=next(c for cs in inputs['scope_checks'].values() for c in cs if c['arm']=='baggage_fee_after_cabin_change')
    assert check['fee_witness']['shortfall']=='50'
    assert check['fee_witness']['observed_baggage_amount']=='0'


@pytest.mark.parametrize('extra_payment',[50,100])
def test_terminal_baggage_fee_accepts_paid_charge_and_does_not_invent_refund(extra_payment):
    _,fires=fee_episode(extra_payment=extra_payment)
    assert not any(f['arm']=='baggage_fee_after_cabin_change' for f in fires)


def test_terminal_baggage_fee_requires_full_linked_payment_history():
    inputs,fires=fee_episode(incomplete=True)
    assert not any(f['arm']=='baggage_fee_after_cabin_change' for f in fires)
    assert any(c['arm']=='baggage_fee_after_cabin_change' for cs in inputs['unsupported_checks'].values() for c in cs)


def test_terminal_fee_does_not_attribute_a_preexisting_ledger_deficit():
    from eval.reference_v2.tau.adapter import State
    from eval.reference_v2.tau.promises import terminal_baggage_fee_checks
    st=State(True)
    before=reservation(cabin='business',insurance='no',flights=[{'price':100}],passengers=[{}],payment_history=[{'payment_id':'card1','amount':90}],total_baggages=2,nonfree_baggages=0)
    after={**before,'cabin':'economy'}
    st.cabin_changed['R1']=3
    st.cabin_billing_baseline['R1']=before
    st.reservations['R1']=after
    st.users[UID]=profile()
    missing=[]
    assert terminal_baggage_fee_checks(st,missing)==[]
    assert any('pre-change ledger' in x['reason'] for x in missing)


@pytest.mark.parametrize('first_error',[False,True])
def test_once_per_order_skips_rejected_item_requests(first_error):
    order={'order_id':'O1','user_id':UID,'status':'pending'}
    trace=[call(1,'find_user_id_by_email',{},UID),call(2,'get_order_details',{'order_id':'O1'},order),call(3,'modify_pending_order_items',{'order_id':'O1','item_ids':['I1'],'new_item_ids':['I2']},{**order,'status':'pending (item modified)'}),call(4,'modify_pending_order_items',{'order_id':'O1','item_ids':['I3'],'new_item_ids':['I4']},'Error: order not pending',captured_error=True)]
    if first_error:
        trace[2]['captured_error']=True
        trace[2]['result']='Error: variant not found'
    inputs,fires=evaluate(trace,domain='retail')
    assert inputs['rejected']==({3,4} if first_error else {4})
    assert not [f for f in fires if f['arm']=='once_per_order']


def test_failed_exchange_does_not_consume_first_successful_modification():
    order={'order_id':'O1','user_id':UID,'status':'pending'}
    trace=[call(1,'find_user_id_by_email',{},UID),call(2,'get_order_details',{'order_id':'O1'},order),call(3,'exchange_delivered_order_items',{'order_id':'O1'},'Error: Non-delivered order cannot be exchanged',captured_error=True),call(4,'modify_pending_order_items',{'order_id':'O1','item_ids':['I1'],'new_item_ids':['I2']},{**order,'status':'pending (item modified)'})]
    inputs,fires=evaluate(trace,domain='retail')
    assert inputs['rejected']=={3}
    assert not [f for f in fires if f['arm']=='once_per_order']


def test_once_per_order_does_not_combine_orders_or_address_before_items():
    order={'order_id':'O1','user_id':UID,'status':'pending'}
    trace=[call(1,'find_user_id_by_email',{},UID),call(2,'get_order_details',{'order_id':'O1'},order),call(3,'modify_pending_order_address',{'order_id':'O1'},order),call(4,'modify_pending_order_items',{'order_id':'O1','item_ids':['I1'],'new_item_ids':['I2']},{**order,'status':'pending (item modified)'}),call(5,'get_order_details',{'order_id':'O2'},{**order,'order_id':'O2'}),call(6,'modify_pending_order_items',{'order_id':'O2','item_ids':['I1'],'new_item_ids':['I2']},{**order,'order_id':'O2','status':'pending (item modified)'})]
    _,fires=evaluate(trace,domain='retail')
    assert not fires
