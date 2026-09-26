# Operating cost (Section 5.3 and its appendix)

Reproduce every operating-cost number in the paper from the recorded runs (Python 3.12, standard library only):

    python operating_cost/analyze.py

It reads `operating_cost/results/`, prints each number next to the value in the paper, and exits 1 if one differs.

| `results/` | Measurement | Produced by |
|---|---|---|
| `settlement-mainnet-01` | 14 challenges over 11 sessions on Base mainnet, escrow `0x9eD99dF9702f6fdb0E5a4acad084Adb8342b4c4e` | `run_settlement.py` |
| `checkpoint-mainnet-hermes-01`, `checkpoint-mainnet-openclaw-01` | fee and record wait per checkpoint policy on Base mainnet (Table 2) | `checkpoint_refresh.py` |
| `recording-hermes-01`, `recording-openclaw-01` | recording overhead, 100 writes, 30 paired repetitions | `measure_recording_refresh.py` |
| `verifier-prefix-comparison-01` | verifier time and memory (verifier-cost table, Table 2 verifier column) | `measure_verifier_handoff.py --mode compare-prefixes` |
| `verifier-handoff-01` | signing and delivery of a 10,000-record trace | `measure_verifier_handoff.py` |

All runs are from September 25, 2026, on one prototype revision. The 180 mainnet transactions are listed with their hashes and canonical receipts, so every fee can be checked on Base mainnet. Per-run files keep only the fields the analysis reads, without local file paths.

## Rerunning the measurements

The run scripts are the ones used for the paper; only their paths were adapted to this layout. Run them from the repository root with Python 3.12, the packages in `packages/`, Foundry (`anvil`; `cd contracts && forge build --skip test`), and the two harnesses checked out next to the repository at the `commit` in `integrations/<harness>/upstream.json` with the release patch applied; the scripts refuse any other checkout:

    git clone https://github.com/NousResearch/hermes-agent ../hermes-agent
    git -C ../hermes-agent checkout 5e01a5dbf1b7bc0144d9057be706da1ea9f065c3
    git -C ../hermes-agent apply "$PWD/integrations/hermes/patches/hermes-5e01a5d-native-authorization.patch"
    git clone https://github.com/openclaw/openclaw ../openclaw
    git -C ../openclaw checkout 89c90210fb90c3c1d1bd54d56cd7be00e59aeed4
    git -C ../openclaw apply "$PWD/integrations/openclaw/patches/native-observation.patch"

Hermes runs with its own Python dependencies installed; OpenClaw needs `pnpm install`. OpenClaw uses the Node binary on `PATH` or `AA_NODE` (paper: 24.19.0). Each `--output` must be a new directory outside the repository.

    python operating_cost/run_settlement.py run --network local --output OUT              # 14 challenges on a local Anvil chain
    python operating_cost/checkpoint_refresh.py run --harness hermes --network local --output OUT
    python operating_cost/checkpoint_refresh.py run --harness openclaw --network local --output OUT
    python operating_cost/measure_recording_refresh.py --harness hermes --output OUT
    python operating_cost/measure_recording_refresh.py --harness openclaw --output OUT

`--smoke` gives a short local run of the checkpoint and recording scripts. The paper's mainnet runs used `run_settlement.py run --network mainnet --execute-mainnet` and `checkpoint_refresh.py run --network mainnet --execute-mainnet --deployment DEPLOYMENT --keystore-dir KEYS --expected-revision REV --prior-fees-wei FEES`. They spend real ETH and need your own funded Base wallet: set `AA_MAINNET_KEYS_DIR` to its encrypted keystores and `AA_MAINNET_PUBLIC` to a `funding.json` with their public addresses (as in `fixtures/funding.json`); no key is included.

The verifier measurements (`measure_verifier_handoff.py`, with `compare_verifier_prefixes.py`) also need the archived verifier inputs of September 16 (a source snapshot of the earlier verifier and the seed records), which are not included because they contain local paths; their recorded per-run results are in `results/`.
