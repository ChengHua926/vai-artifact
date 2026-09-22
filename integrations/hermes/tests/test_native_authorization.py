"""Pinned Hermes middleware/approval integration; no model or blockchain.

The ACP transport and user answers are controlled; patched native dispatch,
approval policy, plugin hooks, SDK records and predicates run directly.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

REVISION = Path(__file__).resolve().parents[3]
HERMES = REVISION.parent / "hermes-agent"
for path in [HERMES, REVISION / "integrations/hermes", *(REVISION / "packages" / p for p in ("commons", "sdk", "store", "verifier"))]:
    sys.path.insert(0, str(path))


@pytest.fixture
def native(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    import model_tools
    import tools.approval as approval
    import aa_hermes
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    old_hooks, old_middleware = manager._hooks, manager._middleware
    manager._hooks, manager._middleware = {}, {}
    class Context:
        def register_hook(self, name, callback):
            manager._hooks.setdefault(name, []).append(callback)
        def register_middleware(self, name, callback):
            manager._middleware.setdefault(name, []).append(callback)
    aa_hermes.register(Context())
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "_permanent_approved", set())
    monkeypatch.setattr(approval, "_session_approved", {})
    monkeypatch.setattr(approval, "_session_approval_origins", {})
    # Optional external command scanner is outside the approval bridge.
    import tools.tirith_security
    monkeypatch.setattr(tools.tirith_security, "check_command_security", lambda _: {"action": "allow", "findings": [], "summary": ""})
    with tempfile.TemporaryDirectory(dir=REVISION / "integrations/hermes") as directory:
        yield SimpleNamespace(plugin=aa_hermes, approval=approval, dispatch=model_tools.handle_function_call, directory=Path(directory), manager=manager)
    manager._hooks, manager._middleware = old_hooks, old_middleware


@pytest.fixture
def acp_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


def authorizations(session):
    return [r for r in session.records if r.tool == "user_authorization"]


def test_acp_allow_once_does_not_authorize_identical_next_invocation(native, acp_loop):
    from acp.schema import AllowedOutcome
    from acp_adapter.edit_approval import make_acp_edit_approval_requester, set_edit_approval_requester, reset_edit_approval_requester
    answers = iter(["allow_once", "deny"])
    async def request(**kwargs):
        return SimpleNamespace(outcome=AllowedOutcome(outcome="selected", option_id=next(answers)))
    token = set_edit_approval_requester(make_acp_edit_approval_requester(request, acp_loop, "acp-one"))
    acc, session = native.plugin.begin_session(native_session_id="acp-one")
    target = native.directory / "file.txt"
    args = {"path": str(target), "content": "approved content"}
    try:
        native.dispatch("write_file", args, session_id="acp-one", tool_call_id="call-1")
        assert target.read_text() == "approved content"
        target.write_text("sentinel")
        denied = json.loads(native.dispatch("write_file", args, session_id="acp-one", tool_call_id="call-2"))
        assert "error" in denied
        assert target.read_text() == "sentinel"
        events = authorizations(session)
        assert [e.args["decision"] for e in events] == ["allow", "deny"]
        assert [e.args["native_decision"] for e in events] == ["allow_once", "deny"]
        actions = [r for r in session.records if r.tool == "write_file"]
        assert len(actions) == 2
        assert events[0].args["action_id"] == actions[0].metadata["action_id"]
        assert events[1].args["action_id"] == actions[1].metadata["action_id"]
        assert events[0].args["action_id"] != events[1].args["action_id"]
        assert events[0].seq < actions[0].seq < events[1].seq < actions[1].seq
        promise = acc.promises[1].promise_id
        assert not acc.self_check(session.records)[promise].violated
        assert acc.self_check([r for r in session.records if r is not events[0]])[promise].violated
        # A later completed call with identical arguments has no native grant.
        # The earlier allow_once cannot authorize it.
        reset_edit_approval_requester(token)
        token = None
        native.dispatch("write_file", args, session_id="acp-one", tool_call_id="call-3")
        assert target.read_text() == "approved content"
        assert acc.self_check(session.records)[promise].violated
    finally:
        if token is not None:
            reset_edit_approval_requester(token)
        native.plugin.end_session(native_session_id="acp-one")


def test_policy_reuse_emits_observed_policy_event(native):
    seen = []
    native.manager._hooks.setdefault("approval_policy_applied", []).append(lambda **kw: seen.append(kw))
    token = native.approval.set_current_session_key("native-policy")
    try:
        command = "rm -rf /tmp/accountability-empty-test-path"
        first = native.approval.check_all_command_guards(command, "local", approval_callback=lambda *a, **k: "session")
        second = native.approval.check_all_command_guards(command, "local", approval_callback=lambda *a, **k: pytest.fail("native policy should be reused"))
        assert first["approved"] and second["approved"]
        assert len(seen) == 1
        assert seen[0]["authority"] == "policy"
        assert seen[0]["policy_id"]
        assert seen[0]["scope"] == "session"
    finally:
        native.approval.reset_current_session_key(token)


def test_acp_timeout_keeps_native_timeout_without_allowing(native, acp_loop):
    from acp_adapter.permissions import make_approval_callback
    async def request(**kwargs):
        await asyncio.sleep(60)
    observed = []
    native.manager._hooks.setdefault("post_approval_response", []).append(lambda **kw: observed.append(kw))
    result = native.approval.check_all_command_guards("rm -rf /tmp/accountability-empty-test-path", "local", approval_callback=make_approval_callback(request, acp_loop, "acp-timeout", timeout=0.01))
    assert result["approved"] is False
    assert observed[-1]["native_decision"] == "timeout"
    assert observed[-1]["surface"] == "acp"


def test_native_turn_end_keeps_session_until_finalize(native):
    from hermes_cli.plugins import invoke_hook
    invoke_hook("on_session_start", session_id="continued")
    _, session = native.plugin.current(native_session_id="continued")
    invoke_hook("on_session_end", session_id="continued", turn_id="turn-one")
    assert native.plugin.current(native_session_id="continued")[1] is session
    path = native.directory / "later.txt"
    native.dispatch("write_file", {"path": str(path), "content": "later"}, session_id="continued", tool_call_id="later")
    assert any(r.tool == "write_file" for r in session.records)
    invoke_hook("on_session_finalize", session_id="continued")
    assert native.plugin.current(native_session_id="continued") == (None, None)


@pytest.mark.parametrize("choice,decision,scope", [("once", "allow", "once"), ("session", "allow", "session"), ("always", "allow", "persistent"), ("deny", "deny", "once")])
def test_actual_terminal_guard_records_native_choice_and_completion(native, choice, decision, scope):
    import tools.terminal_tool as terminal
    previous = terminal._get_approval_callback()
    terminal.set_approval_callback(lambda *a, **k: choice)
    target = native.directory / "disposable.txt"
    target.write_text("disposable test content")
    command = "rm -rf " + shlex.quote(str(target))
    session_key = "terminal-" + choice
    token = native.approval.set_current_session_key(session_key)
    acc, session = native.plugin.begin_session(native_session_id=session_key)
    try:
        result = native.dispatch("terminal", {"command": command, "workdir": str(native.directory)}, task_id=session_key, session_id=session_key, tool_call_id="terminal-call")
        events = authorizations(session)
        assert len(events) == 1, result
        event = events[0]
        assert (event.args["decision"], event.args["scope"], event.args["native_decision"]) == (decision, scope, choice)
        actions = [r for r in session.records if r.tool == "terminal"]
        assert len(actions) == 1
        assert event.args["action_id"] == actions[0].metadata["action_id"]
        assert event.seq < actions[0].seq
        assert target.exists() == (decision == "deny"), result
        assert not any(r.tool in {"delete_file", "user_consent"} for r in session.records)
        assert not acc.self_check(session.records)[acc.promises[1].promise_id].violated
        if choice in {"session", "always"}:
            target.write_text("second invocation fixture")
            terminal.set_approval_callback(lambda *a, **k: pytest.fail("native policy should be reused"))
            native.dispatch("terminal", {"command": command, "workdir": str(native.directory)}, task_id=session_key, session_id=session_key, tool_call_id="terminal-second")
            reused = authorizations(session)[1]
            assert reused.args["authority"] == "policy"
            assert reused.args["scope"] == scope
            assert reused.args["policy_id"]
            assert reused.args["action_id"] != event.args["action_id"]
            assert not target.exists()
            assert not acc.self_check(session.records)[acc.promises[1].promise_id].violated
    finally:
        terminal.set_approval_callback(previous)
        native.approval.reset_current_session_key(token)
        native.plugin.end_session(native_session_id=session_key)


def test_parallel_acp_sessions_do_not_cross_authorizations(native, acp_loop):
    from acp.schema import AllowedOutcome
    from acp_adapter.edit_approval import make_acp_edit_approval_requester, set_edit_approval_requester, reset_edit_approval_requester
    barrier = threading.Barrier(2)
    async def request(**kwargs):
        decision = "allow_once" if kwargs["session_id"] == "allowed" else "deny"
        return SimpleNamespace(outcome=AllowedOutcome(outcome="selected", option_id=decision))
    def run(sid):
        _, session = native.plugin.begin_session(native_session_id=sid)
        token = set_edit_approval_requester(make_acp_edit_approval_requester(request, acp_loop, sid))
        target = native.directory / (sid + ".txt")
        try:
            barrier.wait(timeout=3)
            native.dispatch("write_file", {"path": str(target), "content": sid}, session_id=sid, tool_call_id="same-native-id")
            return session.records, target.exists()
        finally:
            reset_edit_approval_requester(token)
            native.plugin.end_session(native_session_id=sid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["allowed", "denied"]))
    for (records, exists), sid, decision in zip(results, ["allowed", "denied"], ["allow", "deny"]):
        event = next(r for r in records if r.tool == "user_authorization")
        action = next(r for r in records if r.tool == "write_file")
        assert event.args["native_session_id"] == sid
        assert event.args["decision"] == decision
        assert action.args["content"] == sid
        assert exists == (sid == "allowed")


def test_effective_arguments_after_other_middleware_are_bound(native, acp_loop):
    from acp.schema import AllowedOutcome
    from acp_adapter.edit_approval import make_acp_edit_approval_requester, set_edit_approval_requester, reset_edit_approval_requester
    from aa_commons.ids import hash_obj
    original = {"path": str(native.directory / "original.txt"), "content": "original"}
    effective = {"path": str(native.directory / "effective.txt"), "content": "rewritten"}
    native.manager._middleware.setdefault("tool_execution", []).append(lambda **kw: kw["next_call"](effective))
    async def request(**kwargs):
        return SimpleNamespace(outcome=AllowedOutcome(outcome="selected", option_id="allow_once"))
    token = set_edit_approval_requester(make_acp_edit_approval_requester(request, acp_loop, "rewrite"))
    acc, session = native.plugin.begin_session(native_session_id="rewrite")
    try:
        native.dispatch("write_file", original, session_id="rewrite", tool_call_id="rewritten")
        action = next(r for r in session.records if r.tool == "write_file")
        authorization = authorizations(session)[0]
        assert action.args == effective
        assert authorization.args["args_hash"] == hash_obj(effective)
        assert not Path(original["path"]).exists()
        assert Path(effective["path"]).read_text() == "rewritten"
        assert not acc.self_check(session.records)[acc.promises[1].promise_id].violated
    finally:
        reset_edit_approval_requester(token)
        native.plugin.end_session(native_session_id="rewrite")


@pytest.mark.parametrize("verdict", ["approve", "deny"])
def test_smart_terminal_decision_is_never_human_consent(native, monkeypatch, verdict):
    monkeypatch.setattr(native.approval, "_get_approval_mode", lambda: "smart")
    monkeypatch.setattr(native.approval, "_smart_approve", lambda *args: verdict)
    target = native.directory / "smart.txt"
    target.write_text("fixture")
    sid = "smart-" + verdict
    token = native.approval.set_current_session_key(sid)
    acc, session = native.plugin.begin_session(native_session_id=sid)
    try:
        args = {"command": "rm -rf " + shlex.quote(str(target)), "workdir": str(native.directory)}
        native.dispatch("terminal", args, task_id=sid, session_id=sid, tool_call_id="smart-first")
        first = authorizations(session)[0]
        assert first.args["authority"] == "auto_review"
        assert first.args["native_decision"] == verdict
        assert target.exists() == (verdict == "deny")
        assert acc.self_check(session.records)[acc.promises[1].promise_id].violated == (verdict == "approve")
        if verdict == "approve":
            target.write_text("second fixture")
            monkeypatch.setattr(native.approval, "_smart_approve", lambda *args: pytest.fail("stored policy should be reused"))
            native.dispatch("terminal", args, task_id=sid, session_id=sid, tool_call_id="smart-second")
            second = authorizations(session)[1]
            assert second.args["authority"] == "auto_review"
            assert second.args["action_id"] != first.args["action_id"]
            assert second.args["policy_id"]
    finally:
        native.approval.reset_current_session_key(token)
        native.plugin.end_session(native_session_id=sid)


def test_actual_acp_edit_timeout_records_nonexecution(native, acp_loop):
    from acp_adapter.edit_approval import make_acp_edit_approval_requester, set_edit_approval_requester, reset_edit_approval_requester
    async def request(**kwargs):
        await asyncio.sleep(60)
    token = set_edit_approval_requester(make_acp_edit_approval_requester(request, acp_loop, "edit-timeout", timeout=0.01))
    acc, session = native.plugin.begin_session(native_session_id="edit-timeout")
    target = native.directory / "timeout.txt"
    try:
        result = json.loads(native.dispatch("write_file", {"path": str(target), "content": "unapproved"}, session_id="edit-timeout", tool_call_id="timeout"))
        assert result["executed"] is False
        assert not target.exists()
        event = authorizations(session)[0]
        action = next(r for r in session.records if r.tool == "write_file")
        assert event.args["decision"] == "timeout"
        assert event.args["native_decision"] == "timeout"
        assert event.args["action_id"] == action.metadata["action_id"]
        assert "blocked" in action.result
        assert not acc.self_check(session.records)[acc.promises[1].promise_id].violated
    finally:
        reset_edit_approval_requester(token)
        native.plugin.end_session(native_session_id="edit-timeout")


def test_nested_sessions_restore_context_and_finalize_waits_for_action(native):
    from hermes_cli.plugins import invoke_hook
    _, outer = native.plugin.begin_session(native_session_id="outer")
    _, inner = native.plugin.begin_session(native_session_id="inner")
    try:
        native.dispatch("write_file", {"path": str(native.directory / "inner.txt"), "content": "inner"})
        native.plugin.end_session(native_session_id="inner")
        assert native.plugin.current()[1] is outer
        def finalize_inside_dispatch(**kw):
            invoke_hook("on_session_finalize", session_id="outer")
            assert native.plugin.current(native_session_id="outer")[1] is outer
            return kw["next_call"](kw["args"])
        native.manager._middleware["tool_dispatch"].append(finalize_inside_dispatch)
        native.dispatch("write_file", {"path": str(native.directory / "outer.txt"), "content": "outer"})
        assert [r.args["content"] for r in inner.records if r.tool == "write_file"] == ["inner"]
        assert [r.args["content"] for r in outer.records if r.tool == "write_file"] == ["outer"]
        assert native.plugin.current(native_session_id="outer") == (None, None)
    finally:
        native.plugin.end_session(native_session_id="inner")
        native.plugin.end_session(native_session_id="outer")


def test_final_dispatch_wrappers_cannot_rewrite_arguments(native):
    acc, session = native.plugin.begin_session(native_session_id="immutable-dispatch")
    requested = native.directory / "requested.txt"
    replacement = native.directory / "replacement.txt"
    def rewrite(**kw):
        return kw["next_call"]({"path": str(replacement), "content": "replacement"})
    native.manager._middleware["tool_dispatch"].append(rewrite)
    try:
        result = json.loads(native.dispatch("write_file", {"path": str(requested), "content": "requested"}, session_id="immutable-dispatch", tool_call_id="no-rewrite"))
        assert "cannot rewrite arguments" in result["error"]
        assert result["executed"] is False
        assert not requested.exists()
        assert not replacement.exists()
        action = next(r for r in session.records if r.tool == "write_file")
        assert "blocked" in action.result
        assert not acc.self_check(session.records)[acc.promises[1].promise_id].violated
    finally:
        native.plugin.end_session(native_session_id="immutable-dispatch")


def test_explicit_party_mismatch_rejected_but_native_lifecycle_can_reuse(native):
    from hermes_cli.plugins import invoke_hook
    _, session = native.plugin.begin_session(party="alice", native_session_id="party-binding")
    try:
        invoke_hook("on_session_start", session_id="party-binding")
        assert native.plugin.current(native_session_id="party-binding")[1] is session
        assert native.plugin.begin_session(native_session_id="party-binding")[1] is session
        with pytest.raises(ValueError, match="party"):
            native.plugin.begin_session(party="bob", native_session_id="party-binding")
        assert session.party == "alice"
    finally:
        native.plugin.end_session(native_session_id="party-binding")


def test_failed_finalization_retains_default_binding_for_retry(native, monkeypatch):
    _, session = native.plugin.begin_session()
    finish = session.acc._finalize
    attempts = 0
    def temporary_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("controlled finalization failure")
        return finish(*args, **kwargs)
    monkeypatch.setattr(session.acc, "_finalize", temporary_failure)
    try:
        with pytest.raises(ConnectionError, match="finalization"):
            native.plugin.end_session()
        assert native.plugin.current()[1] is session
        assert native.plugin.end_session() is not None
        assert attempts == 2
        assert native.plugin.current() == (None, None)
    finally:
        monkeypatch.setattr(session.acc, "_finalize", finish)
        for native_id, binding in list(native.plugin._sessions.items()):
            if binding.session is session:
                native.plugin.end_session(native_session_id=native_id)


def test_effectful_dispatch_exception_is_not_nonexecution(native, monkeypatch):
    import model_tools
    dispatch = model_tools.registry.dispatch
    def effect_then_error(*args, **kwargs):
        dispatch(*args, **kwargs)
        raise ValueError("controlled error after actual write")
    monkeypatch.setattr(model_tools.registry, "dispatch", effect_then_error)
    acc, session = native.plugin.begin_session(native_session_id="effectful-error")
    target = native.directory / "effectful-error.txt"
    try:
        result = json.loads(native.dispatch("write_file", {"path": str(target), "content": "actual effect"}, session_id="effectful-error", tool_call_id="effectful-error"))
        assert "controlled error" in result["error"]
        assert target.read_text() == "actual effect"
        action = next(r for r in session.records if r.tool == "write_file")
        assert "blocked" not in action.result
        assert "controlled error after actual write" in action.result["error"]["message"]
        assert acc.self_check(session.records)[acc.promises[1].promise_id].violated
    finally:
        native.plugin.end_session(native_session_id="effectful-error")


@pytest.mark.parametrize("nested", [False, True])
def test_cross_thread_finalize_prunes_callers_default_binding(native, nested):
    outer = native.plugin.begin_session(native_session_id="caller-outer")[1] if nested else None
    _, session = native.plugin.begin_session(native_session_id="worker-finalized")
    entered, release = threading.Event(), threading.Event()
    def pause_worker(**kw):
        if kw.get("tool_call_id") == "worker-call":
            entered.set()
            assert release.wait(timeout=5)
        return kw["next_call"](kw["args"])
    native.manager._middleware["tool_dispatch"].append(pause_worker)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(native.dispatch, "write_file",
                {"path": str(native.directory / "worker.txt"), "content": "worker"},
                session_id="worker-finalized", tool_call_id="worker-call")
            assert entered.wait(timeout=3)
            assert native.plugin.end_session() is None
            assert native.plugin.current()[1] is session  # pending closure is still reachable
            release.set()
            future.result(timeout=3)
        if nested:
            # Exercise middleware lookup before current() has pruned the caller's stack.
            native.dispatch("write_file", {"path": str(native.directory / "outer-after-worker.txt"), "content": "outer"})
            assert native.plugin.current()[1] is outer
            assert [r.args["content"] for r in outer.records if r.tool == "write_file"] == ["outer"]
        else:
            assert native.plugin.current() == (None, None)
        assert [r.args["content"] for r in session.records if r.tool == "write_file"] == ["worker"]
    finally:
        release.set()
        native.plugin.end_session(native_session_id="worker-finalized")
        native.plugin.end_session(native_session_id="caller-outer")
