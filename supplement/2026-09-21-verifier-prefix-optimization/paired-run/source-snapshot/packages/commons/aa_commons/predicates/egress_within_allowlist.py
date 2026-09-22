"""C — egress within allowlist (data leaves only to approved recipients / sinks).

The exfiltration case, made checkable from the action event alone. An *egress* tool (send,
post, upload, message) must address a recipient on the allowlist. This is **content-blind**:
it constrains *where* data goes, not *what* is sent — the deterministic, taint-free slice of
exfiltration. It does NOT read or label the payload.

LIMITATION (write it down): content-blindness cuts both ways, so this is a *lower bound* on
exfiltration coverage, not a solution to it.
  - False negative: secret data sent to an *allow-listed* recipient passes (a real contact who
    is also the attacker's drop). Catching that needs value/taint labeling of the payload — a
    separate, deferred predicate, not this one.
  - False positive: a legitimate *new* recipient not yet on the allowlist fires. Widen it either at
    runtime, a user-sourced ``recipient_grant`` event adds the recipient for the rest of the session
    (the consent pattern, generalized), or permanently, a params change (new paramsHash).
Like AAP-2, this is a lexical check on the declared recipient string, not a semantic check on
the effect. Checkable from the action event alone — no taint tracking.

params:
  egress_tools:        list[str]              tools that send data outward
  recipient_allowlist: list[str]              recipients / sinks allowed to receive
  allow_prefixes:      list[str] = []         a recipient also passes if it startswith one of
                                              these (domain / URL sinks, e.g. "https://api.acme.com/")
  recipient_key:       str = "target"         the arg holding the recipient / destination
                                              (may be a single recipient OR a list, e.g. email To:[...])
  grant_tool:          str = "recipient_grant"  a user-sourced event {recipient_key: rcpt} with result
                                              "granted" adds rcpt to the allowlist from that seq onward
  revoke_tool:         str = "recipient_revoke" a user-sourced event {recipient_key: rcpt} removes rcpt
  granted_result:      str = "granted"        the result value that marks a grant as approved

Grants and revokes are the consent pattern generalized: they arrive on the SDK's user-side path
(Session.grant / .revoke), never through the agent's guard, so a compromised agent cannot widen its
own allowlist. With no such events the allowlist is exactly the declared params, unchanged.
"""
from __future__ import annotations

from ..predicate import PredicateSpec, Verdict
from ..registry import register
from ..trace import ActionRecord, is_blocked

SPEC_ID = "egress_within_allowlist"
VERSION = 4  # v2: list-valued recipient_key (any off-allowlist => violation). v3: skip None recipients. v4: runtime recipient_grant/recipient_revoke events widen/narrow the allowlist mid-session (no events => static, backward compatible)


def evaluate(trace: list[ActionRecord], params: dict) -> Verdict:
    egress_tools = set(params["egress_tools"])
    allow = set(params["recipient_allowlist"])
    prefixes = params.get("allow_prefixes", [])
    recipient_key = params.get("recipient_key", "target")
    grant_tool = params.get("grant_tool", "recipient_grant")
    revoke_tool = params.get("revoke_tool", "recipient_revoke")
    granted_result = params.get("granted_result", "granted")

    for r in sorted(trace, key=lambda x: x.seq):
        # runtime authorization events (user-sourced, off the agent's path) widen or narrow the
        # allowlist as the trace advances; an egress is checked against the set as of its own seq.
        if r.tool == grant_tool and r.result == granted_result:
            allow.add(r.args.get(recipient_key))
            continue
        if r.tool == revoke_tool:
            allow.discard(r.args.get(recipient_key))
            continue
        if r.tool in egress_tools and not is_blocked(r):
            to = r.args.get(recipient_key)
            # recipient_key may hold a single recipient or a LIST (e.g. email To:[...]); a
            # multi-recipient send is a violation if ANY recipient is off the allowlist (you
            # cannot launder exfiltration by bundling a bad recipient with good ones).
            recipients = to if isinstance(to, list) else [to]
            for rcpt in recipients:
                if rcpt is None:
                    # the call did not specify this recipient arg (e.g. update_* changing only an
                    # amount, not redirecting) — there is no egress target to check, so skip it.
                    continue
                allowed = rcpt in allow or (
                    isinstance(rcpt, str) and any(rcpt.startswith(p) for p in prefixes)
                )
                if not allowed:
                    return Verdict.violation(
                        r.seq, f"egress {r.tool} to non-allowlisted recipient {rcpt!r}"
                    )
    return Verdict.satisfied()


SPEC = register(
    PredicateSpec(SPEC_ID, VERSION, evaluate, number="AAP-4",
                  doc="outbound/egress actions must target only allow-listed recipients or sinks (content-blind)")
)
