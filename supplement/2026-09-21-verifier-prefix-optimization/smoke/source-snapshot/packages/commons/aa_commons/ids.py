"""Identifiers and content-addressing.

Every hash is keccak256 (Ethereum-compatible) over a *canonical* JSON encoding, so
off-chain (SDK, verifier) and on-chain (Solidity, later) agree byte-for-byte. This is
what makes the binding invariant (I1) and deterministic adjudication (I3) hold.
"""
from __future__ import annotations

import json
from typing import Any

from Crypto.Hash import keccak


def canonical_bytes(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, compact separators, UTF-8.

    Use ints or strings for numeric values; floats are discouraged because their
    textual form is not reliably reproducible. NaN/Inf are rejected outright.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def keccak256(data: bytes) -> bytes:
    k = keccak.new(digest_bits=256)
    k.update(data)
    return k.digest()


def keccak_hex(data: bytes) -> str:
    return "0x" + keccak256(data).hex()


def hash_obj(obj: Any) -> str:
    """keccak256 over the canonical encoding of a JSON-serializable object."""
    return keccak_hex(canonical_bytes(obj))


def predicate_hash(source: str) -> str:
    """On-chain identity of a predicate: keccak of its source.

    Content-addressing the *logic* (not a name) means a third party can audit, from the
    on-chain hash plus the open predicate source, exactly what a promise checks — and any
    change to the predicate yields a different hash, so a promise's meaning cannot drift
    silently. (Requires the source to be available; the prototype installs `aa_commons` editable.)
    """
    return keccak_hex(source.encode("utf-8"))


def params_hash(params: dict) -> str:
    """On-chain identity of a promise's parameters."""
    return hash_obj(params)
