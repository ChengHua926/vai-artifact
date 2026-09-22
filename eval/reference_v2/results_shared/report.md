# Shared catalog replay

The shared AAP catalog was replayed on the same captured observations as v4. No new agent runs or labels were produced.

| Benchmark | Runs | Violation + detected | Violation + not detected | Clean + detected | Clean + not detected |
|---|---:|---:|---:|---:|---:|
| Tau GLM | 164 | 15 | 96 | 0 | 53 |
| Tau Qwen | 164 | 31 | 123 | 0 | 10 |
| ClawsBench | 60 | 0 | 4 | 2 | 54 |

Compared 388 runs, 13318 committed records and 5048 rule verdicts. Differences: 0.

Comparison includes every verdict, exact reason, first offending record, complete fire ledger, diagnostics, unsupported-evidence output and trace hash. The original evaluator is loaded from its frozen source bundle and checked against the original saved commitments. The new evaluator also checks SDK versus hash-resolved replay agreement.

Removing legacy precomputed allowed/amount answers across 6920 checks changed 0 cases.

Labels and same-reason annotations are read only after all evaluations. Selected fires must match the frozen presentation exactly before its reason-match annotations are carried forward. The existing exclusions of message-format checks remain unchanged.

- Tau GLM: 15/15 detected violation tasks match a cited reason.
- Tau Qwen: 30/31 detected violation tasks match a cited reason.

Current promises use AAP-2, AAP-3 or AAP-5. Their parameters commit the exact observation profile, including domain policy definitions and state reconstruction. Shared code evaluates the conditions. Domain-specific policy code remains necessary; this is not automatic policy translation or use of the five unchanged historical functions.

This verifies replay on existing traces. It does not establish live integration into either benchmark agent or public-network settlement.

Regenerate with `.venv/bin/python -m eval.reference_v2.replay_shared`; verify the saved artifacts byte-for-byte with the same command plus `--check`. Historical results and labels are never overwritten.
