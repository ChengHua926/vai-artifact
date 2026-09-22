from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from eval.tau.corpus import load_tau_cases
from eval.tau.replay import classify_effect_shape, replay_case
from eval.tau import replay as replay_module
from eval.tau.tests._paths import tau_root


TAU_ROOT = tau_root()


def test_effect_shape_uses_writers_not_tool_multisets() -> None:
    written = {"writer_seq": 2}
    omitted = {"writer_seq": None}

    assert classify_effect_shape(0.0, True, [], []) == "response_only"
    assert classify_effect_shape(0.0, False, [omitted], []) == "omission_only"
    assert classify_effect_shape(0.0, False, [written], []) == "commission_only"
    assert classify_effect_shape(0.0, False, [written, omitted], []) == "mixed"
    assert classify_effect_shape(0.0, False, [written], ["bad replay"]) == "unknown"


def test_one_real_tau_replay_matches_the_native_db_verdict() -> None:
    case = next(
        case
        for case in load_tau_cases()
        if case["model_id"] == "glm47"
        and case["domain"] == "airline"
        and case["task_id"] == "0"
    )

    replay = replay_case(case, tau_root=TAU_ROOT)

    assert replay["replay_errors"] == []
    assert replay["computed_db_match"] is True
    assert replay["computed_db_match"] == case["recorded_db_match"]
    assert replay["effect_shape"] is None
    assert all(call["result_match"] for call in replay["calls"] if call["mutating"])
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS"] == "True"


def test_tau_runtime_overrides_caller_false_offline_guards(monkeypatch) -> None:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    monkeypatch.setenv("LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS", "False")

    replay_module._ensure_tau_runtime(TAU_ROOT)

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS"] == "True"


def test_replay_rejects_a_preloaded_tau_package_from_another_checkout(
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "tau2",
        SimpleNamespace(__file__="/tmp/unpinned-tau/tau2/__init__.py"),
    )

    try:
        replay_module._ensure_tau_runtime(TAU_ROOT)
    except ValueError as error:
        assert "different checkout" in str(error)
    else:
        raise AssertionError("preloaded Tau package was accepted")
