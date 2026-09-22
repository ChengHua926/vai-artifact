"""Observability dashboard: the on-chain state (a local "explorer" that replays the escrow's
events + reads its mappings) and the SQL store contents, side by side.

Run anytime: `.venv/bin/python scripts/observe.py`
"""
import json
import os
import sqlite3

from web3 import Web3

import _config as C
from aa_commons import ActionRecord, trace_hash

STATUS = {0: "None", 1: "Open", 2: "Valid", 3: "Invalid", 4: "Withdrawn"}
STORE_DB = os.environ.get("STORE_DB", os.path.join(C.REPO, "packages", "store", "trace_store.db"))
EVENTS = ("PromiseRegistered", "PromiseFunded", "PromiseRetired", "SessionOpened", "TraceCommitted",
          "Challenged", "Responded", "Verdict", "DefaultClaimed", "ChallengeWithdrawn")


def section(title):
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def show_chain():
    section("1) ON-CHAIN  (escrow state + event log)")
    w3 = C.w3()
    if not w3.is_connected():
        print(f"  chain not reachable at {C.RPC_URL}"); return
    d = C.load_deployment()
    ws = C.actors(w3)
    escrow = C.escrow_client(w3, ws)
    c = escrow.contract
    prov = d["provider_addr"]
    print(f"  escrow {c.address}")
    print(f"  provider forfeited-bond credit = {escrow.bond_of(prov)} wei")
    for key, pid in d.get("promises", {}).items():
        _prov, _ph, _qh, payout, reserve, _registered_at, retired_at = escrow.get_promise(pid)
        print(f"  promise[{key}] id={pid}  payout={payout} wei  reserve={reserve} wei"
              f"{'  RETIRED' if retired_at else ''}{'  LAPSED' if reserve < payout else ''}")

    frm, to = d["deploy_block"], w3.eth.block_number
    print(f"  events {frm}..{to}:")
    for name in EVENTS:
        for ev in getattr(c.events, name)().get_logs(from_block=frm, to_block=to):
            a = dict(ev["args"])
            a = {k: (Web3.to_hex(v)[:14] + "…" if isinstance(v, (bytes, bytearray)) else v) for k, v in a.items()}
            print(f"    {name:18s} {a}")
    nxt = c.functions.nextChallengeId().call()
    for cid in range(1, nxt):
        sid, pid, who, bond, st, _challenged_at, responded_at = escrow.get_challenge(cid)
        print(f"  challenge #{cid}: session={Web3.to_hex(sid)[:14]}… promise={pid} status={STATUS.get(st, st)} "
              f"challenger={who} responded={'yes' if responded_at else 'no'}")


def show_store():
    section("2) STORE  (SQLite — promises + the per-action records the SDK streamed; those records ARE the trace the verifier re-hashes vs chain)")
    if not os.path.exists(STORE_DB):
        print(f"  no store db at {STORE_DB} (start the store / run a session)"); return
    db = sqlite3.connect(STORE_DB)
    print("  promises:")
    for pid, payload in db.execute("SELECT promise_id, payload FROM promises"):
        r = json.loads(payload)
        print(f"    [{pid}] {r['predicate']}  params={r['params']}")
    print("  sessions (streamed records):")
    by_sid: dict = {}
    for sid, _seq, payload in db.execute("SELECT session_id, seq, payload FROM records ORDER BY session_id, seq"):
        by_sid.setdefault(sid, []).append(json.loads(payload))
    for sid, recs in by_sid.items():
        h = trace_hash([ActionRecord.from_dict(r) for r in recs])
        print(f"    {sid[:16]}…  actions={len(recs)}  traceHash={h[:14]}…  (what end() committed, if the chain agrees)")
        for rec in recs:
            print(f"        [{rec['seq']}] {rec['tool']}({rec['args']}) -> {rec['result']}")
    db.close()


def main():
    show_chain()
    show_store()


if __name__ == "__main__":
    main()
