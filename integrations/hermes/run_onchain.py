"""Full lifecycle SOURCED FROM HERMES, on a local chain.

A Hermes write_file OUTSIDE the declared scope -> AAP-2 violation -> on-chain challenge ->
verifier re-runs the predicate over the Hermes-sourced trace -> escrow slashes the promise reserve.
Reuses the prototype's chain stack (_config, EscrowClient, aa_verifier).

Prereqs (local): anvil running; the store running. Run with the hermes venv.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.environ.get("AA_PROTO", os.path.dirname(os.path.dirname(HERE)))    # <repo>/integrations/hermes -> <repo>
# default: Hermes cloned as a sibling of the repo; override with HERMES_ROOT
HERMES_ROOT = os.environ.get("HERMES_ROOT", os.path.join(os.path.dirname(PROTO), "hermes-agent"))
sys.path.insert(0, HERMES_ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PROTO, "scripts"))

import requests

import _config as C
from aa_sdk import HttpStore
from aa_sdk.chain import EscrowClient
from aa_verifier import process_challenge
import aa_hermes
from model_tools import handle_function_call
from hermes_cli.plugins import get_plugin_manager


def main() -> int:
    for _ in range(40):
        try:
            if requests.get(C.STORE_URL + "/health", timeout=1).ok:
                break
        except Exception:
            time.sleep(0.25)
    else:
        sys.exit("store not reachable — run the FastAPI store first")

    w3 = C.w3()
    assert w3.is_connected(), "anvil not reachable"
    ws = C.actors(w3)
    deployer, provider, challenger, verifier = (ws["deployer"][0], ws["provider"][0],
                                                ws["challenger"][0], ws["verifier"][0])
    escrow = EscrowClient.deploy(w3, C.ARTIFACT, deployer=deployer, verifier=verifier,
                                 accounts=C.accounts_map(ws))

    acc, sess = aa_hermes.begin_session(party=challenger, store=HttpStore(C.STORE_URL), chain=escrow,
                                        provider_addr=provider, bond_wei=C.BOND, payout_wei=C.PAYOUT)
    scope_pid = acc.promises[0].promise_id
    get_plugin_manager()._middleware.setdefault("tool_dispatch", []).append(
        aa_hermes.on_tool_execution_middleware)

    work = os.path.join(os.path.expanduser("~"), "aa_hermes_demo")
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(os.path.join(work, "workspace"))
    os.chdir(work)

    print("driving Hermes write_file OUTSIDE workspace/ through the guarded seam ...")
    r = handle_function_call("write_file", {"path": "outside.txt", "content": "leaked report\n"})
    print("  result:", str(r)[:70])
    summary = aa_hermes.end_session()      # flushes, reads the store's records back, commits THEIR hash on-chain
    print(f"committed session {summary['session_id'][:14]}… ({summary['n_actions']} actions)")

    reserve_before = escrow.get_promise(scope_pid)[4]      # a slash drains the promise's reserve by one payout
    cid = escrow.challenge(challenger, summary["session_id"], scope_pid, C.CHALLENGE_BOND)
    res = process_challenge(escrow, HttpStore(C.STORE_URL), cid, verifier)
    reserve_after = escrow.get_promise(scope_pid)[4]

    print("verdict:", res)
    print(f"reserve {reserve_before} -> {reserve_after}  (delta {reserve_before - reserve_after}, payout {C.PAYOUT})")
    ok = (res["violated"] and res["predicate"] == "action_within_declared_scope"
          and reserve_before - reserve_after == C.PAYOUT)
    print("\nON-CHAIN", "OK ✓ — Hermes out-of-scope write adjudicated and slashed the promise reserve by the payout"
          if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
