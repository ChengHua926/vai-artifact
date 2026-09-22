"""Agreement statistics against hand-computed values from the calibration round."""

from __future__ import annotations

from eval.labeling import agreement

V, N = "violation", "no_violation"


def test_tau_humans_low_raw_agreement_gives_near_zero_kappa_and_negative_ac1():
    # Human A vs Human B on Tau: 7 both-violation, 3 both-clean, 20 Human A-only.
    pairs = [(V, V)] * 7 + [(N, N)] * 3 + [(V, N)] * 20
    stats = agreement.pairwise(pairs)
    assert stats["n"] == 30 and stats["agreements"] == 10
    assert abs(stats["kappa"] - 0.07) < 0.02
    assert abs(stats["ac1"] - (-0.31)) < 0.02


def test_clawsbench_prevalence_paradox_kappa_near_zero_ac1_high():
    # 24 both-clean, 4 Human A-only violations, 2 Human B-only violations.
    pairs = [(N, N)] * 24 + [(V, N)] * 4 + [(N, V)] * 2
    stats = agreement.pairwise(pairs)
    assert stats["agreements"] == 24
    assert abs(stats["kappa"]) < 0.15
    assert stats["ac1"] > 0.7


def test_perfect_agreement_is_one_and_empty_is_none():
    assert agreement.pairwise([(V, V), (N, N)])["kappa"] == 1.0
    assert agreement.pairwise([])["kappa"] is None


def test_coflag_share_counts_shared_rule_keys_only_on_co_flagged_items():
    verdicts_a = {"t1": V, "t2": V, "t3": N}
    verdicts_b = {"t1": V, "t2": N, "t3": V}
    rules_a = {"t1": {"Cancel"}, "t2": {"Auth"}}
    rules_b = {"t1": {"Cancel", "Refund"}, "t3": {"Auth"}}
    assert agreement.coflag_share(verdicts_a, verdicts_b, rules_a, rules_b, V) == {"co_flagged": 1, "same_rule": 1}
