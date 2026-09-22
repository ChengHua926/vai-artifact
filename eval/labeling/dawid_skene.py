"""Dawid-Skene (1979) consensus over categorical votes, unsupervised, numpy only.

Input is a flat list of (item, worker, label) votes; a worker may skip items.
The model alternates two steps until the per-item class posteriors stop
moving: estimate a class prior and one confusion matrix per worker from the
current posteriors (M-step), then recompute each item's posterior from the
prior and the workers' matrices (E-step). Initialisation is each item's raw
vote proportions, so the first pass is a plain majority vote and every later
pass re-weights workers by how often they agreed with the emerging consensus.

Everything is sorted before use and the iteration order is fixed, so the same
votes always give the same result. Additive smoothing keeps every confusion
cell strictly positive; without it a worker who never emitted some label
would make that label impossible for every item they touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable

import numpy as np

Vote = tuple[Hashable, Hashable, Hashable]


@dataclass(frozen=True)
class Result:
    items: tuple
    workers: tuple
    labels: tuple
    posteriors: np.ndarray  # shape (items, labels)
    confusion: np.ndarray  # shape (workers, true label, observed label)
    priors: np.ndarray  # shape (labels,)
    iterations: int
    converged: bool

    def posterior(self, item: Hashable) -> dict:
        row = self.posteriors[self.items.index(item)]
        return {label: float(value) for label, value in zip(self.labels, row)}

    def consensus(self, item: Hashable) -> Hashable:
        """The most probable label; an exact tie resolves to the first label in
        sorted order, so a 50/50 split never convicts on its own."""

        row = self.posteriors[self.items.index(item)]
        best = row.max()
        return next(label for label, value in zip(self.labels, row) if value == best)

    def worker_matrix(self, worker: Hashable) -> dict:
        """P(worker says observed | truth is true), as nested dicts."""

        matrix = self.confusion[self.workers.index(worker)]
        return {
            true: {observed: float(matrix[i, j]) for j, observed in enumerate(self.labels)}
            for i, true in enumerate(self.labels)
        }


def _index(votes: list[Vote]) -> tuple[tuple, tuple, tuple, np.ndarray]:
    items = tuple(sorted({item for item, _, _ in votes}, key=repr))
    workers = tuple(sorted({worker for _, worker, _ in votes}, key=repr))
    labels = tuple(sorted({label for _, _, label in votes}, key=repr))
    item_at = {item: i for i, item in enumerate(items)}
    worker_at = {worker: i for i, worker in enumerate(workers)}
    label_at = {label: i for i, label in enumerate(labels)}
    counts = np.zeros((len(items), len(workers), len(labels)))
    for item, worker, label in votes:
        if counts[item_at[item], worker_at[worker]].any():
            raise ValueError(f"worker {worker!r} voted more than once on item {item!r}")
        counts[item_at[item], worker_at[worker], label_at[label]] = 1.0
    return items, workers, labels, counts


def fit(votes: Iterable[Vote], n_iter: int = 100, tol: float = 1e-8, smoothing: float = 0.01) -> Result:
    votes = list(votes)
    if not votes:
        raise ValueError("no votes")
    if smoothing <= 0:
        raise ValueError("smoothing must be positive")
    items, workers, labels, counts = _index(votes)
    n_labels = len(labels)
    per_item = counts.sum(axis=1)  # (items, labels): how many workers said each label
    totals = per_item.sum(axis=1, keepdims=True)
    posteriors = np.where(totals > 0, per_item / np.maximum(totals, 1), 1.0 / n_labels)

    converged = False
    iterations = 0
    for iterations in range(1, n_iter + 1):
        priors = posteriors.mean(axis=0)
        # confusion[w, j, l] = P(worker w says l | truth j), from soft counts
        soft = np.einsum("ij,iwl->wjl", posteriors, counts) + smoothing
        confusion = soft / soft.sum(axis=2, keepdims=True)
        log_post = np.log(priors)[None, :] + np.einsum("iwl,wjl->ij", counts, np.log(confusion))
        log_post -= log_post.max(axis=1, keepdims=True)
        updated = np.exp(log_post)
        updated /= updated.sum(axis=1, keepdims=True)
        shift = float(np.abs(updated - posteriors).max())
        posteriors = updated
        if shift < tol:
            converged = True
            break
    priors = posteriors.mean(axis=0)
    soft = np.einsum("ij,iwl->wjl", posteriors, counts) + smoothing
    confusion = soft / soft.sum(axis=2, keepdims=True)
    return Result(items, workers, labels, posteriors, confusion, priors, iterations, converged)


def majority(votes: Iterable[Vote]) -> dict:
    """Plurality label per item; None when the top labels tie."""

    tallies: dict = {}
    for item, _, label in votes:
        tallies.setdefault(item, {}).setdefault(label, 0)
        tallies[item][label] += 1
    out = {}
    for item, counts in tallies.items():
        best = max(counts.values())
        leaders = sorted((label for label, count in counts.items() if count == best), key=repr)
        out[item] = leaders[0] if len(leaders) == 1 else None
    return out
