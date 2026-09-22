# Practicality experiment readout

Completed 2026-09-16. Paper files were not edited. These are experiment results and reporting recommendations, not a draft paper section.

## Proposed question for Section 5.2

Can the implemented system record actual agent actions, settle a challenge on a public network, and do so at a measurable cost? Section 4 describes the integration. This section should measure its operation, overhead and checkpoint tradeoff.

## Public execution and settlement

The experiment used the pinned native Hermes tool dispatcher and ACP approval hooks, with controlled user responses and real filesystem writes. There was no LLM generation or human response wait. Provider, challenger and verifier used separate signing keys; the verifier ran in a separate process. One experimental operator controlled all roles.

All 14 challenges settled as expected over 11 unique native sessions. Three repetitions each covered a permitted write checked for scope, the same permitted write checked for consent, an explicitly approved out-of-scope write, and a normally permitted native write without the explicit approval required by its promise. Scope and consent are two existing catalog predicates; this is not a public-network test of all five predicates.

The two additional controlled cases made committed evidence unavailable to the verifier after provider response, or presented a final trace that contradicted an earlier on-chain prefix. Both settled as violations. These were controlled verifier-input faults, not observations of a real network outage.

The eight valid challenges reduced reserves by 80 billion wei and paid the challenger 160 billion wei including returned bonds. The six invalid challenges left their reserves unchanged and credited 60 billion wei of forfeited bonds to the provider's contract balance. That credit is not a wallet withdrawal. All per-case balances, events, hashes and native file effects were checked.

[Base measurement contract](https://basescan.org/address/0xc1EC14121b60EBB6111922e5427C7E5342d25040) · [Raw public evidence](../mainnet04/evidence.json) · [Canonical receipt reconciliation](canonical-receipts.json)

The contract's 3-day response, 10-day verdict and 30-day challenge windows were unchanged. Deadline paths were tested locally with Anvil, not expired on Base mainnet during this experiment.

## Recording overhead

Thirty paired repetitions compared the same 100 native 1 KiB writes, with three warmups excluded. All 9,000 measured file effects matched. Sixty captured traces had 400 records each and passed hash and predicate checks.

| Mode | Mean time per 100 writes, including finalization | Added time |
|---|---:|---:|
| Capture disabled | 6.12 s | — |
| Capture and HTTP store | 6.82 s | 0.70 s, 11.4% |
| Capture, store and local chain | 7.86 s | 1.74 s, 28.3% |

The paired 95% intervals for added time are 0.629–0.776 s and 1.639–1.845 s. These results isolate tool recording and local anchoring; they are not whole-agent or mainnet runtime percentages. Measurements used one shared Mac without CPU isolation.

[Full local measurements](../local-performance/RESULTS.md) · [Runtime and verifier plot](local-practicality.png)

## Checkpoint frequency on Base

Three repetitions per policy used 20 actual native writes, producing 80 records. Each session paused for 35 seconds after its first two writes, then resumed at a target 0.25-second interval. Strategy order was shuffled with a fixed seed. The pause exercises the 30-second timer; this public workload does not reach the 60-second timer before a record-count trigger. A separate local workload exercised both timers.

| Policy | Checkpoint/final transactions | Mean fee (µETH) | Mean maximum unanchored age, seconds (range) | Maximum unanchored records, range |
|---|---:|---:|---:|---:|
| 10 records or 30 seconds | 4–4 | 1.908 | 34.0 (33.7–34.2) | 61–62 |
| 30 records or 60 seconds | 3–3 | 1.409 | 41.1 (41.1–41.1) | 72–72 |
| Final commitment only | 1–1 | 0.323 | 46.7 (46.6–46.7) | 80–80 |

One µETH is 0.000001 ETH. Fees include execution and L1 data fees, but exclude promise registration and session opening. Exposure is reconstructed from each record's timestamp through the client's receipt observation. It is not L1 finality, and it is not a proof of protection from every form of missing capture.

Public RPC calls were paced at 0.25 seconds minimum and retried on rate limits. Those delays are included. The threshold starts submission; it is not a hard cap on pending records. The 35-second pause and short resumed burst also mean these results describe this workload, not an optimal cadence for every agent.

[Checkpoint figure](public-checkpoints.png) · [Per-run and per-operation measurements](public-summary.json)

## Verification cost and the current limit

Using the real verifier with HTTP-store retrieval, but replacing chain reads and settlement with fixtures, 10,000 native-shaped records took 0.395 s with only a final commitment. With 1,000 cumulative prefix checkpoints, the same size took 36.54 s and about 1.0 GiB peak process RSS. Each cell has five fresh-worker repetitions.

The current implementation hashes every full historical prefix again. At a fixed record interval, the total hashed data grows quadratically. The predicate itself took about 0.02 s in both 10,000-record cases; prefix checks accounted for about 36.11 s in the dense case. The larger fixtures expand real native-shaped records rather than executing 10,000 new actions. This result must be reported as a current scaling limitation, not described as uniformly cheap verification.

## Fees, preservation and checks

The completed measurement run contains 136 successful transactions and cost 0.000095174872 ETH. Across all 156 unique transactions, including two deployments, funding, interrupted pilots, recovery and cleanup, total actual fees were 0.000133372218 ETH. The remaining controlled wallet and escrow balances total 0.001919726494 ETH. No role-wallet top-up was needed.

The initial public RPC attempts were interrupted by rate limits. They are preserved, not silently dropped. An uncertain final-commit transaction was reconciled before recovery; the native tool was not executed again. Every transaction receipt was refreshed, its nonzero block hash checked against its canonical L2 block, and total fees reconciled exactly against the funded ETH, all controlled balances and two unrelated incoming transfers totaling 0.000003570216417 ETH. Those incoming credits are recorded separately and are not counted as reduced experiment fees. The receipts establish canonical L2 inclusion, not L1 finality. Provisional L1 fees were replaced with canonical receipt values in the analysis; raw first-observed receipts remain unchanged.

Measurement source is committed locally on `eval-practicality-mainnet` through `08d9695`, based on protocol revision `592d4cc`. Source snapshots are under `../source-snapshots/`. No changes from this turn have been pushed or merged. The core SDK, predicates, verifier and contract semantics were unchanged; experiment scripts and explicit mainnet configuration, pacing and transaction journaling were added. Private keys and passwords remain outside the repository and evidence archive.

## What to put in the paper

Use three short parts: public end-to-end execution, recording overhead, and checkpoint cost versus exposure. Put the verifier scaling result with the checkpoint tradeoff. Use the contract link and a compact case table to establish public execution, then one runtime/scaling figure and one checkpoint figure if space permits. Do not repeat the integration description from Section 4 or turn these controlled runs into claims about all agents, all predicates or network conditions.

Confidence is high in these runs, receipts, balances and measured costs. Generalization to other tools, workloads and production RPC services remains unmeasured.
