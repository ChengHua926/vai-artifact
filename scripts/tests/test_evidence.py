"""Receipt accounting and revision checks used by the public testnet run."""
import importlib.util
from pathlib import Path

import pytest
from types import SimpleNamespace


def evidence():
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1]))
    spec = importlib.util.spec_from_file_location("revision_evidence", Path(__file__).parents[1] / "run_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_receipt_preserves_base_l1_fee():
    result = evidence().receipt_summary({"transactionHash": bytes.fromhex("11" * 32),
        "blockNumber": 3, "status": 1, "gasUsed": 21000,
        "effectiveGasPrice": 4, "l1Fee": "0x10"}, 1.25)
    assert result["execution_fee_wei"] == 84000
    assert result["l1_fee_wei"] == 16
    assert result["execution_plus_l1_fee_wei"] == 84016
    assert result["confirmation_seconds"] == 1.25


def test_missing_l1_fee_is_not_reported_as_zero():
    result = evidence().receipt_summary({"transactionHash": bytes.fromhex("11" * 32),
        "blockNumber": 3, "status": 1, "gasUsed": 21000,
        "effectiveGasPrice": 4}, 1)
    assert result["l1_fee_wei"] is None
    assert result["execution_plus_l1_fee_wei"] is None


def test_public_run_rejects_uncommitted_source():
    with pytest.raises(ValueError, match="committed"):
        evidence().validate_revision("abc", " M contracts/src/Escrow.sol", 84532, True)


def test_local_development_run_must_explicitly_allow_dirty_source():
    with pytest.raises(ValueError, match="committed"):
        evidence().validate_revision("abc", " M scripts/run_evidence.py", 31337, False)
    evidence().validate_revision("abc", " M scripts/run_evidence.py", 31337, True)


def test_imported_package_from_another_checkout_is_rejected(tmp_path):
    wrong = SimpleNamespace(__file__=str(tmp_path / "another-checkout/aa_sdk/__init__.py"), __name__="aa_sdk")
    with pytest.raises(ValueError, match="outside the recorded checkout"):
        evidence().module_sources((wrong,))


def test_receipt_loss_prevents_complete_evidence_claim():
    attempts = [{"state": "confirmed"}, {"state": "unconfirmed"}]
    assert evidence().evidence_status(attempts) == "scenarios_passed_evidence_incomplete"
    assert evidence().evidence_status([{"state": "confirmed"}]) == "passed"


@pytest.mark.parametrize("inventory", [{"promise_count": 1, "record_count": 0},
    {"promise_count": 0, "record_count": 1}, {}, {"promise_count": 0}])
def test_new_deployment_refuses_occupied_or_unknown_store(inventory):
    with pytest.raises(ValueError, match="fresh dedicated"):
        evidence().validate_store_inventory(inventory)


def test_new_deployment_accepts_empty_store():
    evidence().validate_store_inventory({"promise_count": 0, "record_count": 0})


@pytest.mark.parametrize("response_kind", ["empty", "base_error"])
def test_deployment_check_retries_empty_code_at_receipt_block(response_kind):
    module = evidence()
    calls = []
    values = iter((b"", b"expected"))
    def code(address, block_identifier):
        calls.append(block_identifier)
        value = next(values)
        if not value and response_kind == "base_error":
            detail = {"code": -32001, "message": "block not found: 0x2a"}
            raise module.Web3RPCError(str(detail), rpc_response={"error": detail})
        return value
    w3 = SimpleNamespace(eth=SimpleNamespace(get_code=code))
    assert evidence().confirmed_runtime(w3, "contract", b"expected", 42, interval=0) == b"expected"
    assert calls == [42, 42]


def test_deployment_check_rejects_nonempty_wrong_bytecode():
    w3 = SimpleNamespace(eth=SimpleNamespace(get_code=lambda *a, **k: b"wrong"))
    with pytest.raises(ValueError, match="differs"):
        evidence().confirmed_runtime(w3, "contract", b"expected", 42)


def test_deployment_check_bounds_unavailable_receipt_block():
    def unavailable(*args, **kwargs):
        raise evidence().BlockNotFound("not visible yet")
    w3 = SimpleNamespace(eth=SimpleNamespace(get_code=unavailable))
    with pytest.raises(TimeoutError, match="receipt block"):
        evidence().confirmed_runtime(w3, "contract", b"expected", 42, timeout=0)


@pytest.mark.parametrize("error_name", ["BlockNotFound", "BadFunctionCallOutput", "Web3RPCError"])
def test_deployment_roles_retry_missing_receipt_block(error_name):
    module = evidence()
    calls = []
    def owner(block_identifier):
        calls.append(block_identifier)
        if len(calls) == 1:
            if error_name == "Web3RPCError":
                detail = {"code": -32001, "message": "block not found: 0x2a"}
                raise module.Web3RPCError(str(detail), rpc_response={"error": detail})
            raise getattr(module, error_name)("backend behind")
        return "operator"
    contract = SimpleNamespace(functions=SimpleNamespace(
        owner=lambda: SimpleNamespace(call=owner),
        verifier=lambda: SimpleNamespace(call=lambda **k: "operator")))
    module.confirmed_operator(contract, "operator", 42, interval=0)
    assert calls == [42, 42]


def test_deployment_roles_reject_wrong_operator():
    contract = SimpleNamespace(functions=SimpleNamespace(
        owner=lambda: SimpleNamespace(call=lambda **k: "provider"),
        verifier=lambda: SimpleNamespace(call=lambda **k: "operator")))
    with pytest.raises(ValueError, match="operator"):
        evidence().confirmed_operator(contract, "operator", 42)


def test_deployment_role_retries_are_bounded():
    module = evidence()
    def unavailable(**kwargs):
        raise module.BlockNotFound("backend behind")
    contract = SimpleNamespace(functions=SimpleNamespace(
        owner=lambda: SimpleNamespace(call=unavailable),
        verifier=lambda: SimpleNamespace(call=unavailable)))
    with pytest.raises(TimeoutError, match="roles unavailable"):
        module.confirmed_operator(contract, "operator", 42, timeout=0)
