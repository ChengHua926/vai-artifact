import unittest
from decimal import Decimal
from aa_commons.constraints import expr, fact, lit, evaluate


class ConstraintTests(unittest.TestCase):
    def test_fee_uses_policy_table_and_observed_payment_not_a_pass_flag(self):
        free = expr('mul', expr('add', expr('lookup', {'regular': 0, 'gold': 2}, fact('membership', 'regular')),
                               expr('lookup', {'economy': 1, 'business': 2}, fact('cabin', 'economy'))),
                    fact('passengers', 2))
        required = expr('mul', 50, expr('max', 0, expr('sub', fact('bags', 3), free)))
        self.assertEqual(evaluate(required), 50)
        self.assertEqual(evaluate(expr('ge', fact('baggage_paid', Decimal('0')), required)), False)
        self.assertEqual(evaluate(expr('ge', fact('baggage_paid', Decimal('50')), required)), True)

    def test_identity_and_status_are_different_checks(self):
        self.assertEqual(evaluate(expr('present', fact('authenticated_user', None))), False)
        self.assertEqual(evaluate(expr('in', fact('order_status', 'delivered'), ['pending'])), False)
        self.assertEqual(evaluate(expr('in_norm', 'HTTPS://EXAMPLE.COM/A/', ['example.com/a'])), True)
        self.assertEqual(evaluate(expr('ne', 'item-a', 'item-a')), False)

    def test_event_obligation_requires_later_success(self):
        condition = expr('any', [expr('gt', event, 7) for event in [2, 6]])
        self.assertEqual(evaluate(condition), False)
        condition = expr('any', [expr('gt', event, 7) for event in [2, 8]])
        self.assertEqual(evaluate(condition), True)
        self.assertEqual(evaluate(expr('all', [expr('lt', 1, 3), expr('eq', 'x', 'x')])), True)

    def test_collection_and_exact_money_operations(self):
        self.assertEqual(evaluate(expr('count', ['card', 'gift', 'gift'], 'gift')), 2)
        self.assertEqual(evaluate(expr('prefix', [1, 2], [1, 2, 3])), True)
        self.assertEqual(evaluate(expr('multiset_eq', ['a', 'a', 'b'], ['a', 'b', 'b'])), False)
        self.assertEqual(evaluate(expr('add', expr('decimal', '0.1'), expr('decimal', '0.2'))), Decimal('0.3'))

    def test_unknown_operations_fail_instead_of_silently_passing(self):
        with self.assertRaises(ValueError):
            evaluate(expr('not_a_known_operation', 1))





class ConstraintValidationTests(unittest.TestCase):
    def test_bad_shape_operator_and_arity_are_rejected_even_in_unused_branches(self):
        for node in (
            {"op": "eq", "args": [{"lit": 1}]},
            {"op": "not", "args": [{"lit": True}, {"lit": False}]},
            {"op": "eq", "args": "not a list"},
            {"op": "unknown", "args": []},
            {"op": "if", "args": [{"lit": True}, {"lit": 1}]},
            {"op": "and", "args": [{"lit": False}, {"op": "unknown", "args": []}]},
            {"fact": "", "value": 1},
        ):
            with self.subTest(node=node), self.assertRaises(ValueError):
                evaluate(node)

    def test_nonfinite_numbers_and_nested_callbacks_are_rejected(self):
        for value in (float('nan'), float('inf'), Decimal('NaN'), Decimal('Infinity'),
                      {'nested': [float('-inf')]}, {'nested': [lambda: True]}):
            with self.subTest(value=str(value)), self.assertRaises((TypeError, ValueError)):
                evaluate(lit(value))
        with self.assertRaises(ValueError):
            evaluate(expr('decimal', 'NaN'))
        with self.assertRaises(ValueError):
            evaluate(expr('mul', 1e308, 1e308))

    def test_short_circuit_preserves_absent_data_guards(self):
        absent_lookup = expr('lookup', {}, 'absent')
        self.assertFalse(evaluate(expr('and', False, absent_lookup)))
        self.assertTrue(evaluate(expr('or', True, absent_lookup)))
        self.assertEqual(evaluate(expr('if', True, 4, absent_lookup)), 4)

    def test_logical_operations_require_boolean_values(self):
        for condition in (
            expr('and', fact('authorized', 'false')),
            expr('or', fact('authorized', 'true')),
            expr('not', fact('authorized', 0)),
            expr('if', fact('authorized', []), True, False),
            expr('all', [True, 1]),
            expr('any', [False, 'false']),
        ):
            with self.subTest(condition=condition), self.assertRaisesRegex(ValueError, 'Boolean'):
                evaluate(condition)

    def test_logical_operand_validation_preserves_short_circuit(self):
        self.assertFalse(evaluate(expr('and', False, 'unused')))
        self.assertTrue(evaluate(expr('or', True, 'unused')))
        self.assertEqual(evaluate(expr('if', False, 'unused', 3)), 3)
        self.assertFalse(evaluate(expr('all', [False, 'unused'])))
        self.assertTrue(evaluate(expr('any', [True, 'unused'])))


class PolicyEngineTests(unittest.TestCase):
    def test_scope_uses_operands_and_preserves_reason_fields(self):
        from aa_commons.policy_engine import run
        action = {'seq': 2, 'tool': 'modify', 'args': {}}
        check = {'arm': 'status', 'target': 'delivered', 'allowed': ['delivered'],
                 'condition': expr('in', fact('observed_status', 'delivered'), ['pending']),
                 'detail': 'only pending orders may be modified'}
        expected = [{'seq': 2, 'tool': 'modify', 'predicate': 'scope', 'arm': 'status',
                     'targets': ['delivered'], 'detail': check['detail']}]
        self.assertEqual(run([action], {'scope_checks': {2: [check]}}), expected)
        check['condition'] = expr('in', fact('observed_status', 'pending'), ['pending'])
        self.assertEqual(run([action], {'scope_checks': {2: [check]}}), [])

    def test_scope_requires_boolean_condition_without_legacy_fallback(self):
        from aa_commons.policy_engine import run
        for check in ({'arm': 'status', 'target': 'x', 'allowed': ['x']},
                      {'arm': 'status', 'condition': lit('truthy')}):
            with self.subTest(check=check), self.assertRaises(ValueError):
                run([{'seq': 1}], {'scope_checks': {1: [check]}})

    def test_mandate_requires_explicit_amount_condition(self):
        from aa_commons.policy_engine import run
        action = {'seq': 2, 'tool': 'pay'}
        check = {'arm': 'fee', 'amount': 0, 'max_amount': 100,
                 'amount_condition': expr('ge', fact('paid', 0), 50), 'detail': 'fee is $50'}
        self.assertEqual(run([action], {'mandate_checks': {2: [check]}}), [
            {'seq': 2, 'tool': 'pay', 'predicate': 'mandate', 'arm': 'fee',
             'targets': ['amount'], 'detail': 'fee is $50'}])
        del check['amount_condition']
        with self.assertRaises(ValueError):
            run([action], {'mandate_checks': {2: [check]}})

    def test_rejected_effects_do_not_count_but_attempt_obligations_do(self):
        from aa_commons.policy_engine import run
        trace = [{'seq': n, 'tool': 'modify', 'args': {'item': 'x'}} for n in (1, 2, 3)]
        inputs = {'rejected': {2}, 'scope_checks': {2: [{'condition': lit(False)}]},
                  'count_caps': [{'tools': ['modify'], 'partition_arg': 'item', 'cap': 1}]}
        self.assertEqual(run(trace, inputs), [
            {'seq': 3, 'tool': 'modify', 'predicate': 'count', 'arm': None,
             'targets': ['x'], 'detail': 'call 2 > cap 1'}])
        inputs['attempt_scope_checks'] = {2: [{'condition': lit(False), 'arm': 'attempt'}]}
        self.assertEqual(run(trace, inputs)[0]['seq'], 2)
        inputs['count_caps'][0]['mode'] = 'attempts'
        self.assertEqual(run(trace, inputs)[-1]['seq'], 2)

    def test_count_limit_must_be_a_nonnegative_integer(self):
        from aa_commons.policy_engine import run
        for invalid in (-1, True, False, 1.0, 0.5, '1', None, float('inf'), float('nan')):
            with self.subTest(limit=invalid), self.assertRaisesRegex(ValueError, 'nonnegative integer'):
                run([], {'count_caps': [{'tools': ['modify'], 'cap': invalid}]})

    def test_zero_count_limit_fires_once_on_first_successful_matching_operation(self):
        from aa_commons.policy_engine import run
        trace = [{'seq': n, 'tool': 'modify'} for n in (1, 2, 3)]
        fires = run(trace, {'rejected': {1}, 'count_caps': [{'tools': ['modify'], 'cap': 0}]})
        self.assertEqual(fires, [{'seq': 2, 'tool': 'modify', 'predicate': 'count',
                                'arm': None, 'targets': ['_session'], 'detail': 'call 1 > cap 0'}])

    def test_count_tools_require_a_nonempty_collection_of_tool_names(self):
        from aa_commons.policy_engine import run
        for invalid in ('modify', None, [], {}, [None], [['modify']], [True], [''], ['  ']):
            with self.subTest(tools=invalid), self.assertRaisesRegex(ValueError, 'tool names'):
                run([], {'count_caps': [{'tools': invalid, 'cap': 1}]})

    def test_count_configuration_and_partition_errors_are_explicit(self):
        from aa_commons.policy_engine import run
        for invalid in (None, 'cap', [], 1):
            with self.subTest(cap=invalid), self.assertRaisesRegex(ValueError, 'count cap must be a dictionary'):
                run([], {'count_caps': [invalid]})
        for key in ('', 3, ['item']):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'partition_arg'):
                run([], {'count_caps': [{'tools': ['modify'], 'cap': 1, 'partition_arg': key}]})
        with self.assertRaisesRegex(ValueError, 'count cap mode'):
            run([], {'count_caps': [{'tools': ['modify'], 'cap': 1, 'mode': []}]})
        for partition in ([], {'item': 'x'}):
            with self.subTest(partition=partition), self.assertRaisesRegex(ValueError, 'hashable'):
                run([{'seq': 1, 'tool': 'modify', 'args': {'item': partition}}],
                    {'count_caps': [{'tools': ['modify'], 'cap': 1, 'partition_arg': 'item'}]})


    def test_mandate_requires_at_least_one_recipient_or_amount_restriction(self):
        from aa_commons.policy_engine import run
        for check in ({'arm': 'payment'}, {'arm': 'payment', 'allowlist': None}):
            with self.subTest(check=check), self.assertRaisesRegex(ValueError, 'restriction'):
                run([{'seq': 1}], {'mandate_checks': {1: [check]}})
