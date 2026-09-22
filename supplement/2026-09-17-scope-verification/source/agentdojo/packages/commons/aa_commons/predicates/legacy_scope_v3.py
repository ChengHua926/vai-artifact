"""AAP-2 (v2) — action within declared scope.

A "scoped" tool's target must fall inside the declared scope. Shown here with path prefixes (a
workspace boundary); the same shape extends to domain or registry allowlists. Checkable from the
action event alone — no taint tracking.

The target is **lexically normalized** (``posixpath.normpath``) before the prefix check, so
``workspace/../etc/x`` (which v1's raw ``startswith`` let through) resolves to ``etc/x`` and is
caught. normpath is deterministic and OS-independent (the predicate is a pure function of the
trace, so SDK and verifier must agree byte-for-byte — never ``os.path``/``realpath``, which touch
the filesystem and differ by platform).

LIMITATION (write it down): this checks **lexical** containment of the declared path string, not
**semantic** containment of the effect. A symlink created in-scope (``workspace/link -> /``) makes
``workspace/link/etc/x`` pass lexically while the effect lands out of scope — using only
faithfully-logged, in-scope actions, so it survives honest-but-curious. A trace-pure, FS-free
predicate cannot resolve symlinks; soundness against that requires a runtime guarantee that the
scope root is a symlink-free jail. See integrations/hermes/FINDINGS.md.

params:
  scoped_tools:   list[str]
  allow_prefixes: list[str]      normalized target must be at/under one of these
  match_key:      str = "target"
  grant_tool:     str = "scope_grant"   a user-sourced event {scope_key: prefix} result "granted" adds
                                        the prefix to the scope from that seq onward
  revoke_tool:    str = "scope_revoke"  a user-sourced event {scope_key: prefix} removes it
  granted_result: str = "granted"       the result value that marks a grant as approved
  scope_key:      str = "prefix"        the arg holding the granted / revoked prefix

Grants and revokes are the consent pattern generalized: they arrive on the SDK's user-side path
(Session.grant / .revoke), never through the agent's guard, so a compromised agent cannot widen its
own scope. With no such events the scope is exactly the declared params, unchanged.
"""
from __future__ import annotations

import posixpath

from ..predicate import PredicateSpec, Verdict
from ..registry import register_historical as register
from ..trace import ActionRecord, is_blocked

SPEC_ID = "action_within_declared_scope"
VERSION = 3  # v2: lexical normpath containment. v3: runtime scope_grant/scope_revoke events widen/narrow the scope mid-session (no events => static, backward compatible)


def evaluate(trace: list[ActionRecord], params: dict) -> Verdict:
    scoped = set(params["scoped_tools"])
    active = [posixpath.normpath(p) for p in params["allow_prefixes"]]
    match_key = params.get("match_key", "target")
    grant_tool = params.get("grant_tool", "scope_grant")
    revoke_tool = params.get("revoke_tool", "scope_revoke")
    granted_result = params.get("granted_result", "granted")
    scope_key = params.get("scope_key", "prefix")

    for r in sorted(trace, key=lambda x: x.seq):
        # runtime scope grants/revokes widen or narrow the declared scope as the trace advances.
        if r.tool == grant_tool and r.result == granted_result:
            p = r.args.get(scope_key)
            if isinstance(p, str):
                active.append(posixpath.normpath(p))
            continue
        if r.tool == revoke_tool:
            p = r.args.get(scope_key)
            if isinstance(p, str):
                np_ = posixpath.normpath(p)
                active = [x for x in active if x != np_]
            continue
        if r.tool in scoped and not is_blocked(r):
            target = r.args.get(match_key)
            np = posixpath.normpath(target) if isinstance(target, str) else None
            in_scope = np is not None and any(np == p or np.startswith(p + "/") for p in active)
            if not in_scope:
                return Verdict.violation(
                    r.seq, f"{r.tool} target {target!r} outside declared scope {params['allow_prefixes']}"
                )
    return Verdict.satisfied()


SPEC = register(
    PredicateSpec(SPEC_ID, VERSION, evaluate, number="AAP-2",
                  doc="scoped actions must stay (lexically) within the declared scope, e.g. under workspace/")
)
