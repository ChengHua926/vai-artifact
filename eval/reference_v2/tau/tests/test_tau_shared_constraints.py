"""Tau condition behavior, independent of corpus counts and legacy answers."""
import copy
import unittest
from types import SimpleNamespace
from aa_commons.constraints import evaluate
from eval.reference_v2.tau import promises as p


def find(checks, arm):
    return next(c for c in checks if c['arm'] == arm)


class TauExpressions(unittest.TestCase):
    def test_authentication_before_access(self):
        call = {'tool': 'get_product_details', 'args': {}}
        for auth_id, expected in [(None, False), ('user_1', True)]:
            checks, _ = p.retail_checks(SimpleNamespace(auth_id=auth_id), call)
            self.assertIs(evaluate(find(checks, 'auth_first')['condition']), expected)

    def test_genuine_owner_membership_preserves_normalization(self):
        st = SimpleNamespace(auth_id='USER_ONE_1', orders={}, items_modified=set())
        checks, _ = p.retail_checks(st, {'tool': 'get_user_details', 'args': {'user_id': 'user_one_1'}})
        self.assertIs(evaluate(find(checks, 'one_user')['condition']), True)

    def test_status_pending_vs_delivered(self):
        st = SimpleNamespace(auth_id='u', orders={'o': {'user_id': 'u', 'status': 'delivered'}}, items_modified=set())
        checks, _ = p.retail_checks(st, {'tool': 'modify_pending_order_address', 'args': {'order_id': 'o'}})
        c = find(checks, 'status_precondition')
        self.assertIs(evaluate(c['condition']), False)
        # The expression consumes operands, not the legacy allowed outcome.
        c['allowed'] = [c['target']]
        self.assertIs(evaluate(c['condition']), False)

    def test_baggage_table_and_counterfactual_payment(self):
        baseline = {'status': 'confirmed', 'user_id': 'u', 'cabin': 'business', 'passengers': [{}, {}],
                    'flights': [{'price': 100}], 'insurance': 'no', 'total_baggages': 3,
                    'nonfree_baggages': 0, 'payment_history': [{'payment_id': 'card', 'amount': 200}]}
        current = copy.deepcopy(baseline)
        current['cabin'] = 'economy'
        current['flights'][0]['price'] = 80
        current['payment_history'].append({'payment_id': 'card', 'amount': -40})
        st = SimpleNamespace(cabin_changed={'r': 3}, reservations={'r': current},
                             cabin_billing_baseline={'r': baseline}, users={'u': {'membership': 'regular'}})
        c = p.terminal_baggage_fee_checks(st)[0]
        self.assertIs(evaluate(c['condition']), False)
        self.assertEqual(c['fee_witness']['shortfall'], '50')
        current['payment_history'].append({'payment_id': 'card', 'amount': 50})
        repaired = p.terminal_baggage_fee_checks(st)[0]
        self.assertIs(evaluate(repaired['condition']), True)
        self.assertEqual(repaired['fee_witness']['shortfall'], '0')

    def test_bag_allowance_member_and_cabin_boundaries(self):
        self.assertEqual(evaluate(p._required_bags({'membership': 'regular'}, 'economy', [{}, {}], 3)), 1)
        self.assertEqual(evaluate(p._required_bags({'membership': 'silver'}, 'economy', [{}, {}], 3)), 0)
        self.assertEqual(evaluate(p._required_bags({'membership': 'regular'}, 'basic_economy', [{}, {}], 3)), 3)

    def test_flown_evidence_overrides_date(self):
        res = {'flights': [{'flight_number': 'F', 'date': '2024-05-01'}]}
        self.assertIs(evaluate(p._flown_condition(res, {})), True)
        self.assertIs(evaluate(p._flown_condition(res, {('F','2024-05-01'): 'available'})), False)

    def test_handoff_needs_actual_success_event(self):
        for seen in [False, True]:
            checks, _ = p.transfer_message_checks({'text': p.TRANSFER_NOTICE}, [], seen)
            self.assertIs(evaluate(checks[0]['condition']), seen)

    def test_lockout_emits_both_pass_and_failure(self):
        for modified, expected in [(set(), True), ({'o'}, False)]:
            st = SimpleNamespace(auth_id='u', orders={'o': {'user_id': 'u', 'status': 'pending'}}, items_modified=modified)
            checks, _ = p.retail_checks(st, {'tool': 'modify_pending_order_address', 'args': {'order_id': 'o'}})
            self.assertIs(evaluate(find(checks, 'modify_items_lockout')['condition']), expected)

    def test_flown_change_and_cancellation_emit_passes(self):
        for status, proposed_cabin, allowed_change in [('available','economy',True), ('landed','economy',False), ('landed','business',True)]:
            res = {'user_id':'u', 'cabin':'business', 'origin':'A','destination':'B','flight_type':'one_way',
                   'flights':[{'flight_number':'F','date':'2024-05-01'}], 'created_at':'2024-05-15T01:00:00', 'insurance':'no'}
            st = SimpleNamespace(reservations={'r':res}, users={}, turns_available=True, user_ids_mentioned={'u'},
                                 flight_obs={('F','2024-05-01'):status}, flight_routes={})
            call = {'tool':'update_reservation_flights','args':{'reservation_id':'r','cabin':proposed_cabin,'flights':res['flights']}}
            checks, _ = p.airline_checks(st, call)
            self.assertIs(evaluate(find(checks, 'flown_no_cabin_change')['condition']), allowed_change)
            checks, _ = p.airline_checks(st, {'tool':'cancel_reservation','args':{'reservation_id':'r'}})
            self.assertIs(evaluate(find(checks, 'cancel_flown')['condition']), status == 'available')

    def test_adapter_terminal_missing_notice(self):
        from eval.reference_v2.tau import adapter as tau_adapter
        from aa_commons import policy_engine as predicate_engine
        execution = {'episode_complete':True,
                     'trace':[{'seq':1,'tool':'transfer_to_human_agents','args':{},'result':'Transfer successful'}],
                     'turns':[{'role':'assistant','text':'','calls':[1]}, {'role':'assistant','text':'Done.','calls':[]}]}
        inputs = tau_adapter.build_inputs(execution, 'airline')
        fires = predicate_engine.run(execution['trace'] + inputs['message_events'], inputs)
        self.assertEqual(fires, [{'seq':-3,'tool':'tau.episode_end','predicate':'scope',
                                'arm':'transfer_notice_after_success','targets':['missing-notice'],
                                'detail':'completed captured episode ends without prescribed notice after transfers [1]'}])

    def test_protocol_message_bounds(self):
        checks = p.protocol_checks({'calls': [1,2], 'text': ''})
        self.assertIs(evaluate(find(checks,'one_tool_at_a_time')['condition']), False)
        self.assertIs(evaluate(find(checks,'tool_with_response')['condition']), True)

if __name__ == '__main__':
    unittest.main()
