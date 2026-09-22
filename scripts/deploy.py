"""Deploy Escrow to Base Sepolia and write deployment.json.

Deploys from the VERIFIER wallet so owner == verifier (the protocol operator). provider and
challenger stay distinct from the owner, so the provider cannot swap the verifier — avoids the
"one EOA in multiple roles" tell.
"""
import _config as C
from aa_sdk.chain import EscrowClient


def main() -> None:
    w3 = C.w3()
    assert w3.is_connected(), f"cannot reach {C.RPC_URL}"
    ws = C.actors(w3)
    (prov_addr, _), (chal_addr, _), (ver_addr, _) = ws["provider"], ws["challenger"], ws["verifier"]
    deployer = ws["deployer"][0]

    print(f"deploying Escrow from verifier/owner {ver_addr} …")
    escrow = EscrowClient.deploy(w3, C.ARTIFACT, deployer=deployer, verifier=ver_addr, accounts=C.accounts_map(ws))
    C.save_deployment({
        "address": escrow.contract.address,
        "deploy_block": escrow.deploy_block,
        "provider_addr": prov_addr,
        "challenger_addr": chal_addr,
        "verifier_addr": ver_addr,
        "promises": {},
    })
    print(f"  Escrow @ {escrow.contract.address}  (block {escrow.deploy_block})")
    print(f"  {C.addr_url(escrow.contract.address)}")


if __name__ == "__main__":
    main()
