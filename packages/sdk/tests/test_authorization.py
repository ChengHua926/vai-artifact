"""Invocation consent must survive native callback ordering without widening authority."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from aa_commons import ActionRecord, hash_obj, registry, trace_hash
from aa_sdk import Accountability


def session():
    acc = Accountability("provider")
    acc.register_promise("no_destructive_without_consent", {
        "destructive_tools": ["exec"], "authorization_mode": "invocation",
    }, 1)
    return acc, acc.session("party")


def authorize(sess, action="a", event="approval-1", **changes):
    values = dict(event_id=event, action_id=action, request_id="request-1", tool="exec",
                  args={"command": "echo ok"}, decision="allow", scope="once", authority="human")
    values.update(changes)
    sess.record_authorization(**values)


def execute(sess, action="a", event="effect-1", args=None):
    sess.record("exec", args or {"command": "echo ok"}, "done", action_id=action, event_id=event)


def violated(acc, sess):
    return next(iter(acc.self_check(sess.records).values())).violated


def test_legacy_bytes_unchanged_and_metadata_roundtrips():
    old = {"seq": 1, "session_id": "s", "tool": "x", "args": {}, "result": "ok", "ts": 0}
    assert ActionRecord.from_dict(old).to_dict() == old
    extended = {**old, "metadata": {"action_id": "a", "event_id": "e"}}
    restored = ActionRecord.from_dict(extended)
    assert restored.to_dict() == extended
    assert trace_hash([restored]) != trace_hash([ActionRecord.from_dict(old)])


def test_native_approval_inside_executor_precedes_action_once():
    acc, sess = session()
    def executor():
        authorize(sess)
        return "ran"
    assert sess.guard("exec", {"command": "echo ok"}, executor, action_id="a", event_id="effect-1") == "ran"
    assert [r.seq for r in sess.records] == [1, 2]
    assert [r.tool for r in sess.records] == ["user_authorization", "exec"]
    assert not violated(acc, sess)


@pytest.mark.parametrize("approved", [False, True])
def test_partial_effect_then_exception_is_recorded_without_claiming_nonexecution(tmp_path, approved):
    acc, sess = session()
    effect = tmp_path / "effect.txt"
    failure = RuntimeError("failed after writing")
    def executor():
        if approved:
            authorize(sess)
        effect.write_text("the effect happened")
        raise failure
    with pytest.raises(RuntimeError) as raised:
        sess.guard("exec", {"command": "echo ok"}, executor,
                   action_id="a", event_id="effect-1")
    assert raised.value is failure
    assert effect.read_text() == "the effect happened"
    assert [r.seq for r in sess.records] == list(range(1, len(sess.records) + 1))
    outcome = sess.records[-1]
    assert outcome.tool == "exec" and outcome.metadata["action_id"] == "a"
    assert outcome.result == {"error": {"type": "RuntimeError", "message": "failed after writing"}}
    assert violated(acc, sess) is not approved
    assert sess.end()["n_actions"] == (2 if approved else 1)


@pytest.mark.parametrize("scope", ["once", "session", "persistent"])
def test_native_scope_does_not_authorize_another_invocation(scope):
    acc, sess = session()
    authorize(sess, scope=scope)
    execute(sess)
    assert not violated(acc, sess)
    execute(sess, "b", "effect-2")
    assert violated(acc, sess)


@pytest.mark.parametrize("change", [
    {"decision": "deny"}, {"decision": "timeout"}, {"authority": "auto_review"},
    {"authority": "policy"}, {"tool": "write"}, {"args": {"command": "different"}},
])
def test_denial_or_wrong_subject_or_unbound_policy_is_not_consent(change):
    acc, sess = session()
    authorize(sess, **change)
    execute(sess)
    assert violated(acc, sess)


def test_native_policy_application_is_bound_to_actual_invocation():
    acc, sess = session()
    authorize(sess, authority="policy", policy_id="native-policy", scope="persistent")
    execute(sess)
    assert not violated(acc, sess)


def test_native_authorization_payload_has_explicit_schema_version():
    _, sess = session()
    authorize(sess)
    assert type(sess.records[0].args["schema_version"]) is int
    assert sess.records[0].args["schema_version"] == 1


@pytest.mark.parametrize("version", [None, 0, 2, "1", 1.0, True])
def test_missing_or_unsupported_authorization_schema_cannot_grant(version):
    acc, sess = session()
    authorize(sess)
    execute(sess)
    auth = sess.records[0].to_dict()
    if version is None:
        auth["args"].pop("schema_version", None)
    else:
        auth["args"]["schema_version"] = version
    sess.records[0] = ActionRecord.from_dict(auth)
    assert violated(acc, sess)


def test_duplicate_delivery_does_not_replenish_once_or_duplicate_records():
    acc, sess = session()
    authorize(sess)
    authorize(sess)
    execute(sess)
    execute(sess)
    assert len(sess.records) == 2
    assert not violated(acc, sess)
    authorize(sess, event="approval-2")
    execute(sess, event="effect-2")
    assert violated(acc, sess)  # an invocation cannot be executed twice under recycled approval


def test_conflicting_retransmission_rejected():
    _, sess = session()
    authorize(sess)
    with pytest.raises(ValueError, match="event"):
        authorize(sess, decision="deny")


@pytest.mark.parametrize("identity", [
    {"action_id": "a"}, {"event_id": "effect-1"},
    {"action_id": "a", "event_id": "effect-1"},
    {"metadata": {"action_id": "a", "event_id": "effect-1"}},
])
@pytest.mark.parametrize("second_args", [{"command": "echo ok"}, {"command": "different"}])
def test_guard_duplicate_identity_never_reexecutes_even_with_changed_arguments(identity, second_args):
    _, sess = session()
    effects = []
    sess.guard("exec", {"command": "echo ok"}, lambda: effects.append("first"), **identity)
    with pytest.raises(ValueError, match="identity"):
        sess.guard("exec", second_args, lambda: effects.append("second"), **identity)
    assert effects == ["first"] and len(sess.records) == 1


def test_guard_reserves_identity_before_concurrent_executor_finishes():
    _, sess = session()
    entered, release = threading.Event(), threading.Event()
    effects = []
    def executor():
        effects.append("ran")
        entered.set()
        assert release.wait(1)
        return "done"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(sess.guard, "exec", {}, executor, action_id="a", event_id="e")
        assert entered.wait(1)
        try:
            with pytest.raises(ValueError, match="identity"):
                sess.guard("exec", {}, lambda: effects.append("duplicated"), action_id="a", event_id="e")
        finally:
            release.set()
        assert first.result() == "done"
    assert effects == ["ran"] and len(sess.records) == 1


def test_concurrent_records_have_unique_contiguous_sequence():
    _, sess = session()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: sess.record("read", {"i": i}, "ok", event_id=f"e{i}"), range(40)))
    assert sorted(r.seq for r in sess.records) == list(range(1, 41))


def test_wrong_session_authorization_cannot_be_replayed():
    acc, sess = session()
    authorize(sess)
    execute(sess)
    auth = sess.records[0].to_dict()
    auth["session_id"] = "another-session"
    sess.records[0] = ActionRecord.from_dict(auth)
    assert violated(acc, sess)


def test_old_committed_aap1_remains_resolvable():
    # The v2 source hash recorded before this change; retaining its evaluator is a protocol duty.
    old_hash = "0x7eef1be18310c4cec15126880d17d7156f4ba6a4f4a82343e575ff528fd83be6"
    spec = registry.resolve_hash(old_hash)
    assert spec is not None and spec.version == 2
    assert spec.spec_id == "no_destructive_without_consent"
    assert registry.get(spec.spec_id).version == 3
