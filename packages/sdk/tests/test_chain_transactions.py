"""Receipt failure and nonce races must not be mistaken for successful anchoring."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest
from web3.exceptions import ContractLogicError, Web3RPCError

from aa_sdk.chain import EscrowClient

ADDRESS = "0x" + "11" * 20


def client_and_function(status=1):
    class Eth:
        chain_id = 31337
        block_number = 9
        nonce = 0
        nonces = []
        def get_transaction_count(self, address, tag):
            return self.nonce
        def send_raw_transaction(self, raw):
            self.nonces.append(raw)
            self.nonce = max(self.nonce, raw + 1)
            return raw
        def wait_for_transaction_receipt(self, tx):
            return SimpleNamespace(status=status, transactionHash=b"x" * 32, blockNumber=12)
        def contract(self, **kwargs):
            return None
    eth = Eth()
    accounts = {ADDRESS: SimpleNamespace(sign_transaction=lambda tx: SimpleNamespace(raw_transaction=tx["nonce"]))}
    chain = EscrowClient(SimpleNamespace(eth=eth), ADDRESS, [], accounts)
    class Function:
        def estimate_gas(self, tx, block_identifier="latest"):
            return 100000
        def build_transaction(self, tx):
            time.sleep(0.02)  # without the lock both builders would receive the same pending nonce
            return tx
    return chain, Function(), eth


def test_concurrent_checkpoint_and_foreground_transaction_use_distinct_nonces():
    chain, fn, eth = client_and_function()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: chain._send(fn, ADDRESS), range(2)))
    assert eth.nonces == [0, 1]


def test_distinct_connections_to_same_chain_share_sender_nonce_lock():
    first, fn, eth = client_and_function()
    # Separate connection facade, same node state, as in two native sessions' C.w3() calls.
    class OtherConnectionEth:
        chain_id = eth.chain_id
        block_number = eth.block_number
        get_transaction_count = staticmethod(eth.get_transaction_count)
        send_raw_transaction = staticmethod(eth.send_raw_transaction)
        wait_for_transaction_receipt = staticmethod(eth.wait_for_transaction_receipt)
        contract = staticmethod(eth.contract)
    second = EscrowClient(SimpleNamespace(eth=OtherConnectionEth()), ADDRESS, [], first.accounts)
    assert first.w3 is not second.w3 and first.w3.eth is not second.w3.eth
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda client: client._send(fn, ADDRESS), [first, second]))
    assert eth.nonces == [0, 1]


def test_mined_revert_is_not_success():
    chain, fn, _ = client_and_function(status=0)
    with pytest.raises(RuntimeError, match="reverted"):
        chain._send(fn, ADDRESS)


class NewlyOpenedChallengeResponse:
    """Model the observed Base failure: latest lacks the just-confirmed challenge."""
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.estimates = []
        self.built = []

    def estimate_gas(self, tx, block_identifier="latest"):
        self.estimates.append((dict(tx), block_identifier))
        if self.failures:
            raise self.failures.pop(0)
        if block_identifier != 12:
            raise ContractLogicError("execution reverted: not open")
        return 73123

    def build_transaction(self, tx):
        # Like web3.py, fill an omitted gas field with an implicit latest estimate.
        if "gas" not in tx:
            tx = {**tx, "gas": self.estimate_gas(tx)}
        self.built.append(dict(tx))
        return tx


def test_keyed_gas_estimate_uses_confirmed_challenge_block_and_explicit_gas():
    chain, previous_fn, eth = client_and_function()
    chain._send(previous_fn, ADDRESS)  # challenge confirmed at 12; RPC head still says 9
    response = NewlyOpenedChallengeResponse()
    chain._send(response, ADDRESS, value=7)
    assert response.estimates == [({"from": ADDRESS, "value": 7, "nonce": 1}, 12)]
    assert response.built == [{"from": ADDRESS, "value": 7, "nonce": 1, "gas": 73123}]
    assert eth.nonces == [0, 1]  # exactly one broadcast for each operation


def test_missing_estimate_block_retries_before_single_broadcast(monkeypatch):
    chain, previous_fn, eth = client_and_function()
    chain._send(previous_fn, ADDRESS)
    sleeps = []
    monkeypatch.setattr("aa_sdk.chain.time.sleep", sleeps.append)
    detail = {"code": -32001, "message": "block not found: 0xc"}
    response = NewlyOpenedChallengeResponse([Web3RPCError(str(detail), rpc_response={"error": detail})])
    chain._send(response, ADDRESS)
    assert [block for _, block in response.estimates] == [12, 12]
    assert sleeps == [0.5]
    assert len(response.built) == 1 and eth.nonces == [0, 1]


def test_revert_at_explicit_estimate_block_propagates_without_broadcast(monkeypatch):
    chain, previous_fn, eth = client_and_function()
    chain._send(previous_fn, ADDRESS)
    monkeypatch.setattr("aa_sdk.chain.time.sleep", lambda _: pytest.fail("unexpected retry"))
    response = NewlyOpenedChallengeResponse([ContractLogicError("execution reverted: not open")])
    with pytest.raises(ContractLogicError, match="not open"):
        chain._send(response, ADDRESS)
    assert len(response.estimates) == 1 and response.estimates[0][1] == 12
    assert response.built == [] and eth.nonces == [0]
