# Reproduce

The source snapshots correspond to prototype base 592d4cc. Existing working copies are at /workspace/.audit-worktrees/scope-verification and /workspace/.audit-worktrees/agentdojo-recipient-review. To reconstruct elsewhere, create a fresh detached worktree at that base and overlay the relevant source/ tree; do not overwrite another working checkout. Dependencies are available in the prototype eval/.venv and Hermes environment recorded by the AgentDojo reproduction notes.

From the scope-verification worktree, set PYTHONDONTWRITEBYTECODE=1 and PYTHONPATH=.:packages/commons:packages/sdk, then use /workspace/vai/eval/.venv/bin/python:

```sh
scope_python=/workspace/vai/eval/.venv/bin/python
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:packages/commons:packages/sdk
"$scope_python" -m pytest -q packages/commons/tests/test_constraints.py packages/commons/tests/test_policy_profiles.py packages/commons/tests/test_structured_workflow.py packages/sdk/tests/test_authorization.py eval/scope_verification
"$scope_python" -m eval.scope_verification.report --reference-root /workspace/vai/eval --agentdojo-results /workspace/archive/2026-09-16-agentdojo-outbound-v2/results --output /tmp/scope-verification-new-output
"$scope_python" -m eval.scope_verification.initial_policy_scope --initial-results /workspace/.local/agentdojo-initial-destinations-sensitivity-20260917 --scope-results /tmp/scope-verification-new-output --output /tmp/initial-policy-scope-new-output
```

Use fresh output directories. Fixtures create fresh session IDs/timestamps, so their trace bytes change across executions; the declared controls, verdicts, mappings and counts must agree. Each execution stores its own trace hashes and source/input hashes.

AgentDojo's exact replay commands are in agentdojo/initial-destinations/reproduction.txt. Both original and revised policy outputs are preserved. The broad experiment admits only the explicitly enumerated initial destination fields, not arbitrary prose. Its sensitivity report's four pending scope decisions are resolved separately in initial-policy-scope/review.json; the old report remains unchanged as an earlier analysis artifact.

The original benchmark corpus and native dependencies are not duplicated here. Their locations and hashes are in the manifests. CHECKSUMS.sha256 covers every file in this archive.
