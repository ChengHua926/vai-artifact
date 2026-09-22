"""Dawid-Skene: unanimity, a contrarian worker, missing votes, ties, determinism."""

from __future__ import annotations

import numpy as np
import pytest

from eval.labeling import dawid_skene


def _votes(table: dict[str, dict[str, str]]) -> list[tuple]:
    return [(item, worker, label) for item, row in table.items() for worker, label in row.items()]


def test_unanimous_votes_give_certain_posteriors_and_near_identity_matrices():
    table = {
        "t1": {"a": "v", "b": "v", "c": "v"},
        "t2": {"a": "n", "b": "n", "c": "n"},
        "t3": {"a": "v", "b": "v", "c": "v"},
    }
    result = dawid_skene.fit(_votes(table))
    assert result.consensus("t1") == "v" and result.consensus("t2") == "n"
    assert result.posterior("t1")["v"] > 0.99
    for worker in "abc":
        matrix = result.worker_matrix(worker)
        assert matrix["v"]["v"] > 0.95 and matrix["n"]["n"] > 0.95


def test_contrarian_worker_is_outvoted_and_gets_a_flipped_matrix():
    table = {}
    for i in range(12):
        truth = "v" if i % 2 else "n"
        flipped = "n" if truth == "v" else "v"
        table[f"t{i}"] = {"a": truth, "b": truth, "c": truth, "d": truth, "z": flipped}
    result = dawid_skene.fit(_votes(table))
    for i in range(12):
        assert result.consensus(f"t{i}") == ("v" if i % 2 else "n")
    z = result.worker_matrix("z")
    assert z["v"]["n"] > 0.9 and z["n"]["v"] > 0.9
    a = result.worker_matrix("a")
    assert a["v"]["v"] > 0.9 and a["n"]["n"] > 0.9


def test_missing_votes_are_allowed():
    # Worker a has a track record on both labels (t1, t4), so its lone vote on
    # t2 carries weight; items with partial coverage still get a consensus.
    table = {
        "t1": {"a": "v", "b": "v"},
        "t2": {"a": "n"},
        "t3": {"b": "v", "c": "n", "a": "v"},
        "t4": {"a": "n", "b": "n", "c": "n"},
    }
    result = dawid_skene.fit(_votes(table))
    assert result.consensus("t1") == "v"
    assert result.consensus("t2") == "n"
    assert result.consensus("t3") == "v"
    assert result.consensus("t4") == "n"


def test_exact_tie_resolves_to_first_sorted_label_and_majority_returns_none():
    votes = [("t1", "a", "violation"), ("t1", "b", "no_violation")]
    result = dawid_skene.fit(votes)
    assert result.consensus("t1") == "no_violation"
    assert dawid_skene.majority(votes) == {"t1": None}


def test_deterministic_across_runs():
    table = {f"t{i}": {"a": "v" if i % 3 else "n", "b": "v" if i % 2 else "n", "c": "v"} for i in range(9)}
    first, second = dawid_skene.fit(_votes(table)), dawid_skene.fit(_votes(table))
    assert np.array_equal(first.posteriors, second.posteriors)
    assert np.array_equal(first.confusion, second.confusion)
    assert first.iterations == second.iterations


def test_duplicate_vote_is_rejected():
    with pytest.raises(ValueError):
        dawid_skene.fit([("t1", "a", "v"), ("t1", "a", "n")])


def test_smoothing_keeps_every_cell_positive():
    table = {f"t{i}": {"a": "v", "b": "v", "c": "v"} for i in range(5)}
    result = dawid_skene.fit(_votes(table))
    assert (result.confusion > 0).all()
    assert np.allclose(result.confusion.sum(axis=2), 1.0)
