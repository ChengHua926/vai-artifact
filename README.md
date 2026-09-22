# VAI: verifiable agent insurance — artifact

Code, evidence, and evaluation data for the paper "Holding Agents Accountable with Verifiable Agent Insurance" (under review). Prepared for anonymous review: local paths and personal names were rewritten, and checksum files were regenerated afterwards.

## Layout

| Directory | Contents |
| --- | --- |
| `packages/` | Python packages: `commons` (record encoding, hashing, the five predicates), `sdk` (recording, checkpoints, session close), `store` (the provider's HTTP trace store, SQLite), `verifier` (challenge watcher and adjudication). |
| `contracts/` | `Escrow.sol` and its Foundry tests. |
| `integrations/` | The Hermes and OpenClaw adapters, the native patches against the pinned upstream commits (`*/patches/`), manifests, and tests. `PUBLISHED_FORKS.md` records the upstream commits and how the patched trees are verified. |
| `scripts/` | Local demo, Base mainnet and local-chain experiment drivers, the predicate catalog printer. |
| `evidence/` | The Base Sepolia protocol run (September 8, 2026) and the verifier measurements (September 21, 2026): code revision descriptions, receipts, inputs, logs. |
| `eval/` | Benchmark capture and replay code for AgentDojo, tau3-bench, and ClawsBench; the sealed cohort ledgers (`paper_main_v1/`); the labeling committee code, prompts, selections, and vote ledgers (`labeling/`); the replayed reference results and scope review (`reference_v2/`). See `eval/README.md`. |
| `supplement/` | Measurement runs behind the paper's tables: `2026-09-17-scope-verification` (the added-records and subjective-judgment review, the AgentDojo replay), `2026-09-16-practicality-and-agentdojo` (recording overhead, Base mainnet fees and waits, 14 mainnet challenges), `2026-09-21-verifier-prefix-optimization` (verifier processing time). Each has its own README or METHODOLOGY and REPRODUCE notes. |
| `corpus/` | `paper_main_v1-corpus.tar.gz`: the sealed Tau and AgentDojo captures. `clawsbench-standard60-corpus.tar.gz`: the ClawsBench Standard60 replay inputs. |
| `docs/` | How the system works, the promise catalog, the runbook for the local demo, design notes. |

## Running

The local demo (Anvil chain, store, verifier, agent) is described in `docs/RUNBOOK.md`. The benchmark replay is described in `eval/README.md`; the minimum is:

```bash
uv venv && uv pip install -e packages/commons -e packages/sdk -e packages/verifier -r eval/requirements.txt
python -m eval.run fetch --archive corpus/paper_main_v1-corpus.tar.gz
python -m eval.run evaluate --benchmark all
```

Requires Python 3.12, Foundry (`forge`, `anvil`), and for the integrations the pinned Hermes and OpenClaw checkouts named in `integrations/*/upstream.json`.

## What is not included

- The two human annotators' votes from the labeling calibration round. The committee ledgers, prompts, selections, and the final labels are included; the calibration inputs that carry human votes are withheld.
- The browser-based trace viewer, working notes, and two superseded replay outputs (`reference_v2/results`, `reference_v2/results_v3`); `results_v4` and `presentation_v4` are the versions the paper reports.
- The 74 MB SQLite trace store from the recording-overhead run; its summaries are in `supplement/2026-09-16-practicality-and-agentdojo/practicality/`.
- The patched Hermes and OpenClaw trees themselves; apply the patches in `integrations/*/patches/` to the pinned upstream commits.

Because paths were rewritten for review, every checksum file (`SHA256SUMS`, `ROOT_SHA256`, `*.sha256`, `MANIFEST.json`) was recomputed over the rewritten bytes, the seal manifests under `eval/paper_main_v1/seal/` and `cohort.lock.json` were resealed over the rewritten captures, and the original-seal anchor in `eval/dataset.py` was re-pinned accordingly. `python -m eval.run fetch` and `python -m eval.run verify` pass on this copy.

## Licenses

Our code and data: MIT (`LICENSE`). Third-party material and its licenses are listed in `NOTICE.md`.
