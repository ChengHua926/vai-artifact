"""Challenge lifecycle when the trace goes missing (the store rule).

    python scripts/timeout.py respond  [challenge_id]   provider: on-chain "the trace is available"
    python scripts/timeout.py claim    [challenge_id]   anyone: default-settle after the response window
    python scripts/timeout.py withdraw [challenge_id]   challenger: bond back when no verdict can come

`challenge_id` defaults to the last one recorded in deployment.json. `respond` turns off the
default path and tells the verifier to (re-)adjudicate; silence past RESPONSE_WINDOW makes
`claim` settle the challenge as a violation and pay the challenger; `withdraw` refunds the
challenge bond when the provider responded but no verdict landed within VERDICT_WINDOW, or when
the promise was consumed by a slash on another session.
"""
import sys

import _config as C


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "claim"
    assert action in ("respond", "claim", "withdraw"), f"unknown action {action!r} (respond|claim|withdraw)"
    w3 = C.w3()
    ws = C.actors(w3)
    escrow = C.escrow_client(w3, ws)
    d = C.load_deployment()
    cid = int(sys.argv[2]) if len(sys.argv) > 2 else int(d["challenge_id"])

    if action == "respond":
        sender, _ = ws["provider"]
        escrow.respond(sender, cid)
        print(f"responded to challenge {cid}: default path off; the verifier will (re-)adjudicate.")
    elif action == "claim":
        sender, _ = ws["challenger"]
        args = escrow.claim_default(sender, cid)
        print(f"default claimed on challenge {cid}: {int(args['paidToChallenger'])} wei to the challenger "
              f"(payout + bond) — the trace was never produced.")
    else:
        sender, _ = ws["challenger"]
        escrow.withdraw_challenge(sender, cid)
        print(f"challenge {cid} withdrawn: bond refunded, nobody slashed, the pair stays challengeable.")
    print(f"  {C.addr_url(escrow.contract.address)}")


if __name__ == "__main__":
    main()
