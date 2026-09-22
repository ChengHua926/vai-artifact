"""Deployment metadata must not cross networks or reuse verifier cursors."""
import importlib.util
from pathlib import Path

import pytest
from eth_account import Account
from types import SimpleNamespace


def config(monkeypatch, tmp_path, local):
    monkeypatch.setenv("AA_LOCAL", "1" if local else "0")
    monkeypatch.setenv("AA_DEPLOYMENT", str(tmp_path / "deployment.json"))
    spec = importlib.util.spec_from_file_location("revision_config", Path(__file__).parents[1] / "_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_network_identity_is_required(monkeypatch, tmp_path):
    c = config(monkeypatch, tmp_path, local=False)
    c.save_deployment({"address": "0x" + "11" * 20, "deploy_block": 7})
    assert c.load_deployment()["chain_id"] == 84532
    local = config(monkeypatch, tmp_path, local=True)
    with pytest.raises(ValueError, match="chain"):
        local.load_deployment()


def test_each_deployment_has_a_separate_cursor(monkeypatch, tmp_path):
    c = config(monkeypatch, tmp_path, local=True)
    deployment = {"address": "0x" + "11" * 20, "deploy_block": 7}
    c.save_deployment(deployment)
    first = c.load_deployment()
    c.save_deployment(deployment)
    second = c.load_deployment()
    assert c.verifier_cursor_path(first) != c.verifier_cursor_path(second)
    second["promises"] = {"consent": 1}
    c.save_deployment(second)
    assert c.verifier_cursor_path(second) == c.verifier_cursor_path(c.load_deployment())


def test_legacy_metadata_requires_explicit_redeployment(monkeypatch, tmp_path):
    c = config(monkeypatch, tmp_path, local=False)
    Path(c.DEPLOYMENT).write_text('{"address": "0x1111111111111111111111111111111111111111"}')
    with pytest.raises(ValueError, match="chain"):
        c.load_deployment()


def test_wallet_declaration_matches_key(monkeypatch, tmp_path):
    c = config(monkeypatch, tmp_path, local=False)
    account = Account.create()
    path = tmp_path / "ephemeral-wallet.txt"
    path.write_text(f"Address: {account.address}\nPrivate key: 0x{bytes(account.key).hex()}\n")
    assert c._remote_wallet(path)[0] == account.address
    path.write_text(f"Address: 0x{'11'*20}\nPrivate key: 0x{bytes(account.key).hex()}\n")
    with pytest.raises(ValueError, match="address does not match"):
        c._remote_wallet(path)


def test_reused_deployment_rejects_wrong_bytecode(monkeypatch, tmp_path):
    c = config(monkeypatch, tmp_path, local=True)
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"abi": [], "deployedBytecode": {"object": "0x6000"}}')
    c.ARTIFACT = str(artifact)
    c.save_deployment({"address": "0x" + "11" * 20, "deploy_block": 7})
    wrong_chain = SimpleNamespace(eth=SimpleNamespace(chain_id=31337, get_code=lambda _: bytes.fromhex("6001")))
    with pytest.raises(ValueError, match="bytecode differs"):
        c.escrow_client(wrong_chain, {})


def test_verifier_needs_only_its_own_wallet_and_metadata_addresses(monkeypatch, tmp_path):
    c = config(monkeypatch, tmp_path, local=False)
    c.SECRETS = str(tmp_path / "operator-secrets")
    Path(c.SECRETS).mkdir()
    verifier = Account.create()
    wallet = Path(c.SECRETS) / "wallet_verifier.txt"
    wallet.write_text(f"Address: {verifier.address}\nPrivate key: 0x{bytes(verifier.key).hex()}\n")
    c.save_deployment({"address": "0x" + "33" * 20, "deploy_block": 7,
        "provider_addr": "0x" + "11" * 20, "challenger_addr": "0x" + "22" * 20,
        "verifier_addr": verifier.address})
    roles = c.verifier_actors(SimpleNamespace(eth=SimpleNamespace(chain_id=84532)))
    assert roles["provider"] == ("0x" + "11" * 20, None)
    assert roles["challenger"] == ("0x" + "22" * 20, None)
    assert set(c.accounts_map(roles)) == {verifier.address}
    assert {p.name for p in Path(c.SECRETS).iterdir()} == {"wallet_verifier.txt"}
    different = Account.create()
    wallet.write_text(f"Address: {different.address}\nPrivate key: 0x{bytes(different.key).hex()}\n")
    with pytest.raises(ValueError, match="verifier.*deployment"):
        c.verifier_actors(SimpleNamespace(eth=SimpleNamespace(chain_id=84532)))
