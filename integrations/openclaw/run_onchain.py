"""Full lifecycle SOURCED FROM openclaw's event shape, on a local chain — the analog of
hermes/run_onchain.py.

An openclaw `write` OUTSIDE the declared scope -> AAP-2 violation -> on-chain challenge -> verifier
re-runs the predicate over the openclaw-sourced trace -> escrow slashes the promise reserve by exactly the
payout. Reuses the prototype's chain stack (_config, EscrowClient, aa_verifier) unchanged.

Because openclaw is a separate TS process, this deterministic script feeds the exact event the
plugin emits (proven to come from openclaw's REAL dispatch by plugin/tests/aa_seam.e2e.test.ts)
rather than driving openclaw in-process. The live, model-driven path is stage 4 (deferred).
AAP-2 is the slashed promise here; AAP-5 (aggregate) is shown only in seam_test.py — its cap is not
yet calibrated, so it is not slashed (see FINDINGS.md).

Prereqs (local): anvil running; the store running (see ../../docs/RUNBOOK.md).
Run:  .venv/bin/python integrations/openclaw/run_onchain.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.environ.get("AA_PROTO", os.path.dirname(os.path.dirname(HERE)))   # <repo>/integrations/openclaw -> <repo>
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PROTO, "scripts"))

import requests

import _config as C
from aa_sdk import HttpStore
from aa_sdk.chain import EscrowClient
from aa_verifier import process_challenge
import aa_openclaw


def main() -> int:
    for _ in range(40):
        try:
            if requests.get(C.STORE_URL + "/health", timeout=1).ok:
                break
        except Exception:
            time.sleep(0.25)
    else:
        sys.exit("store not reachable — run the FastAPI store first (see docs/RUNBOOK.md)")

    w3 = C.w3()
    assert w3.is_connected(), "anvil not reachable"
    ws = C.actors(w3)
    deployer, provider, challenger, verifier = (ws["deployer"][0], ws["provider"][0],
                                                ws["challenger"][0], ws["verifier"][0])
    escrow = EscrowClient.deploy(w3, C.ARTIFACT, deployer=deployer, verifier=verifier,
                                 accounts=C.accounts_map(ws))

    acc, sess = aa_openclaw.begin_session(party=challenger, store=HttpStore(C.STORE_URL), chain=escrow,
                                          provider_addr=provider, bond_wei=C.BOND, payout_wei=C.PAYOUT)
    scope_pid = acc.promises[0].promise_id

    print("feeding an openclaw write OUTSIDE workspace/ through the aa_openclaw seam ...")
    aa_openclaw.record_action("write", {"path": "outside.txt", "content": "leaked report\n"}, {"ok": True})
    summary = aa_openclaw.end_session()      # flushes, reads the store's records back, commits THEIR hash on-chain
    print(f"committed session {summary['session_id'][:14]}… ({summary['n_actions']} actions)")

    reserve_before = escrow.get_promise(scope_pid)[4]      # a slash drains the promise's reserve by one payout
    cid = escrow.challenge(challenger, summary["session_id"], scope_pid, C.CHALLENGE_BOND)
    res = process_challenge(escrow, HttpStore(C.STORE_URL), cid, verifier)
    reserve_after = escrow.get_promise(scope_pid)[4]

    print("verdict:", res)
    print(f"reserve {reserve_before} -> {reserve_after}  (delta {reserve_before - reserve_after}, payout {C.PAYOUT})")
    ok = (res["violated"] and res["predicate"] == "action_within_declared_scope"
          and reserve_before - reserve_after == C.PAYOUT)
    print("\nON-CHAIN", "OK ✓ — openclaw out-of-scope write adjudicated and slashed the promise reserve by the payout"
          if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
