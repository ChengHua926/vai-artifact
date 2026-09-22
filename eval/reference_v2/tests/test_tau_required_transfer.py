"""Required handoff uses bound cancellation targets, observed facts and episode closure."""
import pytest

from eval import predicates
from eval.reference_v2.tau.adapter import build_inputs
from eval.reference_v2.tests.test_tau_evidence import call, reservation


ARM = 'required_transfer_after_flown_cancellation'
NOTICE = 'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'


def past(rid='ABC123', date='2024-05-14'):
    return reservation(reservation_id=rid, flights=[{'flight_number': 'A1', 'date': date}])


def read(seq, rid='ABC123', date='2024-05-14'):
    return call(seq, 'get_reservation_details', {'reservation_id': rid}, past(rid, date))


def user(text):
    return {'role': 'user', 'text': text, 'calls': []}


def assistant(*seqs, text=''):
    return {'role': 'assistant', 'text': text, 'calls': list(seqs)}


def evaluate(trace, turns, complete=True):
    inputs = build_inputs({'trace': trace, 'turns': turns, 'episode_complete': complete}, 'airline')
    fires = predicates.run(trace + inputs['message_events'], inputs)
    return inputs, [fire for fire in fires if fire['arm'] == ARM]


def test_explicit_cancellation_target_requires_handoff_but_unrelated_old_booking_does_not():
    trace = [read(1), read(2, 'DEF456')]
    turns = [user('The reservation IDs I want to cancel are ABC123. The reservation ID I want to change is DEF456.'),
             assistant(1), assistant(2), assistant(text='Cancellation is not possible.')]
    inputs, fires = evaluate(trace, turns)
    assert [f['targets'] for f in fires] == [['ABC123']]
    assert fires[0]['tool'] == 'tau.episode_end'
    check = next(c for c in inputs['scope_checks'][-5] if c['arm'] == ARM)
    assert check['linked_action_seq'] == 1
    assert check['observation_seq'] == 1
    assert check['request_turn_index'] == 0
    assert check['binding'] == 'explicit_user_request'


def test_successful_cancel_result_can_establish_flown_facts_without_hindsight_at_cancel():
    trace = [call(1, 'cancel_reservation', {'reservation_id': 'ABC123'}, past())]
    inputs, fires = evaluate(trace, [assistant(1), assistant(text='Done.'), user('Goodbye! ###STOP###')])
    assert [f['targets'] for f in fires] == [['ABC123']]
    assert not any(c['arm'] == 'cancel_flown' for c in inputs['scope_checks'].get(1, []))
    check = next(c for c in inputs['scope_checks'][-4] if c['arm'] == ARM)
    assert check['binding'] == 'successful_cancellation'
    assert check['observation_seq'] == 1


@pytest.mark.parametrize('transfer_position,failed,want_fire', [
    ('before', False, True), ('after', False, False), ('after', True, True),
])
def test_only_successful_transfer_after_obligation_discharges_it(transfer_position, failed, want_fire):
    order = ['transfer', 'read'] if transfer_position == 'before' else ['read', 'transfer']
    trace = [read(i) if kind == 'read' else call(i, 'transfer_to_human_agents', {},
             'Error: failed' if failed else 'Transferred', captured_error=failed)
             for i, kind in enumerate(order, 1)]
    turns = [user('Please cancel reservation ABC123.'), assistant(1), assistant(2), assistant(text=NOTICE)]
    assert bool(evaluate(trace, turns)[1]) is want_fire


def test_request_after_old_read_and_transfer_requires_a_new_transfer():
    trace = [read(1), call(2, 'transfer_to_human_agents', {}, 'Transferred')]
    turns = [assistant(1), assistant(2), assistant(text=NOTICE), user('Please cancel reservation ABC123.'),
             assistant(text='It has already flown.')]
    assert [f['targets'] for f in evaluate(trace, turns)[1]] == [['ABC123']]


@pytest.mark.parametrize('date', ['2024-05-15', '2024-05-20', 'unknown'])
def test_unknown_same_day_and_future_dates_do_not_prove_required_handoff(date):
    assert evaluate([read(1, date=date)], [user('Please cancel reservation ABC123.'), assistant(1)])[1] == []


def test_direct_observed_not_flown_status_overrides_date_heuristic():
    trace = [call(1, 'get_flight_status', {'flight_number': 'A1', 'date': '2024-05-14'}, 'available'), read(2)]
    assert evaluate(trace, [user('Please cancel reservation ABC123.'), assistant(1), assistant(2)])[1] == []


def test_rejected_observation_does_not_establish_flown_facts():
    observation = read(1)
    observation['captured_error'] = True
    assert evaluate([observation], [user('Please cancel reservation ABC123.'), assistant(1)])[1] == []


def test_incomplete_episode_does_not_convict_a_future_omission():
    assert evaluate([read(1)], [user('Please cancel reservation ABC123.'), assistant(1)], complete=False)[1] == []


@pytest.mark.parametrize('user_request', [
    'Please show me reservation ABC123.',
    'Do not cancel reservation ABC123.',
    'If I cancel reservation ABC123, what happens?',
    'The policy says "cancel reservation ABC123".',
    'Please cancel reservation DEF456.',
])
def test_no_handoff_is_inferred_from_mention_negation_hypothetical_or_other_target(user_request):
    assert evaluate([read(1)], [user(user_request), assistant(1)])[1] == []


def test_multiple_explicit_cancellation_ids_are_bound_in_the_same_request():
    trace = [read(1), read(2, 'DEF456')]
    turns = [user('The reservation IDs I want to cancel are ABC123 and DEF456.'), assistant(1), assistant(2)]
    assert [f['targets'] for f in evaluate(trace, turns)[1]] == [['ABC123'], ['DEF456']]


def test_registered_promise_reports_completed_episode_witness():
    from eval.reference_v2.runtime import make_tau_records, verify_case
    records = make_tau_records([read(1)], [user('Please cancel reservation ABC123.'), assistant(1)], 'required-transfer')
    result = verify_case(records, 'tau_airline', arms=[ARM])
    assert result['sdk_verifier_parity'] is True
    assert result['verdicts'][ARM]['violated'] is True
    assert result['fires'][0]['targets'] == ['ABC123']
    assert result['sequence_map'][result['fires'][0]['seq']] == len(records)


def test_later_flight_status_is_included_in_required_transfer_evidence():
    trace = [read(1, date='2024-05-15'), call(2, 'transfer_to_human_agents', {}, 'Transferred'),
             call(3, 'get_flight_status', {'flight_number': 'A1', 'date': '2024-05-15'}, 'landed')]
    turns = [user('Please cancel reservation ABC123.'), assistant(1), assistant(2), assistant(text=NOTICE), assistant(3)]
    inputs, fires = evaluate(trace, turns)
    assert [f['targets'] for f in fires] == [['ABC123']]
    check = next(c for c in inputs['scope_checks'][-6] if c['arm'] == ARM)
    assert check['linked_action_seqs'] == [1, 3]
    assert check['required_after_seq'] == 3


def test_withdrawal_before_flown_observation_removes_only_the_named_request():
    trace = [read(1), read(2, 'DEF456')]
    turns = [user('The reservation IDs I want to cancel are ABC123 and DEF456.'),
             user('Please do not cancel reservation ABC123.'), assistant(1), assistant(2)]
    assert [f['targets'] for f in evaluate(trace, turns)[1]] == [['DEF456']]


def test_user_withdrawal_cannot_erase_a_successful_cancellation_obligation():
    trace = [call(1, 'cancel_reservation', {'reservation_id': 'ABC123'}, past())]
    turns = [user('Please cancel reservation ABC123.'), assistant(1),
             user('Please do not cancel reservation ABC123.')]
    assert [f['targets'] for f in evaluate(trace, turns)[1]] == [['ABC123']]


def test_withdrawal_after_flown_observation_does_not_erase_triggered_policy_duty():
    trace = [read(1)]
    turns = [user('Please cancel reservation ABC123.'), assistant(1),
             user('Please do not cancel reservation ABC123.')]
    assert [f['targets'] for f in evaluate(trace, turns)[1]] == [['ABC123']]


def test_conflicting_instructions_in_one_user_turn_do_not_create_a_prose_binding():
    turns = [user('Please cancel reservation ABC123. Please do not cancel reservation ABC123.'), assistant(1)]
    assert evaluate([read(1)], turns)[1] == []
