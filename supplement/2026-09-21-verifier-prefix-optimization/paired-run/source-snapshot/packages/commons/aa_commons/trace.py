"""The trace: an ordered, append-only log of consequential actions.

A trace is just ``list[ActionRecord]``. Its hash is reproducible byte-for-byte across
the SDK (which builds it) and the verifier (which reloads it from the store) — that
reproducibility is design invariant I3.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .ids import hash_obj


@dataclass(frozen=True)
class ActionRecord:
    seq: int                  # monotonic within a session
    session_id: str
    tool: str                 # action/vocabulary name (must be non-opaque; see design I2)
    args: dict[str, Any]
    result: Any               # serializable (or a hash, if large/sensitive)
    ts: int                   # epoch ms; MUST NOT affect a verdict unless the promise is about timing
    metadata: dict[str, Any] | None = None  # optional invocation/event identity; absent in legacy bytes

    def to_dict(self) -> dict:
        data = {
            "seq": self.seq,
            "session_id": self.session_id,
            "tool": self.tool,
            "args": self.args,
            "result": self.result,
            "ts": self.ts,
        }
        if self.metadata:
            data["metadata"] = self.metadata
        return data

    @staticmethod
    def from_dict(d: dict) -> "ActionRecord":
        return ActionRecord(
            seq=d["seq"],
            session_id=d["session_id"],
            tool=d["tool"],
            args=d["args"],
            result=d["result"],
            ts=d["ts"],
            metadata=d.get("metadata") or None,
        )


def _ordered(records: list[ActionRecord]) -> list[ActionRecord]:
    return sorted(records, key=lambda r: r.seq)


def trace_hash(records: list[ActionRecord]) -> str:
    """keccak256 over the canonical encoding of the seq-ordered record list."""
    return hash_obj([r.to_dict() for r in _ordered(records)])


def trace_to_json(records: list[ActionRecord]) -> str:
    return json.dumps([r.to_dict() for r in _ordered(records)])


def trace_from_json(s: str) -> list[ActionRecord]:
    return [ActionRecord.from_dict(d) for d in json.loads(s)]


def is_blocked(record: ActionRecord) -> bool:
    """Legacy/native nonexecution marker: an attempt known not to have run has no effect.

    Retained for historical predicate compatibility; the SDK has no predicate gate mode.
    """
    return isinstance(record.result, dict) and "blocked" in record.result


def check_prefixes(records: list[ActionRecord], checkpoints: list[dict], session_id: str) -> None:
    """Validate all historical cumulative prefixes, including earlier ones a later root overwrote.

    Every trace belongs to the challenged session, even without checkpoints. Checkpoints count
    records, not sequence maxima, so checkpointed traces must also be contiguous. Neither check
    changes the legacy record encoding or its hash.
    """
    if any(r.session_id != session_id for r in records):
        raise ValueError("trace records belong to another session")
    if not checkpoints:
        return
    ordered = _ordered(records)
    if any(r.session_id != session_id or r.seq != i for i, r in enumerate(ordered, 1)):
        raise ValueError("checkpoint trace is not a contiguous single-session log")
    last = 0
    for checkpoint in checkpoints:
        count = checkpoint["record_count"]
        if not isinstance(count, int) or isinstance(count, bool) or not last < count <= len(ordered):
            raise ValueError("checkpoint count is inconsistent with the final trace")
        if trace_hash(ordered[:count]) != checkpoint["prefix_hash"]:
            raise ValueError(f"checkpoint prefix mismatch at record {count}")
        last = count
