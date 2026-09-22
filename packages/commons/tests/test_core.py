from aa_commons import (
    ActionRecord,
    predicate_hash,
    registry,
    trace_from_json,
    trace_hash,
    trace_to_json,
)


def rec(seq, tool, args, result="ok", session="s1", ts=0):
    return ActionRecord(seq=seq, session_id=session, tool=tool, args=args, result=result, ts=ts)


def test_trace_hash_is_order_and_roundtrip_stable():
    recs = [
        rec(2, "delete_file", {"target": "/tmp/a"}),
        rec(1, "request_approval", {"target": "/tmp/a"}, result="granted"),
    ]
    h1 = trace_hash(recs)
    assert h1 == trace_hash(list(reversed(recs)))  # insertion order irrelevant
    assert h1 == trace_hash(trace_from_json(trace_to_json(recs)))  # serialize/reload stable (I3)


def test_AAP1_consent_per_action_grant_revoke_and_deny():
    A = registry.get("no_destructive_without_consent")
    params = {"destructive_tools": ["delete_file"]}  # defaults: user_consent / user_grant / user_revoke

    # delete with no consent at all -> violation
    v = A.evaluate([rec(1, "delete_file", {"target": "workspace/x"})], params)
    assert v.violated and v.seq == 1

    # per-action consent before the delete -> ok; ordering matters
    assert not A.evaluate([
        rec(1, "user_consent", {"target": "workspace/x"}, result="granted"),
        rec(2, "delete_file", {"target": "workspace/x"}),
    ], params).violated
    late = A.evaluate([
        rec(1, "delete_file", {"target": "workspace/x"}),
        rec(2, "user_consent", {"target": "workspace/x"}, result="granted"),
    ], params)
    assert late.violated and late.seq == 1

    # acting despite a deny -> violation
    assert A.evaluate([
        rec(1, "user_consent", {"target": "workspace/x"}, result="denied"),
        rec(2, "delete_file", {"target": "workspace/x"}),
    ], params).violated

    # scoped standing grant covers anything under the prefix
    assert not A.evaluate([
        rec(1, "user_grant", {"scope": "workspace/tmp/"}, result="granted"),
        rec(2, "delete_file", {"target": "workspace/tmp/cache.bin"}),
    ], params).violated
    # a target OUTSIDE the granted scope is still uncovered -> violation
    assert A.evaluate([
        rec(1, "user_grant", {"scope": "workspace/tmp/"}, result="granted"),
        rec(2, "delete_file", {"target": "workspace/reports/q3.pdf"}),
    ], params).violated
    # revoking the grant before the delete -> uncovered -> violation
    assert A.evaluate([
        rec(1, "user_grant", {"scope": "workspace/tmp/"}, result="granted"),
        rec(2, "user_revoke", {"scope": "workspace/tmp/"}),
        rec(3, "delete_file", {"target": "workspace/tmp/cache.bin"}),
    ], params).violated


def test_B_scope():
    B = registry.get("action_within_declared_scope")
    params = {"scoped_tools": ["delete_file"], "allow_prefixes": ["workspace/"]}
    # in scope
    assert not B.evaluate([rec(1, "delete_file", {"target": "workspace/a.log"})], params).violated
    assert not B.evaluate([rec(1, "delete_file", {"target": "workspace/sub/a.log"})], params).violated
    # plainly out of scope
    out = B.evaluate([rec(1, "delete_file", {"target": "/etc/passwd"})], params)
    assert out.violated and out.seq == 1
    # v2: `..` traversal that v1's raw startswith let through is now caught
    assert B.evaluate([rec(1, "delete_file", {"target": "workspace/../etc/passwd"})], params).violated
    # boundary: a sibling dir sharing the prefix string must NOT match (the trailing "/" is load-bearing)
    assert B.evaluate([rec(1, "delete_file", {"target": "workspace_evil/x"})], params).violated


def test_D_payment_mandate_and_gate_skip():
    D = registry.get("payment_within_mandate")
    params = {"pay_tool": "send_payment", "max_amount": 50, "merchant_allowlist": ["0xGood"]}
    assert not D.evaluate([rec(1, "send_payment", {"target": "0xGood", "amount": 10})], params).violated
    bad_merchant = D.evaluate([rec(1, "send_payment", {"target": "0xBad", "amount": 10})], params)
    assert bad_merchant.violated
    over_budget = D.evaluate([rec(1, "send_payment", {"target": "0xGood", "amount": 999})], params)
    assert over_budget.violated
    # a blocked attempt had no effect -> not a violation
    blocked = D.evaluate([rec(1, "send_payment", {"target": "0xBad", "amount": 10}, result={"blocked": "x"})], params)
    assert not blocked.violated


def test_C_egress_within_allowlist():
    C = registry.get("egress_within_allowlist")
    params = {"egress_tools": ["send_email"], "recipient_allowlist": ["alice@acme.com"]}
    # to an allow-listed recipient -> ok
    assert not C.evaluate([rec(1, "send_email", {"target": "alice@acme.com"})], params).violated
    # to a non-allowlisted recipient -> violation at that seq
    bad = C.evaluate([rec(1, "send_email", {"target": "attacker@evil.com"})], params)
    assert bad.violated and bad.seq == 1
    # prefix allowlist (e.g. a URL sink) passes; a different host does not
    p2 = {"egress_tools": ["http_post"], "recipient_allowlist": [], "allow_prefixes": ["https://api.acme.com/"]}
    assert not C.evaluate([rec(1, "http_post", {"target": "https://api.acme.com/v1/log"})], p2).violated
    assert C.evaluate([rec(1, "http_post", {"target": "https://evil.com/steal"})], p2).violated
    # a blocked egress had no effect -> not a violation
    blocked = C.evaluate([rec(1, "send_email", {"target": "attacker@evil.com"}, result={"blocked": "x"})], params)
    assert not blocked.violated
    # custom recipient_key
    p3 = {"egress_tools": ["upload"], "recipient_allowlist": ["s3://mybucket"], "recipient_key": "dest"}
    assert not C.evaluate([rec(1, "upload", {"dest": "s3://mybucket"})], p3).violated
    assert C.evaluate([rec(1, "upload", {"dest": "s3://attacker"})], p3).violated


def test_E_aggregate_within_cap():
    E = registry.get("aggregate_within_cap")
    # COUNT mode: no more than 2 deletes
    p_count = {"counted_tools": ["delete_email"], "max_count": 2}
    assert not E.evaluate([rec(1, "delete_email", {"id": "a"}), rec(2, "delete_email", {"id": "b"})], p_count).violated
    over = E.evaluate([rec(1, "delete_email", {"id": "a"}), rec(2, "delete_email", {"id": "b"}), rec(3, "delete_email", {"id": "c"})], p_count)
    assert over.violated and over.seq == 3  # the (max+1)th call
    # SUM mode: cumulative amount must stay <= 100 (catches structuring into sub-cap transfers)
    p_sum = {"summed_tools": ["send_money"], "amount_key": "amount", "max_sum": 100}
    assert not E.evaluate([rec(1, "send_money", {"target": "x", "amount": 60}), rec(2, "send_money", {"target": "y", "amount": 40})], p_sum).violated
    structuring = E.evaluate([rec(1, "send_money", {"target": "x", "amount": 40}), rec(2, "send_money", {"target": "y", "amount": 40}), rec(3, "send_money", {"target": "z", "amount": 40})], p_sum)
    assert structuring.violated and structuring.seq == 3  # running sum crosses 100 at the 3rd
    # blocked calls do not count
    blk = E.evaluate([rec(1, "delete_email", {"id": "a"}, result={"blocked": "x"}), rec(2, "delete_email", {"id": "b"}), rec(3, "delete_email", {"id": "c"})], p_count)
    assert not blk.violated  # only 2 non-blocked deletes


def test_C_egress_runtime_grant_revoke():
    C = registry.get("egress_within_allowlist")
    params = {"egress_tools": ["send_email"], "recipient_allowlist": ["alice@acme.com"]}
    # off-allowlist recipient fires with no grant
    assert C.evaluate([rec(1, "send_email", {"target": "bob@x.com"})], params).violated
    # a user grant BEFORE the send authorizes it (the consent pattern, generalized)
    assert not C.evaluate([
        rec(1, "recipient_grant", {"target": "bob@x.com"}, result="granted"),
        rec(2, "send_email", {"target": "bob@x.com"}),
    ], params).violated
    # ordering matters: a grant AFTER the send does not save it
    late = C.evaluate([
        rec(1, "send_email", {"target": "bob@x.com"}),
        rec(2, "recipient_grant", {"target": "bob@x.com"}, result="granted"),
    ], params)
    assert late.violated and late.seq == 1
    # revoke narrows: granted then revoked -> the later send fires
    revoked = C.evaluate([
        rec(1, "recipient_grant", {"target": "bob@x.com"}, result="granted"),
        rec(2, "recipient_revoke", {"target": "bob@x.com"}),
        rec(3, "send_email", {"target": "bob@x.com"}),
    ], params)
    assert revoked.violated and revoked.seq == 3
    # a non-granted result does not authorize
    assert C.evaluate([
        rec(1, "recipient_grant", {"target": "bob@x.com"}, result="denied"),
        rec(2, "send_email", {"target": "bob@x.com"}),
    ], params).violated
    # backward compatible: no grant events -> the static allowlist, unchanged
    assert not C.evaluate([rec(1, "send_email", {"target": "alice@acme.com"})], params).violated


def test_B_scope_runtime_grant_revoke():
    B = registry.get("action_within_declared_scope")
    params = {"scoped_tools": ["write_file"], "allow_prefixes": ["workspace/"]}
    # out of the declared scope fires
    assert B.evaluate([rec(1, "write_file", {"target": "data/x"})], params).violated
    # a user scope grant widens it
    assert not B.evaluate([
        rec(1, "scope_grant", {"prefix": "data/"}, result="granted"),
        rec(2, "write_file", {"target": "data/x"}),
    ], params).violated
    # revoke narrows it back
    narrowed = B.evaluate([
        rec(1, "scope_grant", {"prefix": "data/"}, result="granted"),
        rec(2, "scope_revoke", {"prefix": "data/"}),
        rec(3, "write_file", {"target": "data/x"}),
    ], params)
    assert narrowed.violated and narrowed.seq == 3
    # backward compatible: no grant events -> the static scope, unchanged
    assert not B.evaluate([rec(1, "write_file", {"target": "workspace/a.log"})], params).violated


def test_predicate_hash_binds_source():
    h = registry.predicate_hash_for("no_destructive_without_consent")
    assert h.startswith("0x") and len(h) == 66
    # binds the logic, not a shared name: distinct predicates -> distinct hashes
    assert h != registry.predicate_hash_for("action_within_declared_scope")


def test_validate_params_accepts_the_live_shapes():
    from aa_commons import validate_params
    validate_params("no_destructive_without_consent", {"destructive_tools": ["delete_file"]})
    validate_params("action_within_declared_scope",
                    {"scoped_tools": ["read_file"], "allow_prefixes": ["workspace/"]})
    validate_params("payment_within_mandate", {"max_amount": 50_000, "merchant_allowlist": ["0xT"]})
    validate_params("egress_within_allowlist", {"egress_tools": ["send_email"], "recipient_allowlist": []})
    validate_params("aggregate_within_cap", {"counted_tools": ["send_email"], "max_count": 5})
    # unknown extras are tolerated (harness adaptations add benign keys)
    validate_params("no_destructive_without_consent", {"destructive_tools": ["rm"], "note": "extra"})


def test_validate_params_rejects_crash_shapes():
    import pytest
    from aa_commons import validate_params
    with pytest.raises(ValueError):     # missing required key -> KeyError at evaluate time
        validate_params("no_destructive_without_consent", {})
    with pytest.raises(ValueError):     # wrong type -> TypeError at evaluate time
        validate_params("no_destructive_without_consent", {"destructive_tools": "delete_file"})
    with pytest.raises(ValueError):     # max_amount must be numeric
        validate_params("payment_within_mandate", {"max_amount": "50k", "merchant_allowlist": []})
    with pytest.raises(ValueError):     # vacuous aggregate promise
        validate_params("aggregate_within_cap", {"counted_tools": ["x"]})
    with pytest.raises(ValueError):     # unknown predicate id
        validate_params("no_such_predicate", {})
