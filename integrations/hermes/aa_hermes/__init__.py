"""Record Hermes invocations and native authorization through the pinned patch.

Native prompts decide execution. Shell approvals cover an invocation, never
inferred filesystem effects. See ../README.md for tested paths and limits.
"""
from __future__ import annotations
import copy
import json
import logging
import os
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from aa_sdk import Accountability

_log = logging.getLogger("aa_hermes")
SCOPED_TOOLS = ["write_file", "read_file", "patch"]
AUTHORIZATION_TOOLS = ["write_file", "patch", "terminal", "execute_code"]

@dataclass
class _Binding:
    native_id: str
    acc: object
    session: object
    inflight: int = 0
    closing: bool = False
    lock: object = field(default_factory=threading.RLock)

_sessions: dict[str, _Binding] = {}
_sessions_lock = threading.RLock()
_active = ContextVar("aa_hermes_sessions", default=())
_invocation = ContextVar("aa_hermes_invocation", default=None)
_UNSET_PARTY = object()

def _lookup_binding(native_id=None):
    """Prune context-local references finalized successfully in another context."""
    with _sessions_lock:
        stack = tuple(item for item in _active.get()
                      if _sessions.get(item.native_id) is item)
        _active.set(stack)
        # Pending or failed finalization stays in _sessions and remains retryable.
        return _sessions.get(native_id) if native_id else (stack[-1] if stack else None)

def scope_params() -> dict:
    return {"scoped_tools": SCOPED_TOOLS,
            "allow_prefixes": [os.environ.get("AA_SCOPE_PREFIX", "workspace/")],
            "match_key": "path"}

def begin_session(party=_UNSET_PARTY, store=None, chain=None, provider_addr=None,
                  bond_wei=0, payout_wei=10**16, *, native_session_id=None,
                  authorization_tools=None):
    """Open an isolated session; explicit configuration owns chain and party IDs."""
    native_id = native_session_id or f"manual-{uuid.uuid4().hex}"
    with _sessions_lock:
        binding = _lookup_binding(native_id)
        if binding is not None and party is not _UNSET_PARTY and party != binding.session.party:
            raise ValueError("native session is already bound to a different party")
        if binding is None:
            acc = Accountability("hermes-provider", store=store, chain=chain,
                                 provider_addr=provider_addr)
            if chain is not None and bond_wei:
                acc.post_bond(bond_wei)
            acc.register_promise("action_within_declared_scope", scope_params(), payout_wei=payout_wei)
            acc.register_promise("no_destructive_without_consent", {
                "destructive_tools": list(AUTHORIZATION_TOOLS if authorization_tools is None else authorization_tools),
                "authorization_mode": "invocation",
            }, payout_wei=payout_wei)
            binding = _Binding(native_id, acc, acc.session(party="hermes-user" if party is _UNSET_PARTY else party))
            _sessions[native_id] = binding
        if all(item is not binding for item in _active.get()):
            _active.set((*_active.get(), binding))
    return binding.acc, binding.session

def _finish(binding):
    summary = binding.session.end()
    with _sessions_lock:
        _sessions.pop(binding.native_id, None)
    # Keep the default binding reachable until finalization actually succeeds.
    _active.set(tuple(item for item in _active.get() if item is not binding))
    return summary

def end_session(*, native_session_id=None):
    binding = _lookup_binding(native_session_id)
    if binding is None:
        return None
    with binding.lock:
        binding.closing = True
        if not binding.inflight:
            return _finish(binding)
    return None

def current(*, native_session_id=None):
    binding = _lookup_binding(native_session_id)
    return (binding.acc, binding.session) if binding else (None, None)

def _matches(invocation, event):
    """Only correlate observations made within this exact execution context."""
    native_call = event.get("tool_call_id")
    if native_call and invocation["native_call"] and native_call != invocation["native_call"]:
        return False
    if event.get("tool_name"):
        return event["tool_name"] == invocation["tool"] and event.get("arguments") == invocation["args"]
    command = event.get("command")
    if invocation["tool"] == "terminal":
        return command == invocation["args"].get("command")
    if invocation["tool"] == "execute_code":
        return command == f"execute_code <<'PY'\n{invocation['args'].get('code', '')}\nPY"
    return False

def on_approval_request(**event):
    invocation = _invocation.get()
    if invocation is None or not _matches(invocation, event):
        return
    request_id = event.get("request_id")
    if not request_id:
        return
    invocation["binding"].session.record(
        "native_authorization_request", copy.deepcopy(event), "requested",
        event_id=f"request:{request_id}",
        metadata={"action_id": invocation["action_id"], "native_tool_call_id": invocation["native_call"]},
    )

def on_approval_response(**event):
    invocation = _invocation.get()
    if invocation is None or not _matches(invocation, event):
        return
    request_id = event.get("request_id")
    if not request_id or request_id in invocation["observed"]:
        return
    native = str(event.get("native_decision", event.get("choice", "deny")))
    choice = str(event.get("choice", "deny"))
    decision = "timeout" if native == "timeout" else ("allow" if choice in {"once", "session", "always"} else "deny")
    scope = event.get("scope") or {"session": "session", "always": "persistent"}.get(choice, "once")
    invocation["binding"].session.record(
        "native_authorization_response", copy.deepcopy(event), choice,
        action_id=invocation["action_id"], event_id=f"native-response:{request_id}")
    if choice == "escalate":
        invocation["observed"].add(request_id)
        return
    invocation["binding"].session.record_authorization(
        event_id=f"authorization:{request_id}", action_id=invocation["action_id"],
        request_id=event.get("native_request_id") or request_id,
        tool=invocation["tool"], args=invocation["args"],
        decision=decision, scope=scope, authority=event.get("authority", "human"),
        principal=event.get("principal"),
        native_session_id=event.get("native_session_id") or event.get("session_key") or invocation["binding"].native_id,
        native_decision=native, policy_id=event.get("policy_id"),
    )
    invocation["observed"].add(request_id)

def on_tool_execution_middleware(**kwargs):
    next_call, tool = kwargs.get("next_call"), kwargs.get("tool_name")
    args = kwargs.get("args") or {}
    if not callable(next_call):
        return args
    native_id = kwargs.get("session_id")
    binding = _lookup_binding(native_id)
    if binding is None:
        _log.warning("No accountability session for native session %r; invocation is not recorded", native_id)
        return next_call(args)
    with binding.lock:
        if binding.closing:
            raise RuntimeError("accountability session is closing")
        binding.inflight += 1
    action_id = uuid.uuid4().hex
    native_call = kwargs.get("tool_call_id") or ""
    token = _invocation.set({"binding": binding, "action_id": action_id,
        "native_call": native_call, "tool": tool, "args": copy.deepcopy(args), "observed": set()})
    native_result = None
    def execute():
        nonlocal native_result
        native_result = next_call(args)
        try:
            parsed = json.loads(native_result) if isinstance(native_result, str) else native_result
        except (ValueError, TypeError):
            parsed = native_result
        if isinstance(parsed, dict) and (parsed.get("executed") is False or parsed.get("status") in {"blocked", "pending_approval"}):
            return {"blocked": parsed.get("error") or parsed.get("status") or "native approval", "native_result": native_result}
        return parsed
    try:
        binding.session.guard(tool, copy.deepcopy(args), execute,
            action_id=action_id, event_id=f"action:{action_id}", metadata={
                "native_tool_call_id": native_call, "native_session_id": binding.native_id,
                "turn_id": kwargs.get("turn_id") or "",
            })
        return native_result
    finally:
        _invocation.reset(token)
        with binding.lock:
            binding.inflight -= 1
            if binding.closing and not binding.inflight:
                _finish(binding)

def on_turn_end(**event):
    _, session = current(native_session_id=event.get("session_id"))
    if session is not None:
        session.checkpoint(force=False)

def on_session_reset(**event):
    old_id = event.get("old_session_id")
    if old_id:
        end_session(native_session_id=old_id)
    new_id = event.get("new_session_id") or event.get("session_id")
    if new_id:
        begin_session(native_session_id=new_id)

def register(ctx):
    ctx.register_middleware("tool_dispatch", on_tool_execution_middleware)
    ctx.register_hook("pre_approval_request", on_approval_request)
    ctx.register_hook("post_approval_response", on_approval_response)
    ctx.register_hook("approval_policy_applied", on_approval_response)
    ctx.register_hook("on_session_start", lambda **k: begin_session(native_session_id=k.get("session_id")))
    ctx.register_hook("on_session_end", on_turn_end)
    ctx.register_hook("on_session_finalize", lambda **k: end_session(native_session_id=k.get("session_id")))
    ctx.register_hook("on_session_reset", on_session_reset)
