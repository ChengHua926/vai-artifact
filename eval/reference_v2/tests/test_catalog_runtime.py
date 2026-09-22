from aa_commons import registry
from eval.reference_v2 import runtime


def test_current_replay_registers_catalog_promises_with_pinned_profiles():
    records = runtime.make_tau_records([
        {'seq': 1, 'tool': 'modify_pending_order_items', 'args': {'order_id': 'A'}, 'result': 'ok'},
        {'seq': 2, 'tool': 'modify_pending_order_items', 'args': {'order_id': 'A'}, 'result': 'ok'},
    ], [], 'catalog-test')
    result = runtime.verify_case(records, 'tau_retail', arms=['once_per_order'])
    assert 'promise_bindings' in result, 'replay still registers a benchmark-specific predicate'
    binding = result['promise_bindings']['once_per_order']
    assert binding['predicate'] == 'aggregate_within_cap'
    assert binding['params']['observation_profile'] == result['observation_profile_hash']
    assert result['verdicts']['once_per_order']['violated']
    assert registry.resolve_hash(binding['predicate_hash']).spec_id == binding['predicate']
    assert 'benchmark_policy_v4' not in registry.all_specs()
