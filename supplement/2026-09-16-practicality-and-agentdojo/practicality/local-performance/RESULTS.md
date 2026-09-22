# Local practicality results

Complete local run: 30 paired runtime repetitions, five fresh verifier workers per size/cadence, and three local paced repetitions per policy. Confidence is high for these measured runs; broader tool or deployment performance is unmeasured.

## Native Hermes runtime

100 real write_file invocations per run, each writing the same 1 KiB contents through native ACP allow_once approval. Capture produces 400 records. Model generation and human response delay are absent. All 9000 measured native writes produced the same verified file effects; all 60 captured traces matched stored hashes and satisfied both promises. Three warmup runs are excluded.

| Mode | Mean actions (s) | Mean finalize (s) | Mean total (s) | Mean per-action dispatch (ms) | Mean client CPU (s) | Mean record payload (bytes) |
|---|---:|---:|---:|---:|---:|---:|
| capture_off | 6.123 | 0.000 | 6.123 | 61.20 | 1.475 | 0 |
| http_store | 6.799 | 0.024 | 6.823 | 67.98 | 1.792 | 646532 |
| http_store_anvil | 7.605 | 0.254 | 7.858 | 76.03 | 4.822 | 650122 |

Paired added total runtime versus capture disabled (bootstrap 95% intervals, 30 repetition-level pairs):
- http_store: 0.700s [0.629, 0.776], 11.4% of mean baseline.
- http_store_anvil: 1.735s [1.639, 1.845], 28.3% of mean baseline.

Native setup is separate in the raw artifacts. Transaction spans may overlap actions and must not be summed into wall time. CPU is the client process (not all possible native child processes); store and Anvil process CPU are separately available. This was one shared macOS ARM64 host without CPU isolation.

## Off-chain verifier processing

Actual process_challenge logic and HttpStore retrieval; fixture roots replace chain reads and settlement is stubbed. Expanded real native-shaped records retain invocation-bound authorization. These timings exclude chain RPC and settlement. Peak RSS is the fresh verifier process OS high-water mark, including imported libraries.

| Records | Prefix checkpoints | Total (s) | Retrieval (s) | Final hash (s) | Prefix checks (s) | Predicate (s) | Peak RSS (MiB) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 10 | 0.015185 | 0.009093 | 0.001127 | 0.003621 | 0.000216 | 71.8 |
| 100 | 0 | 0.011323 | 0.009082 | 0.001076 | 0.000004 | 0.000208 | 71.7 |
| 1000 | 100 | 0.371483 | 0.033330 | 0.007137 | 0.327173 | 0.001982 | 80.9 |
| 1000 | 0 | 0.045353 | 0.034292 | 0.007198 | 0.000023 | 0.002024 | 79.9 |
| 10000 | 1000 | 36.537052 | 0.320961 | 0.070219 | 36.111189 | 0.020448 | 1037.6 |
| 10000 | 0 | 0.394981 | 0.295080 | 0.069293 | 0.000225 | 0.019805 | 168.7 |

Dense cumulative prefix checking is a material current limitation: at a fixed 10-record cadence the implementation rehashes every historical prefix, causing quadratic hashed data. At 10000 records, prefix validation alone averaged 36.111s; total processing averaged 36.537s and peak RSS 1037.6MiB. Final-only processing averaged 0.395s and 168.7MiB. Do not generalize the dense-prefix timing to every deployed session, because actual asynchronous checkpoint counts vary.

## Local checkpoint cadence

Synthetic paced SDK records on actual Anvil receipts: nine concurrent sessions, each with 20 records at 0.2s spacing, an idle period, then another 20 records starting at 69s. This is not native tool latency. Transactions below include checkpoints and final commitment, excluding session opening. Finalization proceeds serially across sessions, so queue/order effects are included in observed delays.

| Policy | Transactions/session | Mean anchor delay (s) | Max unanchored records | Max unanchored age (s) | Gas/session |
|---|---|---:|---:|---:|---|
| 10_records_30s | [5, 5, 5] | 1.016 | 11 | 2.058 | [399502, 399574, 399574] |
| 30_records_60s | [2, 2, 2] | 30.450 | 20 | 60.361 | [150913, 150937, 150937] |
| final_only | [1, 1, 1] | 37.181 | 40 | 73.971 | [53432, 53444, 53444] |

The configured 10-record trigger was not a hard ceiling: observed unanchored count reached 11 while a checkpoint was in flight. Receipt observation is not L1 finality. Local Anvil gas use and timing are not Base mainnet fee or latency estimates.

## Provenance and checks

Recorded protocol base: `592d4cca577f885368acda8251aea3edfef99f24`. Pinned Hermes: `9801de7052b9f746791f96600ea19b9399de4b13`. Run duration: 16.20 minutes. Seed: 20260916. Exact command, package versions, module origins and runner hash are in metadata.json; source-provenance.json separately records committed measurement-source revision and full hashes. Two measurement-fixture tests passed. All run assertions passed and the process exited 0. Store and Anvil logs contain no error/exception/revert/failed text; successful transaction records alone do not prove absence of a transient SDK checkpoint retry.

Full raw evidence: runtime-raw.jsonl, scaling-raw.jsonl and each worker JSON, cadence-raw.json, chain-events.json, native-workload.json, native-seed-records.json, trace-store.sqlite, and service logs.

During the run, the parent changed scripts/_config.py; both observed hashes are retained. All protocol implementation files and the measurement runner matched the mid-run manifest after completion. Local RPC/chain selection in this runner is explicit and does not call configuration wallet or deployment helpers.
