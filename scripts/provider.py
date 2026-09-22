"""Provider: post a bond and register the three promises on-chain.

Registering ships {predicate, params} to the store (only the hash is on-chain) so the verifier
can fetch + hash-check them later. Writes the promise ids into deployment.json.
"""
import _config as C
from aa_sdk import Accountability, HttpStore

PROMISES = [
    ("consent", "no_destructive_without_approval",
     {"destructive_tools": ["delete_file"], "approval_tool": "request_approval"}),
    ("scope", "action_within_declared_scope",
     {"scoped_tools": ["delete_file", "read_file"], "allow_prefixes": ["workspace/"]}),
    ("mandate", "payment_within_mandate",
     {"pay_tool": "send_payment", "max_amount": 50, "merchant_allowlist": ["0xGoodVendor"]}),
]


def main() -> None:
    w3 = C.w3()
    ws = C.actors(w3)
    escrow = C.escrow_client(w3, ws)
    prov_addr, _ = ws["provider"]
    acc = Accountability("demo-provider", store=HttpStore(C.STORE_URL), chain=escrow, provider_addr=prov_addr)

    print(f"posting bond {C.BOND} wei from provider {prov_addr} …")
    acc.post_bond(C.BOND)

    d = C.load_deployment()
    for key, predicate, params in PROMISES:
        pid = acc.register_promise(predicate, params, C.PAYOUT)
        d["promises"][key] = pid
        print(f"  registered {key:8s} ({predicate}) -> promiseId {pid}")
    C.save_deployment(d)

    print(f"bond now {escrow.bond_of(prov_addr)} wei ; promises {d['promises']}")
    print(f"  {C.addr_url(escrow.contract.address)}")


if __name__ == "__main__":
    main()
