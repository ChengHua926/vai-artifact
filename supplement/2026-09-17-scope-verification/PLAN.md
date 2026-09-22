# Executable scope verification

Approved design: the conversation's September 17 plan. Scope checks support conditional checkability; they do not create measured detections on historical traces.

- [x] Preserve and synchronize the paper's existing edit, reconciling remote edits without force push.
- [x] Create isolated prototype worktree `.audit-worktrees/scope-verification` from `592d4cc`.
- [x] Add reusable structured-evidence recording/check support through existing registered AAP predicates. Test exact approval, data bindings, temporal order, missing/incomplete evidence, terminal obligations, and faithful rendering.
- [x] Build independently specified compliant/violating/unknown fixtures for every retained could-detect obligation family in AgentDojo, Tau and ClawsBench. Connect each cited obligation to its fixture and trusted input requirements.
- [x] Run actual SDK recording paths with scripted actions and captured user/controller inputs. Preserve test output and trace provenance; do not call these new model-generated benchmark runs.
- [x] Derive task-level supported/conditional/unresolved counts from frozen citations and successful fixture results; retain historical counts and explain differences.
- [x] Separately replay a declared AgentDojo policy variant that includes initial structured calendar contacts. Report effects on attack catches and benign alarms across the full corpus, preserving original allowlists and results.
- [x] Consolidate all 29 original-policy benign-alarm reviews, including the six calendar runs and three special Qwen cases.
- [x] Independently review tests, attribution, source authority and counting; verify frozen input hashes and relevant regression tests.
- [x] Archive source, commands, results and checksums outside the prototype. Report paper synchronization and measured/conditional results; propose any final paper claim changes only after verification.

## Ownership

- Shared checking/recording support: `packages/commons/aa_commons/structured_workflow.py`, its tests, and necessary catalog exports; coordinate any shared-file changes.
- Obligation fixtures and mappings: `eval/scope_verification/obligations.py`, `fixtures.py`, `test_obligations.py`, and data maps. No frozen files are modified.
- AgentDojo sensitivity: isolated `agentdojo-recipient-review` worktree, new sensitivity CLI/tests only; preserve prior replay sources and outputs.
- Root: aggregation CLI, manifests, independent checks, paper synchronization, archive and reporting.

## Required evidence and acceptance criteria

Each conditional obligation must name a non-agent authority, a concrete field schema, applicability and completion conditions, a registered predicate and successful positive/negative controls. Missing historical records are not invented. Inputs supplied in synthetic experiments remain explicitly hypothetical. Unknown or incomplete evidence does not become a confirmed policy violation. No attack target, grader verdict or label supplies authorization.

Original SDK verdicts, labels and corpus hashes remain unchanged. Never force a preferred category count. Keep complete initial-state authorization distinct from mere address presence. No new AAP number or benchmark-specific checking callback. Tests use real SDK calls and registered evaluation; expected outcomes are specified independently of the checker.

Unresolved user questions: none. Technical gaps found by implementation are reported as unsupported checks, not silently filled with judgment.
