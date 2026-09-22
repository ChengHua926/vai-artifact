"""The promise catalog — the live, on-chain-auditable index of registered predicates.

Like an ERC registry: every promise a provider can register is a *numbered, versioned*
entry (AAP-N) whose ``predicate_hash`` is ``keccak(the predicate's source)``. That hash is
exactly what a provider commits on-chain at registration, so anyone can pull the source
here and confirm it matches the chain — the meaning of a promise is auditable, not a label.

    python scripts/catalog.py                # the numbered index (+ hashes)
    python scripts/catalog.py --source AAP-1 # the exact source AAP-1's on-chain hash commits to
"""
from __future__ import annotations

import sys

import _config as C  # bind the catalog to this checkout before importing protocol code
from aa_commons import registry


def _resolve(key: str) -> str:
    """Map an AAP-N number or a spec_id to a spec_id."""
    key = key.strip()
    for entry in registry.catalog():
        if key in (entry["number"], entry["spec_id"]):
            return entry["spec_id"]
    sys.exit(f"unknown promise {key!r}; known: " +
             ", ".join(e["number"] for e in registry.catalog()))


def print_index() -> None:
    print("Promise catalog (AAP — Agent Accountability Promise)\n")
    for e in registry.catalog():
        print(f"  {e['number']}  {e['spec_id']}@v{e['version']}")
        print(f"       {e['statement']}")
        print(f"       predicate_hash {e['predicate_hash']}")
        print()
    print("Each predicate_hash = keccak(source). A provider commits it on-chain at")
    print("registerPromise; `--source <AAP-N>` prints the source it commits to.")


def print_source(key: str) -> None:
    spec_id = _resolve(key)
    spec = registry.get(spec_id)
    src = registry.predicate_source(spec_id)
    print(f"# {spec.number}  {spec_id}@v{spec.version}")
    print(f"# statement      : {spec.doc}")
    print(f"# predicate_hash : {registry.predicate_hash_for(spec_id)}")
    print(f"#   == keccak(the source below) — this is what binds on-chain\n")
    print(src)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--source":
        print_source(sys.argv[2])
    else:
        print_index()
