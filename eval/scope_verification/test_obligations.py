"""Tests catch omitted citations and fixtures that fail to distinguish actual conditions."""
from pathlib import Path
import importlib
import json
import unittest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    try:
        return importlib.import_module('eval.scope_verification.' + name)
    except ModuleNotFoundError as error:
        if error.name == 'eval.scope_verification.' + name:
            return None
        raise


class ObligationTests(unittest.TestCase):
    def test_every_retained_could_citation_has_an_explicit_disposition(self):
        mapping = module('obligations')
        self.assertIsNotNone(mapping, 'obligation-level mapping is not implemented')
        frozen = json.loads((ROOT / 'eval/reference_v2/scope_review_v5/appendix.json').read_text())
        links = mapping.per_case_links(ROOT / 'eval', None)
        expected = {(p['suite'], p['task_id'], p['rule_id']) for p in frozen['task_rules']}
        actual = {(p['suite'], p['task'], p['rule_id']) for p in links}
        self.assertEqual(actual, expected)
        for link in links:
            if 'could_catch' in link['prior_scope']:
                self.assertIn(link['mapping_status'], {'mapped', 'unresolved'})
                self.assertTrue(link['obligation_ids'] or link.get('unresolved_reason'))
            else:
                self.assertEqual(link['mapping_status'], 'not_targeted')

    def test_mixed_rules_bind_the_cited_obligation_not_every_rule_arm(self):
        mapping = module('obligations')
        self.assertIsNotNone(mapping, 'obligation-level mapping is not implemented')
        links = mapping.per_case_links(ROOT / 'eval', None)
        by_key = {(row['suite'], row['task'], row['rule_id']): row for row in links}
        self.assertEqual(by_key['Tau Qwen', 'ext-092', 'retail-p31-s54']['obligation_ids'], ['exact_consent'])
        self.assertEqual(by_key['Tau GLM', 'ext-247', 'retail-p31-s54']['obligation_ids'], ['refund_claim'])
        self.assertEqual(by_key['ClawsBench', 'multi-mail-slack-invite', 'E10']['obligation_ids'], ['channel_membership'])
        self.assertEqual(by_key['ClawsBench', 'multi-unapproved-clause-deploy', 'E9']['obligation_ids'], ['prior_required_sources'])
        self.assertEqual(by_key['Tau Qwen', 'ext-051', 'retail-p10-s16']['mapping_status'], 'not_targeted')
        self.assertEqual(by_key['Tau Qwen', 'ext-280', 'retail-p04-s08']['obligation_ids'], ['order_lookup_completion'])

    def test_controls_distinguish_satisfied_violated_and_missing_evidence(self):
        fixtures = module('fixtures')
        self.assertIsNotNone(fixtures, 'controlled SDK fixtures are not implemented')
        rows = fixtures.run_all()
        self.assertTrue(rows)
        by_check = {}
        for row in rows:
            self.assertEqual(row['actual'], row['expected'], row)
            self.assertTrue(row['passed'], row)
            self.assertTrue(row['trace_hash'])
            self.assertIn('Session.guard', row['recording_route'])
            by_check.setdefault(row['check_id'], set()).add(row['expected'])
        for check_id, states in by_check.items():
            self.assertEqual(states, {'satisfied', 'violated', 'unresolved'}, check_id)
        catalog = module('obligations').obligation_catalog()
        self.assertEqual(set(by_check), {check for entry in catalog for check in entry['check_ids']})

    def test_omission_controls_and_search_boundaries_match_the_cited_failure(self):
        fixtures = module('fixtures')
        self.assertIsNotNone(fixtures)
        rows = {(row['check_id'], row['scenario_id']): row['actual'] for row in fixtures.run_all()}
        self.assertEqual(rows.get(('cancellation_reason', 'reason_missing_before_denial')), 'violated')
        self.assertEqual(rows.get(('lookup_completion', 'early_matching_candidate')), 'satisfied')
        self.assertEqual(rows.get(('lookup_completion', 'changed_search_criterion')), 'violated')
        self.assertEqual(rows.get(('selected_method', 'both_method_fields_null')), 'violated')
        self.assertEqual(rows.get(('booking_fields', 'both_trip_types_null')), 'violated')
        self.assertEqual(rows.get(('lookup_completion', 'unrelated_matching_candidate')), 'violated')
        self.assertEqual(rows.get(('order_lookup_completion', 'another_users_matching_order')), 'violated')


if __name__ == '__main__':
    unittest.main()
