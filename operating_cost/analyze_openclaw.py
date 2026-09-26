"""Validate and summarize saved OpenClaw cost measurements, with optional receipt refresh."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys

sys.path.insert(0, str(Path(__file__).parent))
import measure_openclaw as M
from aa_commons import ActionRecord, hash_obj, trace_hash
from aa_commons.trace import check_prefixes
from web3 import Web3


def number(value):
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


def stats(values):
    return {"n": len(values), "mean": statistics.mean(values), "min": min(values), "max": max(values),
            "median": statistics.median(values), "stddev": statistics.stdev(values) if len(values) > 1 else 0}


def paired_ci(values, seed=20260924):
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(10000))
    return {"mean": statistics.mean(values), "bootstrap_95_ci": [means[249], means[9749]], "pairs": len(values)}


def validate_job(row):
    assert row["payload_bytes"] == 1024 and row["total_written_bytes"] == 1024 * row["writes"]
    assert row["approvals_requested"] == row["approvals_resolved"] == row["writes"]
    assert not row["warnings"]
    assert abs(row["elapsed_ms"] - row["tool_loop_ms"] - row["session_end_ms"]) < 1
    for f in row["files"]:
        data = Path(f["path"]).read_bytes()
        assert len(data) == 1024 and hashlib.sha256(data).hexdigest() == f["sha256"] == row["payload_sha256"]
    if not row["enabled"]:
        assert row["native_observations"] == 0
        return
    assert row["native_observations"] == 4 * row["writes"]
    snap = row["helper_snapshot"]
    records = snap["records"]
    assert len(records) == 3 * row["writes"]
    parsed = [ActionRecord.from_dict(r) for r in records]
    assert trace_hash(parsed) == snap["trace_hash"]
    check_prefixes(parsed, snap["checkpoints"], snap["session_id"])
    assert all(not v["violated"] for v in snap["verdicts"].values())
    if row["mode"] == "http_store_anvil":
        assert snap["chain_state"][2] == snap["trace_hash"] and snap["chain_state"][4] > 0
    for index in range(row["writes"]):
        requested, authorized, write = records[3 * index:3 * index + 3]
        assert [r["seq"] for r in [requested, authorized, write]] == list(range(3 * index + 1, 3 * index + 4))
        assert [r["tool"] for r in [requested, authorized, write]] == ["authorization_requested", "user_authorization", "write"]
        assert len({r["metadata"]["action_id"] for r in [requested, authorized, write]}) == 1
        assert requested["args"]["action_id"] == authorized["args"]["action_id"] == write["metadata"]["action_id"]
        assert requested["args"]["request_id"] == authorized["args"]["request_id"]
        assert requested["args"]["args"] == write["args"]
        assert authorized["args"]["args_hash"] == hash_obj(write["args"])
        assert authorized["args"]["decision"] == "allow" and authorized["args"]["scope"] == "once"
        assert authorized["args"]["authority"] == "human"
        assert write["args"]["path"] == row["actions"][index]["path"]
        assert write["args"]["content"] == "x" * 1024


def local(directory):
    raw = json.loads((directory / "native-results.json").read_text())
    for row in raw["jobs"]:
        validate_job(row)
    rows = [r for r in raw["jobs"] if not r["warmup"]]
    assert len(rows) == 90 and len(raw["jobs"]) == 93
    baseline = {r["repetition"]: r for r in rows if r["mode"] == "capture_off"}
    assert set(baseline) == set(range(30))
    summary = {"measured_sessions": 90, "warmup_sessions": 3, "writes_per_session": 100,
               "stored_records_per_enabled_session": 300, "modes": {}}
    for mode in ["capture_off", "http_store", "http_store_anvil"]:
        subset = [r for r in rows if r["mode"] == mode]
        assert len(subset) == 30 and {r["repetition"] for r in subset} == set(range(30))
        values = {key.replace("_ms", "_seconds"): stats([r[key] / 1000 for r in subset])
                  for key in ["elapsed_ms", "tool_loop_ms", "session_end_ms", "session_open_ms_excluded"]}
        if mode != "capture_off":
            differences = [(r["elapsed_ms"] - baseline[r["repetition"]]["elapsed_ms"]) / 1000 for r in subset]
            values["added_seconds_vs_recording_off"] = paired_ci(differences)
            base_mean = statistics.mean(r["elapsed_ms"] / 1000 for r in baseline.values())
            values["added_percent_vs_recording_off"] = 100 * statistics.mean(differences) / base_mean
            values["amortized_added_ms_per_write"] = statistics.mean(differences) * 1000 / 100
        summary["modes"][mode] = values
    store = {r["repetition"]: r for r in rows if r["mode"] == "http_store"}
    summary["extra_commitment_seconds"] = paired_ci([(r["elapsed_ms"] - store[r["repetition"]]["elapsed_ms"]) / 1000
                                      for r in rows if r["mode"] == "http_store_anvil"])
    M.dump(directory / "summary.json", summary)
    return summary


def public(directory, refresh=False):
    if json.loads((directory / "manifest.json").read_text())["status"] != "completed":
        raise ValueError("public run is not completed")
    raw = json.loads((directory / "native-results.json").read_text())
    journal = json.loads((directory / "chain-journal.json").read_text())
    rows = raw["jobs"]
    assert len(rows) == 9
    signed = journal["signed_transactions"]
    saved = {tx["receipt"]["transactionHash"].lower(): tx for tx in journal["transactions"]}
    assert len(saved) == len(signed) and set(saved) == {s["hash"].lower() for s in signed}
    receipt_file = directory / "canonical-receipts.json"
    if refresh:
        w3 = M.mainnet()
        receipts = {}
        for tx in signed:
            receipt = w3.eth.get_transaction_receipt(tx["hash"])
            assert receipt.status == 1 and int.from_bytes(receipt.blockHash, "big") != 0
            original = saved[tx["hash"].lower()]["receipt"]
            assert Web3.to_hex(receipt.transactionHash).lower() == tx["hash"].lower()
            assert receipt["from"].lower() == journal["provider"].lower()
            assert receipt["to"].lower() == journal["contract_address"].lower()
            assert receipt.blockNumber == number(original["blockNumber"])
            # Base's first receipt can be a Flashblock preconfirmation with a
            # zero block hash. Preserve that original, and use mined receipts
            # for fees; do not turn its observation time into block finality.
            if number(original["blockHash"]) != 0:
                assert Web3.to_hex(receipt.blockHash).lower() == original["blockHash"].lower()
            receipts[tx["hash"].lower()] = M.jsonable(receipt)
        balances = {"provider_after_wei": w3.eth.get_balance(journal["provider"]),
                    "funding_after_wei": w3.eth.get_balance(M.public_metadata()[0]["funding_address"])}
        M.dump(receipt_file, {"checked_at_utc": M.utc(), "receipts": receipts, "balances": balances})
    canonical = json.loads(receipt_file.read_text())
    receipts = canonical["receipts"]
    assert set(receipts) == set(saved)
    def fee(tx):
        receipt = receipts[tx["receipt"]["transactionHash"].lower()]
        assert "l1Fee" in receipt, "missing Base L1 data fee"
        return number(receipt["gasUsed"]) * number(receipt["effectiveGasPrice"]) + number(receipt["l1Fee"])
    all_fees = sum(fee(tx) for tx in journal["transactions"])
    values = sum(tx["value_wei"] for tx in journal["transactions"])
    assert journal["provider_balance_before_wei"] - canonical["balances"]["provider_after_wei"] == all_fees + values
    results = []
    for row in rows:
        validate_job(row)
        assert row["writes"] == 20 and row["helper_timeout_ms"] == 120000
        snap = row["helper_snapshot"]
        commits = [tx for tx in snap["transactions"] if tx["operation"] in {"checkpointTrace", "commitTrace"}]
        assert sum(t["operation"] == "commitTrace" for t in commits) == 1
        waits = []
        for record in snap["records"]:
            seq, created = record["seq"], record["ts"] / 1000
            covers = [t["observed_at_unix"] for t in commits if t["operation"] == "commitTrace" or t["record_count"] >= int(seq)]
            delay = min(covers) - created
            assert delay >= 0
            waits.append(delay)
        assert len(waits) == 60
        if row["strategy"] == "final_only":
            assert not snap["checkpoints"]
        results.append({"session_id": snap["session_id"], "strategy": row["strategy"], "repetition": row["repetition"],
                        "records": len(waits), "checkpoint_count": len(snap["checkpoints"]),
                        "commitment_fee_wei": sum(fee(t) for t in commits),
                        "record_wait_seconds": stats(waits), "record_waits_seconds": waits,
                        "action_plus_finalization_seconds": row["elapsed_ms"] / 1000})
    summary = {"sessions": results, "policies": {}, "all_transaction_count": len(signed),
               "all_transaction_fee_wei": all_fees, "reserve_deposits_wei": values,
               "operation_counts": dict(Counter(t["operation"] for t in journal["transactions"])),
               "balances": canonical["balances"],
               "fee_definition": "gasUsed times effectiveGasPrice plus canonical Base receipt l1Fee; policy fees exclude registration and session opening",
               "first_observed_zero_block_hash_receipts": sum(number(t["receipt"]["blockHash"]) == 0 for t in journal["transactions"]),
               "wait_definition": "record creation until client first observes a receipt covering that record; includes Base preconfirmations, not canonical block confirmation or L1 finality"}
    for policy in M.STRATEGIES:
        group = [r for r in results if r["strategy"] == policy]
        assert len(group) == 3 and {r["repetition"] for r in group} == {0, 1, 2}
        summary["policies"][policy] = {"sessions": 3, "writes_per_session": 20, "records_per_session": 60,
            "mean_commitment_fee_microeth": statistics.mean(r["commitment_fee_wei"] for r in group) / 10**12,
            "min_commitment_fee_microeth": min(r["commitment_fee_wei"] for r in group) / 10**12,
            "max_commitment_fee_microeth": max(r["commitment_fee_wei"] for r in group) / 10**12,
            "mean_record_wait_seconds": statistics.mean(r["record_wait_seconds"]["mean"] for r in group),
            "max_record_wait_seconds": max(r["record_wait_seconds"]["max"] for r in group),
            "checkpoint_counts": [r["checkpoint_count"] for r in group]}
    M.dump(directory / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["local", "public"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = local(args.directory) if args.kind == "local" else public(args.directory, args.refresh)
    print(json.dumps({k: v for k, v in result.items() if k != "sessions"}, indent=2))
