"""The predicate interface.

An objective promise is a *pure, deterministic* function of a canonical trace
(invariant I3). A ``PredicateSpec`` pairs the function with a stable id + version,
which together give it an on-chain identity via ``ids.predicate_hash``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .trace import ActionRecord


@dataclass(frozen=True)
class Verdict:
    violated: bool
    seq: Optional[int] = None      # the offending record, when violated
    reason: str = ""

    @staticmethod
    def satisfied() -> "Verdict":
        return Verdict(violated=False)

    @staticmethod
    def violation(seq: int, reason: str) -> "Verdict":
        return Verdict(violated=True, seq=seq, reason=reason)


EvaluateFn = Callable[[list[ActionRecord], dict], Verdict]


@dataclass(frozen=True)
class PredicateSpec:
    spec_id: str
    version: int
    evaluate: EvaluateFn
    doc: str = ""          # one-line, human-readable statement of what the predicate checks
    number: str = ""       # stable catalog number, ERC-style, e.g. "AAP-1"
    # Extensions with helper modules bind their complete implementation, not only
    # a thin evaluate() wrapper. None preserves every historical built-in hash.
    source_bundle: str | None = None
    validate_params: Callable[[dict], None] | None = None
