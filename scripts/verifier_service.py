"""Always-on verifier service.

Polls the chain for `Challenged` events and adjudicates each: fetch the session's records from
the store, check them and the params against the on-chain commitments, run the aa_commons predicate
the on-chain predicateHash names, then submit the verdict. Run this alongside the other actors so
the PI watches it fire live.

Robustness (from the review):
  • get_logs is clamped to <=2000 blocks and lagged CONFIRMATIONS behind head; the inverted range
    in steady state is guarded (it errors, not returns []).
  • idempotency is the chain's job — process, THEN advance the checkpoint. On a transient RPC error
    we don't advance and retry the window; re-processing settled challenges just hits an advance-safe
    revert and is skipped.
  • store vs chain disagreement (unreachable, malformed, or mismatching records/params) is the one
    "committed data not produced" case inside process_challenge: no verdict while the provider is
    silent (claimDefault is the remedy), a violation once the provider has responded on chain. It
    never raises, so a broken store payload cannot wedge the polling window.
"""
import logging
import json
import os
import time
from pathlib import Path

from web3 import Web3

import _config as C
from aa_sdk import HttpStore
from aa_verifier import process_challenge

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-5s  %(message)s")
log = logging.getLogger("verifier")

POLL_SECONDS = 6
CONFIRMATIONS = 0 if C.LOCAL else 2   # anvil has no reorgs and doesn't mine empty blocks
CHECKPOINT = None  # selected after loading the chain-specific deployment
# advance-safe reverts: truly settled (including legacy "pair already slashed"), and the lapse race
# ("reserve insufficient") — the challenger's exit is withdrawChallenge, but a fundPromise cure
# can also revive the claim, so those challenge ids go on a retry list revisited every poll.
SKIP_REVERTS = ("not open", "pair already resolved", "pair already slashed", "reserve insufficient")
RETRY_REVERTS = ("reserve insufficient",)


def _load_state(default: int):
    try:
        state = json.loads(Path(CHECKPOINT).read_text())
    except FileNotFoundError:
        return default, set()
    return int(state["block"]), {int(cid) for cid in state["retry_challenges"]}


def _save_state(block: int, retry: set) -> None:
    path = Path(CHECKPOINT)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"block": block, "retry_challenges": sorted(retry)}) + "\n")
    temporary.replace(path)


def main() -> None:
    global CHECKPOINT
    w3 = C.w3()
    d = C.load_deployment()
    ws = C.verifier_actors(w3, d)
    escrow = C.escrow_client(w3, ws)
    store = HttpStore(C.STORE_URL)
    ver_addr, _ = ws["verifier"]
    CHECKPOINT = C.verifier_cursor_path(d)

    last, retry = _load_state(d["deploy_block"] - 1)
    log.info("verifier %s watching %s from block %d (store %s)",
             ver_addr, escrow.contract.address, last + 1, C.STORE_URL)

    while True:
        try:
            latest = w3.eth.block_number
        except Exception as e:
            log.warning("rpc block_number failed: %s", e)
            time.sleep(POLL_SECONDS)
            continue

        from_block = last + 1
        to_block = min(latest - CONFIRMATIONS, from_block + 1999)   # 2000-cap + confirmation lag
        if from_block > to_block:                                   # steady-state idle (errors, not [])
            time.sleep(POLL_SECONDS)
            continue

        try:
            events = escrow.contract.events.Challenged().get_logs(from_block=from_block, to_block=to_block)
            # a Responded event is the "now adjudicate" signal for a challenge first seen while its
            # trace was unavailable — without it, an awaiting challenge would never be revisited
            responded = escrow.contract.events.Responded().get_logs(from_block=from_block, to_block=to_block)
        except Exception as e:
            log.warning("get_logs failed (retry): %s", e)
            time.sleep(POLL_SECONDS)
            continue

        work = [(ev["args"]["challengeId"], kind, ev) for kind, evs in
                (("Challenged", events), ("Responded", responded)) for ev in evs]
        work += [(cid, "Retry", None) for cid in sorted(retry)]
        advance = True
        for cid, kind, ev in work:
            if kind == "Challenged":
                log.info("Challenged id=%s session=%s… promise=%s challenger=%s", cid,
                         Web3.to_hex(ev["args"]["sessionId"])[:14], ev["args"]["promiseId"], ev["args"]["challenger"])
            elif kind == "Responded":
                log.info("Responded id=%s — provider claims the trace is available; re-adjudicating", cid)
            try:
                result = process_challenge(escrow, store, cid, ver_addr)
                retry.discard(cid)
                if result["violated"] is None:
                    # store rule: nothing to adjudicate yet — the default path is on-chain
                    log.info("  -> no verdict: %s (claimDefault after the response window)", result["reason"])
                else:
                    log.info("  -> verdict violated=%s seq=%s reason=%r paid=%s wei",
                             result["violated"], result.get("seq"), result["reason"], result["paid_to_challenger"])
            except Exception as e:
                msg = str(e)
                if any(s in msg for s in RETRY_REVERTS):
                    if cid not in retry:
                        log.info("  -> challenge %s blocked (%s) — on the retry list until cured or exited",
                                 cid, msg[:50])
                    retry.add(cid)
                elif any(s in msg for s in SKIP_REVERTS):
                    retry.discard(cid)
                    log.info("  -> challenge %s already settled (%s) — skip", cid, msg[:50])
                else:
                    log.warning("  -> transient error on challenge %s, retrying window: %s", cid, msg[:120])
                    advance = False
                    break

        if advance:
            last = to_block
            _save_state(last, retry)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
