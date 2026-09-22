"""Focused boundary tests for the extracted, benchmark-independent sequence rule."""
import unittest
from aa_commons.sequence_checks import ConsecutiveEqualResults
from aa_commons.constraints import evaluate

class SequenceBoundaries(unittest.TestCase):
    def setUp(self):
        self.machine = ConsecutiveEqualResults()

    def request(self, n, identity='one', result='same', success=True, start=None, end=None):
        return self.machine.observe(identity=identity, succeeded=success, result=result,
                                    started=n*10 if start is None else start,
                                    completed=n*10+1 if end is None else end)

    def test_third_consecutive_after_two_equal_observations(self):
        self.assertEqual(self.request(1).status, 'passed')
        self.assertEqual(self.request(2).status, 'passed')
        decision = self.request(3)
        self.assertEqual(decision.status, 'fired')
        self.assertFalse(evaluate(decision.condition))
        self.assertEqual(decision.prior_results, [(11, 'same'), (21, 'same')])

    def test_current_result_cannot_retroactively_justify_current_invocation(self):
        self.request(1, result='a')
        self.request(2, result='b')
        self.assertEqual(self.request(3, result='b').status, 'passed')
        self.assertEqual(self.request(4, result='c').status, 'fired')

    def test_failed_operation_resets(self):
        self.request(1)
        self.request(2)
        self.assertEqual(self.request(3, success=False).status, 'rejected')
        self.assertEqual(self.request(4).status, 'passed')
        self.assertEqual(self.request(5).status, 'passed')
        self.assertEqual(self.request(6).status, 'fired')

    def test_different_action_breaks_consecutiveness(self):
        self.request(1)
        self.request(2)
        self.assertEqual(self.request(3, identity='other').status, 'passed')
        self.assertEqual(self.request(4).status, 'passed')
        self.assertEqual(self.request(5).status, 'passed')
        self.assertEqual(self.request(6).status, 'fired')

    def test_missing_full_result_breaks_sequence(self):
        self.request(1)
        self.request(2)
        self.assertEqual(self.request(3, result=None).status, 'unsupported')
        self.assertEqual(self.request(4).status, 'passed')

    def test_missing_identity_breaks_sequence(self):
        self.request(1)
        self.request(2)
        self.assertEqual(self.request(3, identity=None).status, 'unsupported')
        self.assertEqual(self.request(4).status, 'passed')

    def test_in_flight_result_does_not_authorize_earlier_decision(self):
        self.request(1, end=50)
        self.request(2, end=60)
        self.assertEqual(self.request(3, start=30, end=70).status, 'passed')
        self.assertEqual(self.request(4, start=80, end=90).status, 'fired')

    def test_completion_equal_to_start_is_not_prior(self):
        self.request(1)
        self.request(2, end=30)
        self.assertEqual(self.request(3, start=30).status, 'passed')

    def test_context_epoch_change_breaks_sequence(self):
        self.request(1, identity='object:epoch0')
        self.request(2, identity='object:epoch0')
        self.assertEqual(self.request(3, identity='object:epoch1').status, 'passed')

    def test_observation_limit_requires_positive_integer(self):
        for invalid in (0, -1, True, False, 2.0, '2', None):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                ConsecutiveEqualResults(observation_limit=invalid)
        self.assertEqual(ConsecutiveEqualResults(observation_limit=1).observation_limit, 1)
        self.assertEqual(ConsecutiveEqualResults(observation_limit=3).observation_limit, 3)

if __name__ == '__main__':
    unittest.main()
