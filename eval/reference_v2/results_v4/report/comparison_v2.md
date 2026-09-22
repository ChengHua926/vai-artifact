# Baseline and corrected replay

Raw task cross-tabs against unchanged labels. Same-reason matches and protocol/evidence limitations are reported separately in report.md.

| Panel | v2 violation + fire | v4 violation + fire | v2 clean + fire | v4 clean + fire |
|---|---:|---:|---:|---:|
| tau_glm47 | 9 | 107 | 0 | 50 |
| tau_qwen3_30b | 13 | 50 | 0 | 0 |
| clawsbench_primary | 0 | 0 | 10 | 2 |

The changes_from_v2.jsonl ledger compares action/arm/target witnesses. A changed witness can produce an added and removed row while the same breach continues.
