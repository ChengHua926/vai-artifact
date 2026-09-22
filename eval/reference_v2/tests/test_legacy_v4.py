"""Old commitments must retain their original code, including imported policy helpers."""
from importlib import import_module
import json

import pytest

from aa_commons import params_hash, registry, trace_hash
from aa_commons.ids import predicate_hash


OLD_HASH = "0x69b4c18eea4a77458460d504f5e4babc6c75c6e82d49c9f2be7cae89a6856c8c"
SID = "0x" + "ab" * 32
PARAMS = {"benchmark": "tau_retail", "arm": "auth_first"}


def legacy():
    return import_module("eval.reference_v2.legacy_v4")


def records():
    return legacy().runtime().make_tau_records(
        [{"seq": 1, "tool": "get_order_details", "args": {"order_id": "order-1"},
          "result": {"order_id": "order-1", "user_id": "user-1", "status": "pending"}}],
        [{"role": "user", "text": "Read my order", "calls": []},
         {"role": "assistant", "text": "", "calls": [1]}], SID)


def test_historical_registration_preserves_hash_and_stays_out_of_catalog():
    spec = legacy().ensure_registered()
    assert predicate_hash(spec.source_bundle) == OLD_HASH
    assert registry.resolve_hash(OLD_HASH) is spec
    assert spec.spec_id == "benchmark_policy_v4"
    assert spec.spec_id not in registry.all_specs()
    assert legacy().ensure_registered() is spec
    assert legacy().runtime().source_bundle() == spec.source_bundle


def test_frozen_policy_is_unchanged_when_current_implementations_change(monkeypatch):
    spec = legacy().ensure_registered()
    trace = records()
    expected = spec.evaluate(trace, PARAMS)
    assert expected.violated and expected.seq == 4
    assert json.loads(expected.reason) == {
        "benchmark_seq": 1, "arm": "auth_first",
        "detail": "retail information/action requires prior successful find_user_id_*"}

    def changed(*args, **kwargs):
        raise AssertionError("historical evaluation imported mutable current code")

    for name, attr in (("eval.reference_v2.tau.adapter", "build_inputs"),
                       ("eval.clawsbench.analysis.adapter", "replay_captured"),
                       ("eval.predicates", "run")):
        monkeypatch.setattr(import_module(name), attr, changed)
    assert spec.evaluate(trace, PARAMS) == expected
    assert legacy().runtime().tau_adapter is not import_module("eval.reference_v2.tau.adapter")
    assert legacy().runtime().tau_adapter.promises is not import_module("eval.reference_v2.tau.promises")


def test_frozen_importer_rejects_unbundled_eval_code():
    module = legacy().runtime()
    frozen_import = module.__builtins__["__import__"]
    with pytest.raises(ImportError, match="not in the frozen"):
        frozen_import("eval.labeling.committee", module.__dict__, None, ("run",), 0)


def test_changed_frozen_source_is_rejected_before_execution():
    module = legacy()
    with pytest.raises(ValueError, match="committed hash"):
        module._load_frozen(module.SOURCE_FILE.read_text() + " ")


def test_historical_verifier_uses_committed_hash_and_original_reason():
    from aa_verifier import process_challenge

    spec = legacy().ensure_registered()
    trace = records()

    class Store:
        def get_records(self, session_id):
            return [record.to_dict() for record in trace]

        def get_promise(self, promise_id):
            return {"predicate": "ignored-current-id", "params": PARAMS}

    class Escrow:
        submitted = []

        def get_challenge(self, challenge_id):
            return (bytes.fromhex(SID[2:]), 1, "challenger", 1, 1, 100, 200)

        def get_promise(self, promise_id):
            return ("provider", bytes.fromhex(OLD_HASH[2:]),
                    bytes.fromhex(params_hash(PARAMS)[2:]), 1, 1, 50, 0)

        def get_session(self, session_id):
            return ("provider", "user", bytes.fromhex(trace_hash(trace)[2:]), 60, 90, True)

        def get_checkpoints(self, session_id):
            return []

        def submit_verdict(self, verifier, challenge_id, violated):
            self.submitted.append(violated)
            return {"paidToChallenger": int(violated)}

    escrow = Escrow()
    result = process_challenge(escrow, Store(), 1, "verifier")
    expected = spec.evaluate(trace, PARAMS)
    assert result["violated"] is True
    assert result["reason"] == expected.reason
    assert result["seq"] == expected.seq
    assert escrow.submitted == [True]
