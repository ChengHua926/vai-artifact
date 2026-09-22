"""Verify existing cumulative JSON-list commitments without rehashing every prefix."""
from __future__ import annotations

from sha3 import keccak_256

from aa_commons import ActionRecord
from aa_commons.ids import canonical_bytes


def check_prefixes_streaming(
    records: list[ActionRecord], checkpoints: list[dict], session_id: str,
) -> None:
    """Match ``aa_commons.trace.check_prefixes`` using one incremental Keccak state.

    The running state contains the canonical list without its closing bracket. At
    each checkpoint, copy that state and append ``]`` to the copy before digesting.
    This produces the *existing* trace_hash bytes, not a new hash-chain format.
    Records are encoded once through the last checkpoint; state copies have fixed
    size. The SDK's commitment construction and legacy validator stay unchanged.
    """
    if any(r.session_id != session_id for r in records):
        raise ValueError("trace records belong to another session")
    if not checkpoints:
        return
    ordered = sorted(records, key=lambda r: r.seq)
    if any(r.session_id != session_id or r.seq != i for i, r in enumerate(ordered, 1)):
        raise ValueError("checkpoint trace is not a contiguous single-session log")

    state = keccak_256(b"[")
    last = 0
    for checkpoint in checkpoints:
        count = checkpoint["record_count"]
        if not isinstance(count, int) or isinstance(count, bool) or not last < count <= len(ordered):
            raise ValueError("checkpoint count is inconsistent with the final trace")
        for index in range(last, count):
            if index:
                state.update(b",")
            state.update(canonical_bytes(ordered[index].to_dict()))
        closed = state.copy()
        closed.update(b"]")
        if "0x" + closed.hexdigest() != checkpoint["prefix_hash"]:
            raise ValueError(f"checkpoint prefix mismatch at record {count}")
        last = count
