"""Inter-rater agreement statistics for pairs of labelers.

pairwise() takes matched label pairs (one per item both labelers rated) and
returns raw agreement, Cohen's kappa, and Gwet's AC1. Kappa's chance term is
the product of the two labelers' own marginal rates, so it collapses toward
zero when one class dominates even at high raw agreement (the prevalence
paradox); AC1's chance term is 1/(Q-1) * sum_c pi_c (1 - pi_c) over the two
labelers' averaged marginals, which shrinks as one class dominates. The two
are reported side by side for that reason.
"""

from __future__ import annotations

from typing import Hashable, Iterable


def _marginals(pairs: list[tuple], side: int, categories: list) -> dict:
    n = len(pairs)
    return {category: sum(1 for pair in pairs if pair[side] == category) / n for category in categories}


def pairwise(pairs: Iterable[tuple[Hashable, Hashable]]) -> dict:
    """Agreement statistics over matched (label_a, label_b) pairs."""

    pairs = list(pairs)
    n = len(pairs)
    if n == 0:
        return {"n": 0, "agreements": 0, "percent": None, "kappa": None, "ac1": None}
    categories = sorted({a for a, _ in pairs} | {b for _, b in pairs}, key=repr)
    agreements = sum(1 for a, b in pairs if a == b)
    observed = agreements / n
    left, right = _marginals(pairs, 0, categories), _marginals(pairs, 1, categories)
    chance_kappa = sum(left[c] * right[c] for c in categories)
    kappa = (observed - chance_kappa) / (1 - chance_kappa) if chance_kappa < 1 else 1.0
    averaged = {c: (left[c] + right[c]) / 2 for c in categories}
    q = len(categories)
    chance_ac1 = sum(p * (1 - p) for p in averaged.values()) / (q - 1) if q > 1 else 0.0
    ac1 = (observed - chance_ac1) / (1 - chance_ac1) if chance_ac1 < 1 else 1.0
    return {
        "n": n,
        "agreements": agreements,
        "percent": round(observed, 4),
        "kappa": round(kappa, 4),
        "ac1": round(ac1, 4),
    }


def coflag_share(
    verdicts_a: dict, verdicts_b: dict, rules_a: dict, rules_b: dict, positive: Hashable
) -> dict:
    """Among items both labelers marked `positive`, how many share at least one
    cited rule key. rules_* map item -> set of keys."""

    co_flagged = sorted(
        (item for item in verdicts_a if verdicts_a.get(item) == positive and verdicts_b.get(item) == positive),
        key=repr,
    )
    shared = sum(1 for item in co_flagged if set(rules_a.get(item, ())) & set(rules_b.get(item, ())))
    return {"co_flagged": len(co_flagged), "same_rule": shared}
