# Updated evaluation

Labels and the 49/60 ClawsBench cohorts are unchanged. Counts use the selected promises from the complete v4 SDK/resolved-evaluator replay.

| Benchmark | Tasks | Violation + fire | Violation + no fire | Clean + fire | Clean + no fire | Same reason |
|---|---:|---:|---:|---:|---:|---:|
| Tau GLM | 164 | 15 | 96 | 0 | 53 | 15/15 |
| Tau Qwen | 164 | 31 | 123 | 0 | 10 | 30/31 |
| ClawsBench, 49-run subset | 49 | 0 | 3 | 2 | 44 | 0/0 |
| ClawsBench, all runs | 60 | 0 | 4 | 2 | 54 | 0/0 |

Scope classifications remain an analysis of the cited rules. They are not new labels. A task may have multiple scope categories. Allegation-quality flags stay separate from capability: a mechanically checkable allegation is not a confirmed miss when its factual premise is contradicted by the trace.

The complete rule inventory and per-task evidence are in rule_review.md, summary.json, and tasks.jsonl. Changes against the previous selected results are in changes.jsonl.
