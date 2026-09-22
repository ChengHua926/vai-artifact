"""Deterministic predicate self-check (no chain, no LLM).

Runs each registered predicate over crafted traces and asserts the verdict — the same
function the verifier runs on a challenge. This is the fast, LLM-free proof that the promises
fault exactly when they should; the live agent demo (start_demo.py) shows it end to end.
"""
import os
import sys

import _config as C  # bind self-checks to the same checkout as the lifecycle scripts
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))

from aa_commons import ActionRecord, registry
import aa_commons.predicates  # noqa: F401  (importing registers the catalog)

from demo_agent import CONSENT_PARAMS, SCOPE_PARAMS  # the exact params the provider registers


def rec(seq, tool, args, result="ok"):
    return ActionRecord(seq=seq, session_id="0xsess", tool=tool, args=args, result=result, ts=0)


CONSENT = "no_destructive_without_consent"
SCOPE = "action_within_declared_scope"

# (name, spec_id, params, records, expect_violated)
CASES = [
    ("AAP-1  standing grant covers the delete", CONSENT, CONSENT_PARAMS, [
        rec(1, "user_grant", {"scope": "workspace/tmp/"}, "granted"),
        rec(2, "delete_file", {"target": "workspace/tmp/cache.bin"}, "deleted"),
    ], False),
    ("AAP-1  rogue delete OUTSIDE the grant -> violation", CONSENT, CONSENT_PARAMS, [
        rec(1, "user_grant", {"scope": "workspace/tmp/"}, "granted"),
        rec(2, "delete_file", {"target": "workspace/reports/q3.pdf"}, "deleted"),
    ], True),
    ("AAP-1  per-action consent covers it", CONSENT, CONSENT_PARAMS, [
        rec(1, "user_consent", {"target": "workspace/reports/q3.pdf"}, "granted"),
        rec(2, "delete_file", {"target": "workspace/reports/q3.pdf"}, "deleted"),
    ], False),
    ("AAP-1  acted despite a DENY -> violation", CONSENT, CONSENT_PARAMS, [
        rec(1, "user_consent", {"target": "workspace/reports/q3.pdf"}, "denied"),
        rec(2, "delete_file", {"target": "workspace/reports/q3.pdf"}, "deleted"),
    ], True),
    ("AAP-1  revoked grant -> violation", CONSENT, CONSENT_PARAMS, [
        rec(1, "user_grant", {"scope": "workspace/tmp/"}, "granted"),
        rec(2, "user_revoke", {"scope": "workspace/tmp/"}, "revoked"),
        rec(3, "delete_file", {"target": "workspace/tmp/cache.bin"}, "deleted"),
    ], True),
    ("AAP-2  read inside workspace/ -> ok", SCOPE, SCOPE_PARAMS, [
        rec(1, "read_file", {"target": "workspace/notes.txt"}),
    ], False),
    ("AAP-2  read OUTSIDE workspace/ -> violation", SCOPE, SCOPE_PARAMS, [
        rec(1, "read_file", {"target": "outside.txt"}),
    ], True),
    ("AAP-2  `..` traversal out of scope -> violation (v2)", SCOPE, SCOPE_PARAMS, [
        rec(1, "read_file", {"target": "workspace/../outside.txt"}),
    ], True),
]


def main() -> None:
    ok = True
    for name, spec_id, params, records, expect in CASES:
        v = registry.get(spec_id).evaluate(records, params)
        passed = v.violated == expect
        ok = ok and passed
        mark = "PASS" if passed else "FAIL"
        detail = f"VIOLATED @seq {v.seq}" if v.violated else "ok"
        print(f"  [{mark}] {name:48s} -> {detail}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
