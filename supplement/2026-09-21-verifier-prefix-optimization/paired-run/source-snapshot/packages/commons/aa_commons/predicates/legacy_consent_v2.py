"""AAP-1 (v2) — no destructive action without USER consent.

The consent promise. A destructive action is permitted iff the USER consented to it earlier
in the session, at one of two granularities:

  • per-action  — a ``user_consent`` for that exact target (result ``"granted"``); a ``"denied"``
                  does NOT authorize it (acting despite a deny is a violation).
  • standing    — an active scoped ``user_grant`` whose scope is a prefix of the target
                  (e.g. "auto-accept deletes under workspace/tmp/ this session"), as long as it
                  has not since been revoked by a matching ``user_revoke``.

This models how real agents delegate authority — a one-off yes, or a session-scoped
auto-accept (Claude Code's auto-accept mode; Hermes' once/session/always). Consent here is
USER-ORIGINATED: the record carries the user's actual decision, not an approval the agent
issues to itself. Each consent/grant record is structured as the future signable payload
``{principal, scope|target, nonce, session}``; cryptographic signing is deferred under the
honest-but-curious model (the single upgrade that closes the malicious-provider gap — see docs).

v2 supersedes v1 (``no_destructive_without_approval``, which keyed on an agent-issued
``request_approval`` gate). Same number (AAP-1, "the consent promise"); new logic ⇒ new
predicate_hash — the versioning discipline working, not bypassed.

params:
  destructive_tools: list[str]
  consent_tool:      str = "user_consent"   # per-action; args carry {match_key: target, ...}, result granted/denied
  grant_tool:        str = "user_grant"     # standing scoped grant; args carry {scope_key: prefix, ...}
  revoke_tool:       str = "user_revoke"    # ends a standing grant; args carry {scope_key: prefix, ...}
  granted_result:    Any = "granted"
  match_key:         str = "target"
  scope_key:         str = "scope"
"""
from __future__ import annotations

from ..predicate import PredicateSpec, Verdict
from ..registry import register_historical as register
from ..trace import ActionRecord, is_blocked

SPEC_ID = "no_destructive_without_consent"
VERSION = 2


def evaluate(trace: list[ActionRecord], params: dict) -> Verdict:
    destructive = set(params["destructive_tools"])
    consent_tool = params.get("consent_tool", "user_consent")
    grant_tool = params.get("grant_tool", "user_grant")
    revoke_tool = params.get("revoke_tool", "user_revoke")
    granted_result = params.get("granted_result", "granted")
    match_key = params.get("match_key", "target")
    scope_key = params.get("scope_key", "scope")

    consented: set = set()       # per-action grants, keyed by exact target
    active_scopes: set = set()   # standing grants, matched by prefix
    for r in sorted(trace, key=lambda x: x.seq):
        if r.tool == consent_tool and r.result == granted_result:
            consented.add(r.args.get(match_key))
        elif r.tool == grant_tool and r.result == granted_result:
            active_scopes.add(r.args.get(scope_key))
        elif r.tool == revoke_tool:
            active_scopes.discard(r.args.get(scope_key))
        elif r.tool in destructive and not is_blocked(r):
            target = r.args.get(match_key)
            covered = target in consented or (
                isinstance(target, str)
                and any(isinstance(s, str) and target.startswith(s) for s in active_scopes)
            )
            if not covered:
                return Verdict.violation(
                    r.seq, f"{r.tool}({match_key}={target!r}) without user consent"
                )
    return Verdict.satisfied()


SPEC = register(
    PredicateSpec(SPEC_ID, VERSION, evaluate, number="AAP-1",
                  doc="a destructive action requires prior user consent (per-action or an active scoped grant)")
)
