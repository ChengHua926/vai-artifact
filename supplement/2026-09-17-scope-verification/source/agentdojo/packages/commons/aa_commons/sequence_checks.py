"""Reusable successful-operation sequence check, without service or policy names.

An invocation is constrained when the previous N consecutive successful requests
have the same context-sensitive identity, their full results have already been
observed, and those results are identical. The current result is incorporated
only after constructing the constraint for the invocation. Missing identity,
missing full observations, failure, and a changed identity reset the sequence.

The caller owns faithful extraction of request identity, result bytes, success,
and timestamps. This class neither identifies services nor reads benchmark data.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .constraints import expr, lit, fact, evaluate


@dataclass
class SequenceDecision:
    """Decision and previously observed results for one invocation."""

    status: str
    condition: dict | None
    prior_results: list[tuple[float, str]]


@dataclass
class ConsecutiveEqualResults:
    """Track repeated identical results for consecutive successful requests.

    Request identities should include any observed resource context that makes a
    later read different. The caller must supply complete result representations,
    rather than hashes of truncated previews or inferred outcomes.
    """

    observation_limit: int = 2
    previous_identity: str | None = None
    results: list[tuple[float, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if (not isinstance(self.observation_limit, int)
                or isinstance(self.observation_limit, bool)
                or self.observation_limit <= 0):
            raise ValueError("observation_limit must be a positive integer")

    def reset(self) -> None:
        """Forget the sequence when continuity or complete evidence is lost."""
        self.previous_identity = None
        self.results = []

    def observe(self, *, identity: str | None, succeeded: bool,
                result: str | None, started: float | None,
                completed: float | None) -> SequenceDecision:
        """Check prior observations, then incorporate the current successful result.

        Failure returns ``rejected``; incomplete evidence returns ``unsupported``.
        Both reset the sequence. Completed results must precede the current start
        strictly: an equal timestamp does not establish prior observation.
        """
        if not succeeded:
            prior = list(self.results)
            self.reset()
            return SequenceDecision("rejected", None, prior)
        if identity is None or result is None or started is None or completed is None:
            prior = list(self.results)
            self.reset()
            return SequenceDecision("unsupported", None, prior)
        if identity != self.previous_identity:
            self.results = []
        prior = list(self.results)
        comparisons = [
            expr("eq", expr("len", fact("prior_complete_results", prior)), lit(self.observation_limit)),
            *[expr("lt", fact("prior_result_observed_at", end), fact("invocation_started_at", started))
              for end, _ in prior],
            *[expr("eq", fact("prior_result", prior[0][1]), fact("other_prior_result", value))
              for _, value in prior[1:]],
        ]
        condition = expr("not", expr("and", *comparisons))
        status = "passed" if evaluate(condition) else "fired"
        self.results = (prior + [(completed, result)])[-self.observation_limit:]
        self.previous_identity = identity
        return SequenceDecision(status, condition, prior)
