"""Shared config for the lifecycle scripts.

LOCAL (anvil) by default — set AA_LOCAL=0 for the Base Sepolia toy-wallet path. In LOCAL mode
the roles map to anvil's unlocked default accounts (no keys, no funding); owner == verifier.
Coordination state is separated by chain and deployment under _runtime/.
"""
from __future__ import annotations

import json
import os
import re
import uuid
import hashlib
import sys
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
_PACKAGE_ROOTS = {
    "aa_commons": Path(REPO) / "packages/commons/aa_commons",
    "aa_sdk": Path(REPO) / "packages/sdk/aa_sdk",
    "aa_verifier": Path(REPO) / "packages/verifier/aa_verifier",
}


def verify_package_origins() -> dict:
    """Reject a process that already loaded protocol code from another checkout."""
    origins = {}
    for name, module in tuple(sys.modules.items()):
        root = _PACKAGE_ROOTS.get(name.split(".", 1)[0])
        if root is None or module is None:
            continue
        source = getattr(module, "__file__", None)
        if source is None or not Path(source).resolve().is_relative_to(root):
            raise ValueError(f"{name} was imported outside the configured checkout")
        origins[name] = str(Path(source).resolve().relative_to(REPO))
    return origins


def bootstrap_packages() -> None:
    """Bind every script's imports before editable installs can select a different worktree."""
    verify_package_origins()
    for package in ("commons", "sdk", "store", "verifier"):
        path = str(Path(REPO) / "packages" / package)
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


bootstrap_packages()

from web3 import Web3
from aa_sdk.chain import EscrowClient

verify_package_origins()
ARTIFACT = os.path.join(REPO, "contracts", "out", "Escrow.sol", "Escrow.json")
SECRETS = os.environ.get("AA_SECRETS_DIR", os.path.join(REPO, "secrets"))
KEYSTORE_DIR = os.environ.get("AA_KEYSTORE_DIR")

LOCAL = os.environ.get("AA_LOCAL", "1") == "1"
CHAIN_ID = int(os.environ.get("AA_CHAIN_ID", "31337" if LOCAL else "84532"))
if (LOCAL and CHAIN_ID != 31337) or (not LOCAL and CHAIN_ID not in (8453, 84532)):
    raise ValueError("AA_LOCAL and AA_CHAIN_ID must select Anvil, Base, or Base Sepolia")
RUNTIME = os.path.join(REPO, "_runtime", str(CHAIN_ID))
DEPLOYMENT = os.environ.get("AA_DEPLOYMENT", os.path.join(RUNTIME, "deployment.json"))
RPC_URL = os.environ.get("AA_RPC_URL", {31337: "http://127.0.0.1:8545",
    8453: "https://mainnet.base.org", 84532: "https://sepolia.base.org"}[CHAIN_ID])
STORE_URL = os.environ.get("STORE_URL", "http://127.0.0.1:8000")
EVIDENCE_URL = os.environ.get("EVIDENCE_URL", "http://127.0.0.1:8001")
EXPLORER = {31337: None, 8453: "https://basescan.org", 84532: "https://sepolia.basescan.org"}[CHAIN_ID]

# tiny demo amounts (wei)
BOND = 3 * 10**14
PAYOUT = 1 * 10**14
CHALLENGE_BOND = 5 * 10**13

_WALLET_FILES = {"provider": "wallet.txt", "challenger": "wallet_challenger.txt", "verifier": "wallet_verifier.txt"}


def w3() -> Web3:
    verify_package_origins()
    connected = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    actual = connected.eth.chain_id
    if actual != CHAIN_ID:
        raise ValueError(f"expected chain {CHAIN_ID}, connected to {actual}")
    return connected


def _remote_wallet(path: str):
    from eth_account import Account
    text = open(path).read()
    addr = re.search(r"Address:\s*(0x[0-9a-fA-F]{40})", text).group(1)
    key = re.search(r"Private key:\s*(0x[0-9a-fA-F]{64})", text).group(1)
    declared, account = Web3.to_checksum_address(addr), Account.from_key(key)
    if declared != account.address:
        raise ValueError("wallet address does not match its signing key")
    return declared, account


def _wallet(role):
    if KEYSTORE_DIR is None:
        return _remote_wallet(os.path.join(SECRETS, _WALLET_FILES[role]))
    from eth_account import Account
    key = Path(KEYSTORE_DIR) / f"{role}.json"
    password = Path(KEYSTORE_DIR) / f"{role}.password"
    if key.stat().st_mode & 0o077 or password.stat().st_mode & 0o077:
        raise ValueError("wallet files must have private permissions")
    account = Account.from_key(Account.decrypt(json.loads(key.read_text()), password.read_text().strip()))
    return account.address, account


def actors(connected_w3: Web3) -> dict:
    """{role: (address, account_or_None)} for deployer/provider/challenger/verifier.
    LOCAL: anvil's unlocked accounts (account=None -> the .transact path). REMOTE: funded toy wallets."""
    verify_package_origins()
    if LOCAL:
        a = connected_w3.eth.accounts
        ver = a[3]
        return {"deployer": (ver, None), "provider": (a[1], None), "challenger": (a[2], None), "verifier": (ver, None)}
    out = {role: _wallet(role) for role in _WALLET_FILES}
    out["deployer"] = out["verifier"]   # owner == verifier (the protocol operator)
    return out


def _role_actors(connected_w3, role, deployment=None):
    """Load only this process's signing key; other roles are public metadata."""
    verify_package_origins()
    if connected_w3.eth.chain_id != CHAIN_ID:
        raise ValueError("connected chain does not match deployment configuration")
    deployment = load_deployment() if deployment is None else deployment
    if deployment.get("chain_id") != CHAIN_ID:
        raise ValueError("deployment chain does not match role configuration")
    addresses = {name: Web3.to_checksum_address(deployment[f"{name}_addr"])
                 for name in ("provider", "challenger", "verifier")}
    if len(set(addresses.values())) != 3:
        raise ValueError("provider, challenger and verifier must be separate roles")
    if LOCAL:
        if addresses[role] not in connected_w3.eth.accounts:
            raise ValueError(f"deployment {role} is not an unlocked local account")
        signer = (addresses[role], None)
    else:
        signer = _wallet(role)
        if signer[0] != addresses[role]:
            raise ValueError(f"configured {role} differs from deployment")
    out = {name: (address, None) for name, address in addresses.items()}
    out[role] = signer
    out["deployer"] = out["verifier"]
    return out


def verifier_actors(connected_w3, deployment=None):
    return _role_actors(connected_w3, "verifier", deployment)


def provider_actors(connected_w3, deployment=None):
    return _role_actors(connected_w3, "provider", deployment)


def accounts_map(actors_dict: dict) -> dict:
    return {addr: acct for (addr, acct) in actors_dict.values() if acct is not None}


def escrow_client(connected_w3: Web3, actors_dict: dict) -> EscrowClient:
    verify_package_origins()
    if connected_w3.eth.chain_id != CHAIN_ID:
        raise ValueError("connected chain does not match deployment configuration")
    artifact = Path(ARTIFACT).read_bytes()
    compiled = json.loads(artifact)
    abi = compiled["abi"]
    deployment = load_deployment()
    if deployment.get("artifact_sha256") != hashlib.sha256(artifact).hexdigest():
        raise ValueError("deployment artifact differs from the current build")
    runtime = connected_w3.eth.get_code(deployment["address"])
    expected = bytes.fromhex(compiled["deployedBytecode"]["object"].removeprefix("0x"))
    if runtime != expected:
        raise ValueError("deployed bytecode differs from the recorded artifact")
    client = EscrowClient(connected_w3, deployment["address"], abi, accounts_map(actors_dict))
    provider, challenger, verifier = (actors_dict[r][0] for r in ("provider", "challenger", "verifier"))
    if len({provider, challenger, verifier}) != 3:
        raise ValueError("provider, challenger and verifier must be separate roles")
    for role in ("provider", "challenger", "verifier"):
        if deployment.get(f"{role}_addr") != actors_dict[role][0]:
            raise ValueError(f"configured {role} differs from deployment")
    if (client.contract.functions.owner().call() != verifier
            or client.contract.functions.verifier().call() != verifier):
        raise ValueError("verifier/owner differs from the independent operator configuration")
    client.deploy_block = deployment["deploy_block"]
    return client


def load_deployment() -> dict:
    with open(DEPLOYMENT) as f:
        deployment = json.load(f)
    if deployment.get("chain_id") != CHAIN_ID:
        raise ValueError(f"deployment chain does not match {CHAIN_ID}; deploy with the current scripts")
    return deployment


def save_deployment(d: dict) -> None:
    deployment = dict(d)
    deployment.setdefault("chain_id", CHAIN_ID)
    if deployment["chain_id"] != CHAIN_ID:
        raise ValueError("cannot save deployment from a different chain")
    deployment.setdefault("deployment_id", uuid.uuid4().hex)
    if Path(ARTIFACT).exists():
        deployment.setdefault("artifact_sha256", hashlib.sha256(Path(ARTIFACT).read_bytes()).hexdigest())
    path = Path(DEPLOYMENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(deployment, indent=2) + "\n")
    temporary.replace(path)


def verifier_cursor_path(deployment: dict) -> str:
    """A scan cursor belongs to one chain and deployment, never to a trace prefix."""
    address = Web3.to_checksum_address(deployment["address"]).lower()
    identity = deployment["deployment_id"]
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ValueError("invalid deployment identity")
    return os.path.join(RUNTIME, f"verifier-{address}-{identity}.cursor")


def provider_cursor_path(deployment):
    return verifier_cursor_path(deployment).replace("verifier-", "provider-")


def evidence_inbox_path(deployment):
    return os.environ.get("EVIDENCE_DB", verifier_cursor_path(deployment).replace(".cursor", ".inbox.sqlite3"))


def addr_url(a: str) -> str:
    return a if EXPLORER is None else f"{EXPLORER}/address/{a}"
