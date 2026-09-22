"""Challenger: file a challenge on a promise for this session.

    python scripts/challenge.py [consent|scope]      (default: consent)

The challenger is the party recorded on the session, so the privacy check passes. Posts a
challenge bond; the always-on verifier picks up the Challenged event and adjudicates.
"""
import sys

import _config as C


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "consent"
    w3 = C.w3()
    ws = C.actors(w3)
    escrow = C.escrow_client(w3, ws)
    chal_addr, _ = ws["challenger"]

    d = C.load_deployment()
    session_id = d["session_id"]
    promise_id = d["promises"][which]
    print(f"challenging session {session_id[:14]}… on {which} promise {promise_id} "
          f"(bond {C.CHALLENGE_BOND} wei) …")
    cid = escrow.challenge(chal_addr, session_id, promise_id, C.CHALLENGE_BOND)
    d["challenge_id"] = cid
    C.save_deployment(d)
    print(f"challenge filed: id {cid} — watch the verifier service adjudicate it.")
    print(f"  {C.addr_url(escrow.contract.address)}")


if __name__ == "__main__":
    main()
