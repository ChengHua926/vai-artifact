"""Print every operating-cost number of Section 5.3 and its appendix from the recorded results.

Usage: python practicality/analyze.py
Standard library only; reads practicality/results/ and writes nothing. Prints each number next to
the value printed in the paper and exits 1 if any differs.

Inputs (V2 runs of 2026-09-25, one prototype revision):
  settlement-mainnet-01/          14 challenges on Base mainnet (run_settlement.py): summary.json and its
                                  84 transactions (hash, role, operation, case, canonical receipt)
  checkpoint-mainnet-{hermes,openclaw}-01/
                                  checkpoint-policy matrix on Base mainnet (checkpoint_refresh.py): summary.json,
                                  canonical-receipts.json, and chain-journal.json / native-results.json reduced
                                  to the fields read here (operation, session, transaction hash; record timestamps)
  recording-{hermes,openclaw}-01/ recording overhead, 100 writes x 30 paired repetitions (measure_recording_refresh.py):
                                  per-run rows (runtime-raw.jsonl / native-results.json) and the script's summary
  verifier-handoff-01/            signing and delivery of a 10,000-record trace (measure_verifier_handoff.py)
  verifier-prefix-comparison-01/  verifier processing time and memory (measure_verifier_handoff.py --mode compare-prefixes)

Definitions follow the paper: fees = canonical L2 execution + L1 data fee; record wait = record
timestamp to the first observed receipt of the first commitment covering it; USD at the paper's
reference price of 2,682.79 USD/ETH; MiB = 2**20 bytes. Recording and verifier numbers are recomputed
from the per-run rows with the run scripts' own statistics (seeded bootstrap) and checked against the
summaries those scripts wrote.
"""
import json
import random
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
USD_PER_ETH = 2682.79
POLICIES = ["10_records_or_30s", "30_records_or_60s", "final_only"]
LABELS = {"10_records_or_30s": "Every 10 records or 30 s (default)", "30_records_or_60s": "Every 30 records or 60 s",
          "final_only": "Final commitment only"}
OPS = [("registerPromise", "Register promise"), ("openSession", "Open session"),
       ("checkpointTrace", "Submit checkpoint"), ("commitTrace", "Commit final trace"),
       ("challenge", "File challenge"), ("submitVerdict", "Submit verdict")]


# --- Mainnet settlement and checkpoint matrix (same computation that produced the paper's numbers) ---

def load(path):
    return json.loads(Path(path).read_text())


def as_int(value):
    if value is None:
        return 0
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def fee_wei(receipt):
    """Canonical Base fee: L2 execution plus the L1 data fee (and operator fee if present)."""
    l2 = as_int(receipt["gasUsed"]) * as_int(receipt["effectiveGasPrice"])
    operator = 0
    if receipt.get("operatorFeeScalar") or receipt.get("operatorFeeConstant"):
        operator = (as_int(receipt["gasUsed"]) * as_int(receipt.get("operatorFeeScalar")) // 10**6
                    + as_int(receipt.get("operatorFeeConstant")))
    return l2 + as_int(receipt.get("l1Fee")) + operator


def checkpoint_ops(directory):
    """Per-operation canonical fees from a checkpoint run's journal and canonical receipts."""
    journal = load(Path(directory) / "chain-journal.json")
    receipts = load(Path(directory) / "canonical-receipts.json")["receipts"]
    out = {}
    for tx in journal["transactions"]:
        key = tx["receipt"]["transactionHash"].lower()
        out.setdefault(tx["operation"], []).append(fee_wei(receipts[key]))
    return out


def settlement_ops(summary):
    return {op: (v["transactions"], v["total_fee_wei"]) for op, v in summary["operations"].items()}


def pending_counts(directory, harness, summary):
    """Maximum number of records created but not yet covered by a first commitment receipt."""
    native = load(Path(directory) / "native-results.json")
    rows = (native["checkpoint_runs"] if harness == "hermes"
            else [{**r["helper_snapshot"], "strategy": r["strategy"], "repetition": r["repetition"]}
                  for r in native["jobs"]])
    waits = {s["session_id"]: s["record_waits_seconds"] for s in summary["sessions"]}
    per_policy = {}
    for row in rows:
        ts = [r["ts"] / 1000 for r in row["records"]]
        cover = [t + w for t, w in zip(ts, waits[row["session_id"]])]
        peak = max(sum(1 for tj, cj in zip(ts, cover) if tj <= t < cj) for t in ts)
        per_policy.setdefault(row["strategy"], []).append(peak)
    return per_policy


def checkpoint_numbers(directory, harness):
    summary = load(Path(directory) / "summary.json")
    result = {"transactions": summary["all_transaction_count"],
              "fee_microeth": summary["all_transaction_fee_wei"] / 1e12,
              "reserve_deposits_wei": summary.get("reserve_deposits_wei"),
              "first_receipts_with_zero_block_hash": summary.get("first_receipts_with_zero_block_hash"),
              "policies": {}}
    pending = pending_counts(directory, harness, summary)
    for policy in POLICIES:
        p = summary["policies"][policy]
        runs = sorted(p["per_run"], key=lambda r: r["repetition"])
        sessions = [s for s in summary["sessions"] if s["strategy"] == policy]
        entry = {
            "mean_fee_microeth": p["mean_commitment_fee_microeth"],
            "mean_fee_usd": p["mean_commitment_fee_microeth"] * 1e-6 * USD_PER_ETH,
            "mean_wait_s": p["mean_record_wait_seconds"],
            "per_run_mean_wait_s": [r["mean_record_wait_seconds"] for r in runs],
            "per_run_max_wait_s": [r["max_record_wait_seconds"] for r in runs],
            "mean_of_per_run_max_wait_s": p["mean_of_per_run_max_record_wait_seconds"],
            "overall_max_wait_s": p["max_record_wait_seconds"],
            "checkpoint_counts": p["checkpoint_counts"],
            "per_run_fee_microeth": [r["commitment_fee_wei"] / 1e12 for r in runs],
            "max_pending_records": pending.get(policy),
        }
        if harness == "hermes":
            first = [w for s in sessions for w in s["record_waits_seconds"][:8]]
            rest = [w for s in sessions for w in s["record_waits_seconds"][8:]]
            entry["first_8_mean_wait_s"] = statistics.mean(first)
            entry["remaining_mean_wait_s"] = statistics.mean(rest)
            entry["remaining_count_per_session"] = len(sessions[0]["record_waits_seconds"]) - 8
        result["policies"][policy] = entry
    return result, summary


def mainnet_numbers(settlement_dir, hermes_dir, openclaw_dir):
    settlement = load(settlement_dir / "summary.json")
    hermes, _ = checkpoint_numbers(hermes_dir, "hermes")
    openclaw, _ = checkpoint_numbers(openclaw_dir, "openclaw")

    # Operation fee table: the Hermes run = settlement matrix + Hermes checkpoint matrix.
    fees = {}
    for op, (count, total) in settlement_ops(settlement).items():
        fees.setdefault(op, []).append((count, total))
    for op, values in checkpoint_ops(hermes_dir).items():
        fees.setdefault(op, []).append((len(values), sum(values)))
    table = []
    for op, label in OPS:
        count = sum(c for c, _ in fees.get(op, []))
        total = sum(t for _, t in fees.get(op, []))
        table.append({"operation": label, "transactions": count,
                      "mean_fee_microeth": (total / count / 1e12) if count else None})
    deploy = fees.get("deploy", [(0, 0)])
    hermes_run_tx = sum(r["transactions"] for r in table)
    hermes_run_fee = sum((r["mean_fee_microeth"] or 0) * r["transactions"] for r in table)
    return {
        "settlement": {
            "status": settlement["status"], "contract": settlement["contract_address"],
            "revision": settlement.get("source_revision"), "source_dirty": settlement.get("source_dirty"),
            "outcomes": settlement["outcome_table"],
            "failed_final_checks": [k for k, v in settlement["final_checks"].items() if v is not True],
            "transactions": settlement["all_transaction_count"],
            "fee_microeth": settlement["all_transaction_fee_wei"] / 1e12,
            "deploy_fee_microeth": sum(t for _, t in deploy) / 1e12,
            "fees_by_role_microeth": {k: v / 1e12 for k, v in settlement["fees_by_role_wei"].items()},
        },
        "checkpoint": {"hermes": hermes, "openclaw": openclaw},
        "operation_fee_table_hermes_run": table,
        "hermes_run_excluding_deploy": {"transactions": hermes_run_tx, "fee_microeth": hermes_run_fee},
        "openclaw_run": {"transactions": openclaw["transactions"], "fee_microeth": openclaw["fee_microeth"]},
        "grand_total": {
            "transactions": settlement["all_transaction_count"] + hermes["transactions"] + openclaw["transactions"],
            "fee_microeth": settlement["all_transaction_fee_wei"] / 1e12 + hermes["fee_microeth"] + openclaw["fee_microeth"],
        },
    }


# --- Recording and verifier numbers from the per-run rows (same statistics as the run scripts) ----

def percentile(values, q):
    values = sorted(values)
    at = (len(values) - 1) * q
    lo = int(at)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (at - lo)


def paired_bootstrap(values, seed=20260916, samples=10000):
    """measure_recording_refresh.py (Hermes)."""
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(samples)]
    return {"mean": statistics.mean(values), "ci95": [percentile(means, .025), percentile(means, .975)]}


def paired_ci(values, seed=20260924):
    """analyze_openclaw.py (OpenClaw)."""
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(10000))
    return {"mean": statistics.mean(values), "bootstrap_95_ci": [means[249], means[9749]], "pairs": len(values)}


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


MODES = ["capture_off", "http_store", "http_store_anvil"]


def hermes_recording(directory):
    """Mean time of 100 writes per configuration and paired added time vs. recording disabled."""
    rows = [r for r in jsonl(directory / "runtime-raw.jsonl") if not r["warmup"]]
    key = "total_action_plus_finalization_seconds"
    baseline = {r["repetition"]: r for r in rows if r["mode"] == "capture_off"}
    out = {mode: {"n": sum(r["mode"] == mode for r in rows),
                  "mean": statistics.mean(r[key] for r in rows if r["mode"] == mode)} for mode in MODES}
    for mode in MODES[1:]:
        out[mode]["added"] = paired_bootstrap([r[key] - baseline[r["repetition"]][key] for r in rows if r["mode"] == mode])
    summary = load(directory / "runtime-summary.json")
    agrees = all(summary[m][key]["mean"] == out[m]["mean"] and summary[m][key]["n"] == out[m]["n"] for m in MODES) and all(
        summary["paired_differences_vs_capture_off"][m][key] == out[m]["added"] for m in MODES[1:])
    return out, agrees


def openclaw_recording(directory):
    rows = [r for r in load(directory / "native-results.json")["jobs"] if not r["warmup"]]
    baseline = {r["repetition"]: r for r in rows if r["mode"] == "capture_off"}
    out = {}
    for mode in MODES:
        subset = [r for r in rows if r["mode"] == mode]
        out[mode] = {"n": len(subset), "mean": statistics.mean(r["elapsed_ms"] / 1000 for r in subset)}
        if mode != "capture_off":
            out[mode]["added"] = paired_ci([(r["elapsed_ms"] - baseline[r["repetition"]]["elapsed_ms"]) / 1000
                                            for r in subset])
    summary = load(directory / "summary.json")["modes"]
    agrees = all(summary[m]["elapsed_seconds"]["mean"] == out[m]["mean"] for m in MODES) and all(
        summary[m]["added_seconds_vs_recording_off"] == out[m]["added"] for m in MODES[1:])
    return out, agrees


def cell_means(rows, fields, **match):
    subset = [r for r in rows if all(r[k] == v for k, v in match.items())]
    return {"n": len(subset), **{field: statistics.mean(r[field] for r in subset) for field in fields}}


# --- Formatting and checking ---------------------------------------------------------------------

rows = []   # (label, computed, paper value or None)


def check(label, computed, paper=None):
    rows.append((label, computed, paper))


def f(value, digits):
    return f"{value:.{digits}f}"


def small(value, digits=4):
    text = f(value, digits)
    return f"<{10 ** -digits:.{digits}f}" if float(text) == 0 else text


def mib(value):
    return f"{value / 2**20:,.1f}"


def ci(entry, digits):
    lo, hi = entry
    return f"[{f(lo, digits)}, {f(hi, digits)}]"


def receipts_match(directory):
    """Recompute each session's commitment fee and the run's total fee from the canonical receipts."""
    summary = load(directory / "summary.json")
    journal = load(directory / "chain-journal.json")
    receipts = load(directory / "canonical-receipts.json")["receipts"]
    per_session, total = {}, 0
    for tx in journal["transactions"]:
        fee = fee_wei(receipts[tx["receipt"]["transactionHash"].lower()])
        total += fee
        if tx["operation"] in ("checkpointTrace", "commitTrace"):
            per_session[tx["session_id"]] = per_session.get(tx["session_id"], 0) + fee
    return (all(per_session.get(s["session_id"]) == s["commitment_fee_wei"] for s in summary["sessions"])
            and total == summary["all_transaction_fee_wei"]
            and len(journal["transactions"]) == len(receipts) == summary["all_transaction_count"])


def settlement_receipts_match(directory):
    """Recompute the settlement's transaction count, per-operation fees and total fee from its canonical receipts."""
    summary = load(directory / "summary.json")
    transactions = load(directory / "transactions.json")
    operations = {}
    for tx in transactions:
        entry = operations.setdefault(tx["operation"], [0, 0])
        entry[0] += 1
        entry[1] += fee_wei(tx["receipt"])
    return (len(transactions) == summary["all_transaction_count"]
            and all(tx["status"] == "canonical" and as_int(tx["receipt"]["status"]) == 1 for tx in transactions)
            and operations == {op: [v["transactions"], v["total_fee_wei"]] for op, v in summary["operations"].items()}
            and sum(fee for _, fee in operations.values()) == summary["all_transaction_fee_wei"]), len(transactions)


def main():
    settlement_dir = RESULTS / "settlement-mainnet-01"
    hermes_dir, openclaw_dir = RESULTS / "checkpoint-mainnet-hermes-01", RESULTS / "checkpoint-mainnet-openclaw-01"
    n = mainnet_numbers(settlement_dir, hermes_dir, openclaw_dir)
    settlement = load(settlement_dir / "summary.json")
    h, o = n["checkpoint"]["hermes"]["policies"], n["checkpoint"]["openclaw"]["policies"]
    # Verifier processing: mean over 5 fresh processes per cell, streaming prefix check (the released verifier).
    fields = ["verification_seconds", "prefix_validation_seconds", "verifier_peak_rss_bytes"]
    paired_rows = jsonl(RESULTS / "verifier-prefix-comparison-01/paired-raw.jsonl")
    paired_summary = load(RESULTS / "verifier-prefix-comparison-01/paired-summary.json")
    stream = {}
    for key, value in paired_summary.items():
        records, cadence = key.split(":")
        stream[key] = cell_means(paired_rows, fields, records=int(records), cadence=cadence, implementation="streaming")
    paired_agrees = all(paired_summary[key]["arms"]["streaming"][field]["mean"] == stream[key][field]
                        for key in stream for field in fields)
    handoff_rows = jsonl(RESULTS / "verifier-handoff-01/handoff-raw.jsonl")
    handoff_summary = load(RESULTS / "verifier-handoff-01/handoff-summary.json")
    handoff = cell_means(handoff_rows, ["signing_seconds", "delivery_seconds"], records=10000, cadence="every_10_records")
    handoff_agrees = all(handoff_summary["10000:every_10_records"][field]["mean"] == handoff[field]
                         for field in ("signing_seconds", "delivery_seconds"))
    hermes_rec, hermes_rec_agrees = hermes_recording(RESULTS / "recording-hermes-01")
    openclaw_rec, openclaw_rec_agrees = openclaw_recording(RESULTS / "recording-openclaw-01")

    # Table 2 (tab:eval:policies): fee per session (USD), mean record wait (s), verifier time (s).
    check("Fee reference price (USD per ETH)", f"{USD_PER_ETH:,.2f}", "2,682.79")
    paper_fee = {"10_records_or_30s": ("0.00512", "0.00378"), "30_records_or_60s": ("0.00378", "0.00244"),
                 "final_only": ("0.00086", "0.00087")}
    paper_wait = {"10_records_or_30s": ("12.46", "12.27"), "30_records_or_60s": ("11.25", "10.91"),
                  "final_only": ("12.03", "11.14")}
    paper_longest = {"10_records_or_30s": ("33.9", "33.9"), "30_records_or_60s": ("41.4", "41.4"),
                     "final_only": ("46.8", "45.3")}
    for policy in POLICIES:
        for i, (name, data) in enumerate((("Hermes", h), ("OpenClaw", o))):
            check(f"Table 2, {LABELS[policy]}: fee per session (USD), {name}",
                  f(data[policy]["mean_fee_usd"], 5), paper_fee[policy][i])
        for i, (name, data) in enumerate((("Hermes", h), ("OpenClaw", o))):
            check(f"Table 2, {LABELS[policy]}: mean record wait (s), {name}",
                  f(data[policy]["mean_wait_s"], 2), paper_wait[policy][i])
    check("Table 2, verifier time (s), 10,000 records, 1,000 checkpoints",
          f(stream["10000:every_10_records"]["verification_seconds"], 3), "0.203")
    check("Table 2, verifier time (s), 10,000 records, 0 checkpoints",
          f(stream["10000:final_only"]["verification_seconds"], 3), "0.131")
    check("Table 2 fees recomputed from the canonical receipts, Hermes", "yes" if receipts_match(hermes_dir) else "no", "yes")
    check("Table 2 fees recomputed from the canonical receipts, OpenClaw", "yes" if receipts_match(openclaw_dir) else "no", "yes")

    # Longest waits: mean over the three runs of each run's maximum record wait.
    for policy in POLICIES:
        for i, (name, data) in enumerate((("Hermes", h), ("OpenClaw", o))):
            check(f"Longest wait, mean of per-run max (s), {LABELS[policy]}, {name}",
                  f(data[policy]["mean_of_per_run_max_wait_s"], 1), paper_longest[policy][i])
    ten, thirty = h["10_records_or_30s"], h["30_records_or_60s"]
    check("Hermes: ten-record policy has the shorter maximum but longer average wait than thirty-record",
          "yes" if (ten["mean_of_per_run_max_wait_s"] < thirty["mean_of_per_run_max_wait_s"]
                    and ten["mean_wait_s"] > thirty["mean_wait_s"]) else "no", "yes")

    # Recording overhead (tab:eval:recording-cost): 100 writes, 30 measured repetitions per configuration.
    check("Recording, measured repetitions per configuration, Hermes / OpenClaw",
          f"{hermes_rec['capture_off']['n']} / {openclaw_rec['capture_off']['n']}", "30 / 30")
    check("Recording disabled (s), Hermes", f(hermes_rec["capture_off"]["mean"], 2), "6.07")
    check("Recording disabled (s), OpenClaw", f(openclaw_rec["capture_off"]["mean"], 3), "0.022")
    paper_rec = {"http_store": ("6.80", "1.096", "0.73 [0.70, 0.77]", "1.074 [1.059, 1.089]"),
                 "http_store_anvil": ("7.83", "1.788", "1.76 [1.69, 1.85]", "1.765 [1.709, 1.821]")}
    names = {"http_store": "Recording and HTTP store", "http_store_anvil": "Recording, store, and local chain"}
    for mode in ("http_store", "http_store_anvil"):
        paper = paper_rec[mode]
        check(f"{names[mode]} (s), Hermes", f(hermes_rec[mode]["mean"], 2), paper[0])
        check(f"{names[mode]} (s), OpenClaw", f(openclaw_rec[mode]["mean"], 3), paper[1])
        added = hermes_rec[mode]["added"]
        check(f"{names[mode]}: added (s) [95% CI], Hermes", f"{f(added['mean'], 2)} {ci(added['ci95'], 2)}", paper[2])
        added = openclaw_rec[mode]["added"]
        check(f"{names[mode]}: added (s) [95% CI], OpenClaw",
              f"{f(added['mean'], 3)} {ci(added['bootstrap_95_ci'], 3)}", paper[3])
    check("Recording summaries written by the run scripts agree with the per-run rows",
          "yes" if hermes_rec_agrees and openclaw_rec_agrees else "no", "yes")
    per_call = [hermes_rec["http_store_anvil"]["added"]["mean"] * 1000 / 100,
                openclaw_rec["http_store_anvil"]["added"]["mean"] * 1000 / 100]
    check("Section 5.3: added time per tool call with store and chain (ms), Hermes / OpenClaw",
          f"{f(per_call[0], 1)} / {f(per_call[1], 1)}")
    check("Section 5.3: under 20 ms per tool call", "yes" if max(per_call) < 20 else "no", "yes")
    fees = [d[p]["mean_fee_usd"] for d in (h, o) for p in POLICIES]
    check("Section 5.3: under 1 cent per session on Base mainnet", "yes" if max(fees) < 0.01 else "no", "yes")

    # Verifier cost (tab:eval:verifier-cost): streaming prefix check, from the verifier's stored copy.
    paper_cost = {(100, "final_only"): ("0.0023", "<0.0001", "78.8"), (100, "every_10_records"): ("0.0030", "0.0008", "78.8"),
                  (1000, "final_only"): ("0.0132", "<0.0001", "85.0"), (1000, "every_10_records"): ("0.0201", "0.0071", "84.9"),
                  (10000, "final_only"): ("0.1307", "0.0002", "159.5"), (10000, "every_10_records"): ("0.2029", "0.0722", "159.8")}
    for (records, cadence), paper in paper_cost.items():
        arm = stream[f"{records}:{cadence}"]
        cell = f"{records:,} records, {records // 10 if cadence == 'every_10_records' else 0:,} checkpoints"
        check(f"Verifier cost, {cell}: total (s)", f(arm["verification_seconds"], 4), paper[0])
        check(f"Verifier cost, {cell}: prefix checks (s)", small(arm["prefix_validation_seconds"]), paper[1])
        check(f"Verifier cost, {cell}: peak memory (MiB)", mib(arm["verifier_peak_rss_bytes"]), paper[2])

    # Signing and delivery of a 10,000-record trace with 1,000 checkpoints, five runs.
    check("Verifier summary written by the run script agrees with the per-run rows", "yes" if paired_agrees else "no", "yes")
    check("Signing (s), 10,000 records, 1,000 checkpoints", f(handoff["signing_seconds"], 3), "0.069")
    check("Delivery (s), 10,000 records, 1,000 checkpoints", f(handoff["delivery_seconds"], 3), "0.334")
    check("Runs averaged", str(handoff["n"]), "5")
    check("Handoff summary written by the run script agrees with the per-run rows", "yes" if handoff_agrees else "no", "yes")
    sign_send = handoff["signing_seconds"] + handoff["delivery_seconds"]
    check("Table 2 caption: signing and delivering adds (s)", f(sign_send, 2), "0.40")
    check("Section 5.3: provider signs and sends in (s)", f(sign_send, 1), "0.4")
    check("Section 5.3: verifier checks and reaches a verdict in (s)",
          f(stream["10000:every_10_records"]["verification_seconds"], 1), "0.2")

    # Mainnet settlement experiment (tab:eval:mainnet-cases).
    s = n["settlement"]
    check("Mainnet challenges", str(sum(r["challenges"] for r in s["outcomes"])), "14")
    check("Mainnet sessions challenged", str(settlement["operations"]["openSession"]["transactions"]), "11")
    paper_cases = {"Approved write within scope, checked for scope": "3, no violation",
                   "Same approved write, checked for authorization": "3, no violation",
                   "Approved write outside the promised scope": "3, violation",
                   "Native write without the promise's required approval": "3, violation",
                   "Evidence altered after the final commitment": "1, violation",
                   "Final trace contradicts an earlier checkpoint": "1, violation"}
    observed = {r["case"]: f"{r['challenges']}, {', '.join(r['observed'])}" for r in s["outcomes"]}
    for case, paper in paper_cases.items():
        check(f"  {case}: challenges, outcome", observed.get(case, "missing"), paper)
    check("  Each observed outcome equals the case's expected outcome",
          "yes" if all(r["observed"] == [r["expected"]] for r in s["outcomes"]) else "no", "yes")
    check("All settlement checks passed", "yes" if s["status"] == "passed" and not s["failed_final_checks"] else "no", "yes")
    check("Mainnet transactions, settlement + both checkpoint matrices (incl. deployment)",
          str(n["grand_total"]["transactions"]), "180")
    settlement_ok, settlement_tx = settlement_receipts_match(settlement_dir)
    check("Settlement transactions and fees recomputed from the canonical receipts", "yes" if settlement_ok else "no", "yes")
    shipped = settlement_tx + sum(len(load(d / "chain-journal.json")["transactions"]) for d in (hermes_dir, openclaw_dir))
    check("Mainnet transactions with a shipped hash and canonical receipt", str(shipped), "180")
    deployed = [tx["receipt"]["contractAddress"] for tx in load(settlement_dir / "transactions.json") if tx["operation"] == "deploy"]
    check("Escrow contract on Base mainnet (deployment receipt)", deployed[0] if deployed == [s["contract"]] else "mismatch",
          "0x9eD99dF9702f6fdb0E5a4acad084Adb8342b4c4e")

    width = max(len(label) for label, _, _ in rows)
    failures = 0
    print(f"{'Quantity':<{width}}  {'computed':>24}  paper")
    for label, computed, paper in rows:
        status = ""
        if paper is not None:
            status = f"{paper}  {'ok' if computed == paper else 'DIFFERS'}"
            failures += computed != paper
        print(f"{label:<{width}}  {computed:>24}  {status}")
    checked = sum(paper is not None for _, _, paper in rows)
    print(f"\n{checked - failures}/{checked} values match the paper exactly.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
