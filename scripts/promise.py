"""Promise lifecycle: inspect and manage the reserve.

    python scripts/promise.py status   [promise_id]      reserve, windows, in-force — public reads
    python scripts/promise.py fund     [promise_id] WEI  top up the reserve (cures a lapse)
    python scripts/promise.py retire   [promise_id]      close new coverage, start the cooldown
    python scripts/promise.py withdraw [promise_id]      reclaim the reserve after the cooldown

With no promise_id, acts on every promise in deployment.json. A promise is in force while it is
not retired and its reserve covers at least one payout; a slash drains the reserve and the lapse
is visible here the moment it happens.
"""
import sys
import time

import _config as C


def _status(escrow, pid: int) -> None:
    provider, _ph, _qh, payout, reserve, registered_at, retired_at = escrow.get_promise(pid)
    lapsed = reserve < payout
    state = "RETIRED" if retired_at else ("LAPSED" if lapsed else "in force")
    print(f"promise {pid}: {state}  payout {payout} wei  reserve {reserve} wei "
          f"({reserve // payout if payout else 0} payouts)  open challenges {escrow.contract.functions.openChallenges(pid).call()}")
    if retired_at:
        unlock = retired_at + escrow.contract.functions.CHALLENGE_WINDOW().call()
        left = unlock - int(time.time())
        print(f"  retired; reserve unlocks {'now' if left <= 0 else f'in {left // 3600}h'}")


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    assert action in ("status", "fund", "retire", "withdraw"), f"unknown action {action!r}"
    w3 = C.w3()
    ws = C.actors(w3)
    escrow = C.escrow_client(w3, ws)
    prov_addr, _ = ws["provider"]
    d = C.load_deployment()
    pids = [int(sys.argv[2])] if len(sys.argv) > 2 else sorted(d["promises"].values())

    for pid in pids:
        if action == "status":
            _status(escrow, pid)
        elif action == "fund":
            amount = int(sys.argv[3]) if len(sys.argv) > 3 else C.PAYOUT
            escrow.fund_promise(prov_addr, pid, amount)
            print(f"funded promise {pid} with {amount} wei")
            _status(escrow, pid)
        elif action == "retire":
            escrow.retire_promise(prov_addr, pid)
            print(f"retired promise {pid} — new sessions uncovered; claims stay open one challenge window")
            _status(escrow, pid)
        else:
            escrow.withdraw_reserve(prov_addr, pid)
            print(f"withdrew remaining reserve of promise {pid}")
    print(f"  {C.addr_url(escrow.contract.address)}")


if __name__ == "__main__":
    main()
