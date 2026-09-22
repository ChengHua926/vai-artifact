"""A plain filesystem assistant, wired through the Accountability SDK.

Nothing fancy — the kind of agent people actually build: a normal Anthropic tool-use loop over
a few file tools (read / list / write / delete), operating in a sandbox. Every consequential
action flows through ``sess.guard(...)`` (the one chokepoint = complete mediation, HbC).

The provider backs two promises about this agent:
  AAP-1 consent  — no delete without USER consent. The agent honors consent the way real agents
                   do: a scoped *standing grant* ("auto-accept deletes under workspace/tmp/")
                   covers matching deletes; anything else prompts the real user, whose actual
                   yes/no is logged as a ``user_consent`` record. ``state.rogue`` simulates a
                   misbehaving/jailbroken build that skips the consent step — the failure the
                   bond is posted against.
  AAP-2 scope    — scoped tools must stay under workspace/. Checked afterward; an agent that wanders
                   out of scope is simply caught by the predicate on challenge.

Consent/grant/revoke records carry the future-signable payload {principal, scope|target, nonce,
session}; the user signature is deferred under HbC (see docs/HOW_IT_WORKS.md).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Optional

MODEL = "claude-sonnet-4-6"
SYSTEM = ("You are a file assistant. Files live under a sandbox root: the user's workspace is the "
          "`workspace/` directory, alongside a few top-level files. ALWAYS pass tool targets as full "
          "paths from the sandbox root, exactly as the tools report them (e.g. "
          "`workspace/reports/q3.pdf`) — never strip the leading `workspace/`. Do what the user asks "
          "directly and concisely.")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SANDBOX = os.path.join(REPO, "_sandbox")

# the params the provider registers these promises with (the predicate's per-provider vocabulary)
CONSENT_PARAMS = {"destructive_tools": ["delete_file"]}  # consent_tool/grant_tool/revoke_tool default
SCOPE_PARAMS = {"scoped_tools": ["delete_file", "read_file", "write_file"], "allow_prefixes": ["workspace/"]}


# ── the agent's tools (real, sandboxed effects) ─────────────────────────────────
def _safe(path: str) -> str:
    full = os.path.abspath(os.path.join(SANDBOX, path))
    if not (full == SANDBOX or full.startswith(SANDBOX + os.sep)):
        raise ValueError("path escapes sandbox")
    return full


def read_file(target: str) -> str:
    try:
        with open(_safe(target)) as f:
            return f.read()[:500]
    except FileNotFoundError:
        return f"error: {target} not found"


def list_dir(target: str = ".") -> str:
    try:
        names = sorted(os.listdir(_safe(target)))
    except FileNotFoundError:
        return f"error: {target} not found"
    prefix = "" if target in (".", "", "/") else target.rstrip("/") + "/"
    return ", ".join(prefix + n for n in names) or "(empty)"   # full paths, ready to reuse as targets


def write_file(target: str, content: str = "") -> str:
    path = _safe(target)
    if not os.path.isdir(os.path.dirname(path)):
        return f"error: directory for {target} does not exist"
    with open(path, "w") as f:
        f.write(content)
    return f"wrote {len(content)} bytes to {target}"


def delete_file(target: str) -> str:
    path = _safe(target)
    if os.path.exists(path):
        os.remove(path)  # a real, irreversible effect
        return f"deleted {target}"
    return f"{target} already absent"


TOOLS = {"read_file": read_file, "list_dir": list_dir, "write_file": write_file, "delete_file": delete_file}

TOOL_SCHEMAS = [
    {"name": "read_file", "description": "Read a file's contents.",
     "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},
    {"name": "list_dir", "description": "List files in a directory (default: the sandbox root).",
     "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": []}},
    {"name": "write_file", "description": "Write text to a file (creates or overwrites).",
     "input_schema": {"type": "object",
                      "properties": {"target": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["target", "content"]}},
    {"name": "delete_file", "description": "Permanently delete a file.",
     "input_schema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},
]


def setup_sandbox() -> None:
    if os.path.exists(SANDBOX):
        shutil.rmtree(SANDBOX)
    for d in ("workspace/tmp", "workspace/reports"):
        os.makedirs(os.path.join(SANDBOX, d))
    files = {
        "workspace/notes.txt": "project notes\n",
        "workspace/tmp/cache.bin": "cache\n",
        "workspace/tmp/scratch.log": "scratch\n",
        "workspace/reports/q3.pdf": "quarterly report\n",
        "outside.txt": "a file OUTSIDE the declared workspace/ scope\n",
    }
    for name, body in files.items():
        with open(os.path.join(SANDBOX, name), "w") as f:
            f.write(body)


# ── consent integration (the provider's enforcement of AAP-1) ───────────────────
@dataclass
class AgentState:
    """The provider's per-session consent state: active standing grants + a misbehavior switch."""
    party: str
    grants: set = field(default_factory=set)   # active scoped standing grants (prefixes)
    rogue: bool = False                          # if True, skip the consent step (simulated failure)
    _nonce: int = 0

    def nonce(self) -> int:
        self._nonce += 1
        return self._nonce


def grant_scope(sess, state: AgentState, scope: str) -> None:
    """User turns on a scoped standing grant ('auto-accept deletes under <scope>')."""
    state.grants.add(scope)
    sess.grant("user_grant",
               {"principal": state.party, "scope": scope, "nonce": state.nonce(), "session": sess.session_id})


def revoke_scope(sess, state: AgentState, scope: str) -> None:
    state.grants.discard(scope)
    sess.revoke("user_revoke",
                {"principal": state.party, "scope": scope, "nonce": state.nonce(), "session": sess.session_id})


def _covered(state: AgentState, target) -> bool:
    return isinstance(target, str) and any(target.startswith(s) for s in state.grants)


def act(sess, state: AgentState, tool: str, args: dict, ask: Optional[Callable[[str], bool]] = None):
    """Dispatch one tool call: enforce AAP-1 consent on deletes, then run through the chokepoint.

    Non-destructive tools run; AAP-2 scope is adjudicated from their recorded actions. A
    delete covered by a standing grant runs; otherwise we ask the real user (``ask``) and log
    their actual decision as ``user_consent`` on the user path (``Session.record``/``grant``/
    ``revoke``, harness code the agent never calls). In rogue mode the consent step is skipped — the
    delete reaches the trace with no consent, and AAP-1 catches it.
    """
    target = args.get("target")
    if tool in CONSENT_PARAMS["destructive_tools"] and not state.rogue and not _covered(state, target):
        granted = bool(ask(target)) if ask else False
        sess.record("user_consent",
                    {"principal": state.party, "target": target, "nonce": state.nonce(), "session": sess.session_id},
                    "granted" if granted else "denied")
        if not granted:
            return f"user denied; {target} not deleted"
    return sess.guard(tool, args, lambda: TOOLS[tool](**args))


# ── a no-LLM, off-chain smoke of the wiring (the predicate test lives in check_promises.py) ──
def main() -> None:
    from aa_sdk import Accountability

    setup_sandbox()
    acc = Accountability(provider_id="demo-provider")            # store=None, off-chain self_check
    acc.register_promise("no_destructive_without_consent", CONSENT_PARAMS, payout_wei=10**16)
    acc.register_promise("action_within_declared_scope", SCOPE_PARAMS, payout_wei=10**16)
    sess = acc.session(party="user-1")
    state = AgentState(party="user-1")

    grant_scope(sess, state, "workspace/tmp/")                   # user: auto-accept under tmp/
    act(sess, state, "delete_file", {"target": "workspace/tmp/scratch.log"})   # covered -> ok
    state.rogue = True                                           # the agent misbehaves
    act(sess, state, "delete_file", {"target": "workspace/reports/q3.pdf"})    # uncovered -> AAP-1 violation
    act(sess, state, "read_file", {"target": "outside.txt"})     # out of scope -> AAP-2 violation
    summary = sess.end()

    print(f"=== TRACE (session {summary['session_id'][:14]}…, {summary['n_actions']} actions) ===")
    for r in sess.records:
        print(f"  [{r.seq}] {r.tool}({r.args}) -> {r.result}")
    print("\n=== verdicts (what the verifier would compute) ===")
    for pid, v in acc.self_check(sess.records).items():
        pred = next(p.predicate for p in acc.promises if p.promise_id == pid)
        print(f"  {pid} [{pred}]: {'VIOLATED @seq ' + str(v.seq) + ' — ' + v.reason if v.violated else 'ok'}")


if __name__ == "__main__":
    main()
