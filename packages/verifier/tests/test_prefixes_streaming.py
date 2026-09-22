"""Compatibility with the committed JSON-list format and legacy validation rules."""
from dataclasses import replace
import random

import pytest

from aa_commons import ActionRecord, trace_hash
from aa_commons.ids import canonical_bytes, keccak_hex
from aa_commons.trace import check_prefixes as legacy
from aa_verifier import prefixes


SID = "test-session"


def records(n=8):
    return [ActionRecord(i, SID, "tool", {"text": "é中\\\"\n" * i},
                         {"ok": True, "nested": [None, -0.0, i]}, i,
                         {"invocation_id": str(i)} if i % 2 else None)
            for i in range(1, n + 1)]


def checkpoints(rs, counts):
    return [{"record_count": n, "prefix_hash": trace_hash(rs[:n])} for n in counts]


def outcome(fn, rs, cps, sid=SID):
    try:
        fn(rs, cps, sid)
    except Exception as exc:
        return type(exc), str(exc)
    return None


@pytest.mark.parametrize("seed", range(40))
def test_every_prefix_matches_legacy_bytes(seed):
    rng = random.Random(seed)
    rs = records(rng.randrange(1, 45))
    # Exercise Keccak block boundaries, Unicode, nested values, and empty metadata.
    rs = [replace(r, args={"payload": "x" * rng.randrange(0, 600),
                           "nested": [r.args, 1e-8, 1e20]},
                  metadata={} if r.seq % 3 == 0 else r.metadata) for r in rs]
    cps = checkpoints(rs, range(1, len(rs) + 1))
    rng.shuffle(rs)
    assert outcome(legacy, rs, cps) is None
    assert outcome(prefixes.check_prefixes_streaming, rs, cps) is None


@pytest.mark.parametrize("count", [None, False, True, "1", 1.0, -1, 0, 9])
def test_invalid_checkpoint_counts_match_legacy(count):
    rs = records()
    cps = [{"record_count": count, "prefix_hash": "0x00"}]
    assert outcome(prefixes.check_prefixes_streaming, rs, cps) == outcome(legacy, rs, cps)


@pytest.mark.parametrize("case", [
    "empty", "no-checkpoints", "no-checkpoints-gap", "wrong-session",
    "wrong-session-no-checkpoints", "gap", "duplicate", "tail-gap",
    "float-seq", "bool-seq", "reversed-checkpoints", "duplicate-checkpoints",
    "missing-count", "missing-hash", "early-mismatch", "late-mismatch",
    "mismatch-before-bad-count", "missing-hash-before-bad-count", "nan",
    "infinity", "unserializable", "uncheckpointed-nan", "changed-committed-record",
])
def test_malformed_and_tampered_inputs_match_legacy(case):
    rs = records()
    cps = checkpoints(rs, [2, 5, 8])
    if case == "empty": rs, cps = [], []
    if case == "no-checkpoints": cps = []
    if case == "no-checkpoints-gap": rs, cps = [rs[1]], []
    if case.startswith("wrong-session"):
        rs[-1] = replace(rs[-1], session_id="other")
        if case.endswith("no-checkpoints"): cps = []
    if case == "gap": rs.pop(2)
    if case == "duplicate": rs[2] = replace(rs[2], seq=2)
    if case == "tail-gap":
        cps = cps[:1]
        rs[-1] = replace(rs[-1], seq=99)
    if case in ("float-seq", "bool-seq"):
        rs[0] = replace(rs[0], seq=1.0 if case == "float-seq" else True)
        cps = checkpoints(rs, [2, 5, 8])
    if case == "reversed-checkpoints": cps.reverse()
    if case == "duplicate-checkpoints": cps.insert(1, cps[0])
    if case == "missing-count": del cps[0]["record_count"]
    if case in ("missing-hash", "missing-hash-before-bad-count"):
        del cps[0]["prefix_hash"]
    if case in ("early-mismatch", "mismatch-before-bad-count"):
        cps[0]["prefix_hash"] = "0x00"
    if case.endswith("before-bad-count"): cps[1]["record_count"] = False
    if case == "late-mismatch": cps[-1]["prefix_hash"] = "0x00"
    if case in ("nan", "infinity", "unserializable"):
        bad = {"nan": float("nan"), "infinity": float("inf"), "unserializable": {1, 2}}[case]
        rs[0] = replace(rs[0], result=bad)
    if case == "uncheckpointed-nan":
        cps = cps[:1]
        rs[-1] = replace(rs[-1], result=float("nan"))
    if case == "changed-committed-record":
        rs[0] = replace(rs[0], args={"text": "altered"})
    assert outcome(prefixes.check_prefixes_streaming, rs, cps) == outcome(legacy, rs, cps)


def test_checkpoint_digest_closes_a_copy_without_changing_running_state():
    state = prefixes.keccak_256(b"[")
    state.update(b"1")
    first = state.copy()
    first.update(b"]")
    assert "0x" + first.hexdigest() == keccak_hex(b"[1]")
    state.update(b",2")
    second = state.copy()
    second.update(b"]")
    assert "0x" + second.hexdigest() == keccak_hex(b"[1,2]")
    assert first.hexdigest() != second.hexdigest()


def test_each_record_is_encoded_once_even_with_a_checkpoint_per_record(monkeypatch):
    rs = records(100)
    cps = checkpoints(rs, range(1, 101))
    seen = []
    def encode(obj):
        seen.append(obj["seq"])
        return canonical_bytes(obj)
    monkeypatch.setattr(prefixes, "canonical_bytes", encode)
    prefixes.check_prefixes_streaming(rs, cps, SID)
    assert seen == list(range(1, 101))


def test_uncheckpointed_tail_is_not_encoded(monkeypatch):
    rs = records()
    cps = checkpoints(rs, [2])
    seen = []
    def encode(obj):
        seen.append(obj["seq"])
        return canonical_bytes(obj)
    monkeypatch.setattr(prefixes, "canonical_bytes", encode)
    prefixes.check_prefixes_streaming(rs, cps, SID)
    assert seen == [1, 2]
