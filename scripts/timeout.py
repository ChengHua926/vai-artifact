"""Manual claim actions outside the always-on services.

    python scripts/timeout.py deliver  [challenge_id]   provider: deliver the claim's evidence now
    python scripts/timeout.py withdraw [challenge_id]   challenger: bond back when no verdict can come

`challenge_id` defaults to the last one recorded in deployment.json. `deliver` does by hand what
`provider_service.py` does for every claim: read the session's records and parameters from the
provider's store (STORE_TOKEN, STORE_URL), sign them for this claim, and post them to the
verifier (EVIDENCE_URL). It sends no transaction. The verifier accepts evidence only after the
session's final trace commitment and no later than the claim's filing time plus three days
(`EVIDENCE_WINDOW`); a claim without accepted evidence by then is settled as a violation.
`withdraw` refunds the challenge bond when the promise's reserve no longer covers its payout, or
when no verdict landed within VERDICT_WINDOW (10 days) of filing. Nobody is slashed and the pair
stays challengeable.
"""
import sys
from datetime import datetime, timezone

import _config as C

ACTIONS = ("deliver", "withdraw")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ACTIONS:
        sys.exit(__doc__)
    action = sys.argv[1]
    w3 = C.w3()
    ws = C.provider_actors(w3) if action == "deliver" else C.actors(w3)
    escrow = C.escrow_client(w3, ws)
    d = C.load_deployment()
    cid = int(sys.argv[2]) if len(sys.argv) > 2 else int(d["challenge_id"])

    if action == "deliver":
        from aa_sdk import HttpStore
        from aa_sdk.evidence import EvidenceClient, UnlockedSigner, evidence_deadline
        sender, account = ws["provider"]
        deadline = evidence_deadline(escrow, cid)
        receipt = EvidenceClient(C.EVIDENCE_URL).deliver(
            escrow, sender, account or UnlockedSigner(w3, sender), HttpStore(C.STORE_URL), cid)
        print(f"challenge {cid}: evidence {receipt['evidence_hash']} accepted at chain time "
              f"{receipt['received_at']} (deadline {deadline}, "
              f"{datetime.fromtimestamp(deadline, timezone.utc).isoformat()}).")
    else:
        sender, _ = ws["challenger"]
        escrow.withdraw_challenge(sender, cid)
        print(f"challenge {cid} withdrawn: bond refunded, nobody slashed, the pair stays challengeable.")
    print(f"  {C.addr_url(escrow.contract.address)}")


if __name__ == "__main__":
    main()
