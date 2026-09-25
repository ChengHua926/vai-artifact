"""Lagging RPC heads must not erase state already confirmed to this client."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
from web3.exceptions import BlockNotFound, Web3RPCError

from aa_sdk.chain import EscrowClient

ADDRESS = "0x" + "11" * 20
SID = "0x" + "22" * 32
REPO = Path(__file__).resolve().parents[3]
ARTIFACT = REPO / "contracts/out/Escrow.sol/Escrow.json"


def built_abi():
    """The ABI the scripts load; it must be built from the current Solidity source."""
    from web3 import Web3
    if not ARTIFACT.exists():
        pytest.skip("build the contract first: forge build --offline --root contracts")
    compiled = json.loads(ARTIFACT.read_text())
    metadata = compiled["metadata"]
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    source = (REPO / "contracts/src/Escrow.sol").read_bytes()
    assert metadata["sources"]["src/Escrow.sol"]["keccak256"] == Web3.to_hex(Web3.keccak(source)), \
        "contract build is stale; rebuild before testing the client ABI"
    return compiled["abi"]


def test_client_and_abi_have_no_response_or_default_path():
    names = {item.get("name") for item in built_abi()}
    assert not names & {"respond", "claimDefault", "Responded", "DefaultClaimed", "RESPONSE_WINDOW"}
    assert {"challenge", "submitVerdict", "withdrawChallenge", "Challenged", "Verdict"} <= names
    assert not hasattr(EscrowClient, "respond") and not hasattr(EscrowClient, "claim_default")


def test_get_challenge_decodes_the_six_field_claim():
    from eth_abi import encode
    from web3 import Web3
    from web3.providers.base import BaseProvider
    abi = built_abi()
    outputs = next(item for item in abi if item.get("name") == "challenges")["outputs"]
    assert [o["name"] for o in outputs] == ["sessionId", "promiseId", "challenger", "bond", "status", "challengedAt"]
    claim = (bytes.fromhex(SID[2:]), 3, Web3.to_checksum_address("0x" + "33" * 20), 7, 1, 1_700_000_000)
    encoded = encode([o["type"] for o in outputs], list(claim))

    class OneClaim(BaseProvider):
        def make_request(self, method, params):
            results = {"eth_chainId": hex(31337), "eth_blockNumber": hex(9), "eth_call": Web3.to_hex(encoded)}
            return {"jsonrpc": "2.0", "id": 1, "result": results[method]}

    client = EscrowClient(Web3(OneClaim()), ADDRESS, abi)
    assert tuple(client.get_challenge(1)) == claim


def lagging_client():
    calls = []
    class Functions:
        failures = []
        def __getattr__(self, name):
            def function(*args):
                def call(block_identifier="latest"):
                    calls.append((name, args, block_identifier))
                    if self.failures:
                        raise self.failures.pop(0)
                    if name == "checkpointCount":
                        return 2 if block_identifier == 12 else 0
                    if name == "traceCheckpoints":
                        return (args[1] + 1, bytes([args[1] + 1]) * 32)
                    # A stale read is well formed, but falsely says the write is absent.
                    return "confirmed state" if block_identifier == 12 else "stale state"
                return SimpleNamespace(call=call)
            return function
    functions = Functions()
    class Eth:
        chain_id = 31337
        block_number = 9
        def contract(self, **kwargs):
            if "bytecode" in kwargs:
                return SimpleNamespace(constructor=lambda verifier: SimpleNamespace(
                    transact=lambda options: b"x" * 32))
            return SimpleNamespace(functions=functions)
        def wait_for_transaction_receipt(self, tx):
            return SimpleNamespace(status=1, transactionHash=b"x" * 32,
                                   blockNumber=12, contractAddress=ADDRESS)
    eth = Eth()
    client = EscrowClient(SimpleNamespace(eth=eth), ADDRESS, [])
    fn = SimpleNamespace(transact=lambda options: b"x" * 32)
    client._send(fn, ADDRESS)
    return client, functions, calls


@pytest.mark.parametrize("method,args", [
    ("get_session", (SID,)), ("get_promise", (1,)),
    ("get_challenge", (1,)), ("bond_of", (ADDRESS,)),
])
def test_read_never_uses_head_older_than_successful_receipt(method, args):
    client, _, calls = lagging_client()
    assert getattr(client, method)(*args) == "confirmed state"
    assert calls[-1][2] == 12


def test_checkpoints_share_receipt_bounded_snapshot_even_if_head_moves():
    client, functions, calls = lagging_client()
    original_count = functions.checkpointCount
    def checkpoint_count(sid):
        def call(block_identifier):
            value = original_count(sid).call(block_identifier=block_identifier)
            client.w3.eth.block_number = 15
            return value
        return SimpleNamespace(call=call)
    functions.checkpointCount = checkpoint_count
    checkpoints = client.get_checkpoints(SID)
    assert [p["record_count"] for p in checkpoints] == [1, 2]
    assert [call[2] for call in calls] == [12, 12, 12]


def test_deployment_receipt_seeds_first_read_floor(tmp_path):
    client, _, calls = lagging_client()
    artifact = tmp_path / "escrow.json"
    artifact.write_text(json.dumps({"abi": [], "bytecode": {"object": "0x00"}}))
    deployed = EscrowClient.deploy(client.w3, str(artifact), ADDRESS, ADDRESS)
    assert deployed.deploy_receipt.blockNumber == 12
    assert deployed.get_session(SID) == "confirmed state"
    assert calls[-1][2] == 12


def test_older_receipt_cannot_lower_floor():
    client, _, calls = lagging_client()
    client.w3.eth.wait_for_transaction_receipt = lambda tx: SimpleNamespace(
        status=1, transactionHash=b"y" * 32, blockNumber=10)
    client._send(SimpleNamespace(transact=lambda options: b"y" * 32), ADDRESS)
    assert client.get_session(SID) == "confirmed state"
    assert calls[-1][2] == 12


def test_explicit_observed_block_retries_only_block_not_found(monkeypatch):
    client, functions, calls = lagging_client()
    sleeps = []
    monkeypatch.setattr("aa_sdk.chain.time.sleep", sleeps.append)
    functions.failures = [BlockNotFound("backend head is 9"), BlockNotFound("backend head is 11")]
    assert client.get_session(SID) == "confirmed state"
    assert [call[2] for call in calls] == [12, 12, 12]
    assert sleeps == [0.5, 0.5]


def test_block_lag_retries_are_bounded(monkeypatch):
    client, functions, calls = lagging_client()
    sleeps = []
    monkeypatch.setattr("aa_sdk.chain.time.sleep", sleeps.append)
    functions.failures = [BlockNotFound("backend remains behind")] * 10
    with pytest.raises(BlockNotFound, match="remains behind"):
        client.get_session(SID)
    assert len(calls) == 6
    assert sleeps == [0.5] * 5


@pytest.mark.parametrize("error", [ConnectionError("RPC transport down"), ValueError("bad response")])
def test_other_errors_are_not_retried(monkeypatch, error):
    client, functions, calls = lagging_client()
    monkeypatch.setattr("aa_sdk.chain.time.sleep", lambda _: pytest.fail("unexpected retry"))
    functions.failures = [error]
    with pytest.raises(type(error), match=str(error)):
        client.get_session(SID)
    assert len(calls) == 1


def test_newer_observed_head_is_used_without_reinterpreting_returned_data():
    client, _, calls = lagging_client()
    client.w3.eth.block_number = 15
    # The wrapper cannot certify arbitrary returned data: only its requested snapshot.
    assert client.get_session(SID) == "stale state"
    assert calls[-1][2] == 15


def test_base_missing_block_rpc_response_retries_same_snapshot(monkeypatch):
    client, functions, calls = lagging_client()
    sleeps = []
    monkeypatch.setattr("aa_sdk.chain.time.sleep", sleeps.append)
    response = {"error": {"code": -32001, "message": "block not found: 0xc"}}
    functions.failures = [Web3RPCError(str(response["error"]), rpc_response=response)]
    assert client.get_session(SID) == "confirmed state"
    assert [call[2] for call in calls] == [12, 12]
    assert sleeps == [0.5]


@pytest.mark.parametrize("detail", [{"code": -32001, "message": "block not found: 0xd"},
    {"code": -32000, "message": "block not found: 0xc"},
    {"code": -32001, "message": "unrelated node failure"}])
def test_unrelated_rpc_responses_do_not_enter_missing_block_retry(monkeypatch, detail):
    client, functions, _ = lagging_client()
    monkeypatch.setattr("aa_sdk.chain.time.sleep", lambda _: pytest.fail("unexpected retry"))
    functions.failures = [Web3RPCError(str(detail), rpc_response={"error": detail})]
    with pytest.raises(Web3RPCError):
        client.get_session(SID)
