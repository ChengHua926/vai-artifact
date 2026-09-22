from __future__ import annotations

import pytest

from eval.tau.evaluate import evaluate_tau, summarize_case_rows
from eval.tau.tests._paths import tau_root


TAU_ROOT = tau_root()


@pytest.fixture(scope="module")
def full_evaluation() -> dict:
    if not (TAU_ROOT / ".venv" / "bin" / "python").exists():
        pytest.skip("pinned Tau environment is unavailable")
    return evaluate_tau(tau_root=TAU_ROOT)


def test_full_tau_evaluation_has_exhaustive_ledgers_and_pure_summary(
    full_evaluation: dict,
) -> None:
    rows = full_evaluation["case_rows"]
    calls = full_evaluation["call_rows"]
    effects = full_evaluation["effect_rows"]
    fires = full_evaluation["fire_rows"]

    assert len(rows) == 328
    assert len({row["case_id"] for row in rows}) == 328
    assert {row["case_id"] for row in calls} <= {row["case_id"] for row in rows}
    assert {row["case_id"] for row in effects} <= {row["case_id"] for row in rows}
    assert {row["case_id"] for row in fires} <= {row["case_id"] for row in rows}
    assert full_evaluation["summary"] == summarize_case_rows(rows)
    assert full_evaluation["monitor_manifest"]["implementation"] == "compiled_tau_monitor"


def test_native_denominators_and_old_fire_parity(full_evaluation: dict) -> None:
    models = full_evaluation["summary"]["models"]
    glm = models["glm47"]
    qwen = models["qwen3_30b"]

    assert glm["captured"] == qwen["captured"] == 164
    assert (glm["native_passes"], glm["native_failures"]) == (110, 54)
    assert (qwen["native_passes"], qwen["native_failures"]) == (64, 100)
    assert (glm["recorded_db_failures"], qwen["recorded_db_failures"]) == (51, 95)
    assert (glm["any_fire_runs"], qwen["any_fire_runs"]) == (10, 26)
    assert (glm["failure_fire_runs"], qwen["failure_fire_runs"]) == (7, 18)
    assert (glm["pass_fire_runs"], qwen["pass_fire_runs"]) == (3, 8)
    assert glm["replay_error_runs"] == qwen["replay_error_runs"] == 0
    assert full_evaluation["monitor_parity_mismatches"] == []


def test_exact_claims_are_bounded_by_same_writer_and_any_fire(
    full_evaluation: dict,
) -> None:
    for model in full_evaluation["summary"]["models"].values():
        assert (
            model["failure_exact_protected_effect_runs"]
            <= model["failure_same_writer_runs"]
            <= model["failure_fire_runs"]
        )
    exact = [
        row
        for row in full_evaluation["fire_rows"]
        if row["writer_relation"] == "exact_protected_effect"
    ]
    assert exact
    assert all(row["exact_links"] for row in exact)
    assert all(
        row["arm"]
        not in {
            "auth_first",
            "read_before_write",
            "user_id_from_user",
            "payment_in_profile",
        }
        for row in exact
    )
    assert {
        model: sum(row["model_id"] == model for row in exact)
        for model in ("glm47", "qwen3_30b")
    } == {"glm47": 2, "qwen3_30b": 7}
    assert all(row["gold_warnings"] == [] for row in exact)


def test_every_failure_and_pass_fire_has_a_mechanical_explanation(
    full_evaluation: dict,
) -> None:
    for row in full_evaluation["case_rows"]:
        if row["native_reward"] == 0.0 and not row["any_fire"]:
            assert row["no_fire_reason"] in {
                "response_only",
                "omission_no_writer",
                "no_applicable_check_on_wrong_writer",
                "applicable_checks_passed_on_wrong_writer",
                "mixed_monitor_surface_on_wrong_writers",
            }
        if row["native_reward"] == 1.0 and row["any_fire"]:
            assert row["pass_fire_reasons"]
            assert set(row["pass_fire_reasons"]) <= {
                "nonmutating_rejected_or_noop",
                "transient_or_overwritten",
                "persistent_gold_consistent",
            }
