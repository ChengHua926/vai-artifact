# Rule-scope review data

This directory records the scope review of cited policy violations, the evidence a check would need, and the resulting task counts. Benchmark traces, committee labels, and promise fires are unchanged.

## Files

- `appendix.json`: exact policy quotes, original categories, reviewed categories, original citations, and source paths.
- `retail_review.json`, `airline_review.json`, `claws_review.json`: review reasoning and required evidence. ClawsBench includes native-evidence checks and source hashes.
- `citation_notes.json`: the two Qwen cases where actual tool activity differed from the cited description.
- `data_verification.json`, `task_partition_verification.json`: checks recorded when the review and reporting artifacts were generated. They preserve historical verification results; report-file hashes do not imply those exports are stored here.
- `build_appendix.py`: reconstructs `appendix.json` and `summary.json` from the saved review and frozen task citations.
- `summary.json`: task panels and overlapping scope memberships.
- `task_partition.json`: non-overlapping task categories, assigning mixed tasks to could catch.

## What was reviewed

All 49 distinct Tau rule IDs and five ClawsBench rules cited in the 223 tasks labeled violation with no fire. There are 589 task-rule pairs: 205 Tau GLM, 376 Tau Qwen, and eight ClawsBench. The fifth ClawsBench rule appears in the fourth no-fire task included when reporting all 60 runs.

The user categorized the earlier groups of violations. The subsequent LLM-assisted review used the exact policy sentences, those categories, concrete proposed checks, their required inputs, and targeted native-trace checks. It was not independent human adjudication of all 589 pairs. Uninspected allegations remain the committee's allegations, not newly verified facts.

A mixed sentence is classified by the obligation actually cited. Its rule-level entry may therefore list multiple possible categories. Task-level counts use the reviewed task-rule entries, not the rule's possible capabilities.

## Category definitions

- Should catch: the particular violation can be decided from the current trustworthy structured evidence. This includes missing implementations when a valid violation is actually established.
- Could catch: a concrete additional consent, requested-item, proposal, source, target, or workflow record would enable a deterministic check. A claim from the acting agent that it complied is not sufficient evidence.
- Out of scope: deciding truth, meaning, or general handoff applicability requires semantic judgment under the current interface.

Empty reviewed categories mark specifically inspected claims that do not establish a counted violation. The original claims and task verdicts are retained for traceability. A mechanically checkable rule does not establish a missed violation if its alleged triggering event never happened.

## Counting

A task belongs to a category if at least one retained cited obligation belongs to it. Each task counts once per category. Categories overlap; they are not fractions of one exclusive partition. These counts describe scope, not measured performance after adding structure.

| Benchmark | No-fire violation tasks | Could catch | Out of scope | Both | Could only | Out of scope only |
|---|---:|---:|---:|---:|---:|---:|
| Tau GLM | 96 | 63 | 86 | 53 | 10 | 33 |
| Tau Qwen | 123 | 100 | 114 | 91 | 9 | 23 |
| ClawsBench, all 60 | 4 | 3 | 1 | 0 | 3 | 1 |

No confirmed current-scope mechanical miss was established by this review. That statement is bounded by the rule review and targeted inspections; it is not proof that every allegation or every policy requirement was checked.

## Why scope counts changed

General handoff applicability is now semantic, as the user requested. This reduces could-catch task memberships from 75 to 66 for GLM and from 109 to 103 for Qwen. Three further tasks in each cohort have only a refusal-versus-escalation issue supporting their previous could-catch membership; treating that judgment consistently as semantic produces 63 and 100. Concrete forbidden payment, cabin, and route proposals remain could catch.

The Qwen transfer-text and failed-read citations no longer create should-catch memberships. Ext-051 still has an authentication-workflow citation. Ext-266 has both an authentication-workflow citation and a semantic handoff citation. Their labels are unchanged.

For ClawsBench, failed lookups do not establish the cited repetition violation. The all-runs cohort adds the Slack task with missing repost and error-note actions, which could be checked with an explicit required-action set. Its claimed failure to read the target is contradicted by native observations. The result is three could-catch tasks and one semantic task.

## Limits to retain in any paper appendix

This is a retrospective explanation of scope, not held-out validation. Additional structure has not yet been evaluated on new runs. Broad semantic rules cannot be made mechanically checkable merely by asking an agent to emit an assertion. Source identity, consent, and requested-item bindings need evidence tied to the user's request or authoritative records; any generated message checks cover the bound claims or controlled templates, not arbitrary prose.

The full 60-run ClawsBench result retains all existing labels, including 11 runs whose labeling view lacked a usable agent timeline. Recovering native evidence for selected cases supports targeted corrections to explanations; it does not establish the completeness of every original labeling input.

## Non-overlapping task counts

The reported task partition assigns a task with at least one could-catch violation to could catch; otherwise it goes in out of scope only. Mixed tasks stay in could catch. This changes the presentation of task counts, not the per-rule categories or original labels.

| Benchmark | No-fire violation tasks | Could catch | Out of scope only |
|---|---:|---:|---:|
| Tau GLM | 96 | 63 | 33 |
| Tau Qwen | 123 | 100 | 23 |
| ClawsBench | 4 | 3 | 1 |

`task_partition.json` records this derivation; `appendix.json` retains the underlying overlapping memberships for detailed analysis. Spreadsheet and reporting exports are archived outside the prototype. The machine-readable review and its required inputs remain here.
