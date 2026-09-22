# Native Hermes verification

Pinned base: `5e01a5dbf1b7bc0144d9057be706da1ea9f065c3`, manifest `0.16.0`, plus the patch identified in [upstream.json](upstream.json).

From `/workspace/hermes-agent`:

```sh
scripts/run_tests.sh /workspace/agent_accountability_revision/integrations/hermes/tests/test_native_authorization.py tests/tools/test_approval_plugin_hooks.py tests/acp/test_edit_approval.py tests/acp/test_approval_isolation.py tests/tools/test_approval.py tests/tools/test_execute_code_approval_cluster.py tests/hermes_cli/test_plugins.py -- -q --tb=short
```

Full native patch regression result before the final adapter-only lifecycle fix: **367 passed, 0 failed**, across seven files. The new integration file contributes 18 cases; six upstream files contribute 349 regression cases. Full runner output is saved in [tests/native-test-output.txt](tests/native-test-output.txt). The runner's 0% display reflects its initial zero-test estimate; the per-file results and final exit status are successful.

The 18 integration cases use real native middleware, approval guards, ACP bridges, SDK predicates, and file/terminal handlers. ACP transport, user answers, automated-review verdicts, and the optional external command scanner are controlled. No live model, chain, production client UI, or plugin discovery runs in these tests. Filesystem mutations are confined to disposable fixtures.

The patch contains seven source files, with 180 added and 37 removed lines. It passed forward `git apply --check` against pristine files extracted from the pinned commit. Applying it reproduced all seven edited files byte for byte. Native `git diff --check` passed. No dependency lockfile or manifest was changed; only the Hermes virtual environment received the missing pinned test dependencies.

The tests first reproduced missing/misattributed authorization and premature per-turn finalization. Regression cases now cover exact allow-once consumption, denied and timed-out nonexecution, reused native policies, automated-review provenance, rewritten effective arguments, concurrent/nested sessions, and finalization during an active call. See [FINDINGS.md](FINDINGS.md) for measured paths and limits.

The independent-review regressions also require typed nonexecution for a rejected final-seam rewrite (no effects and no AAP-1 fault), preserve ordinary effectful exceptions, reject explicit party mismatches on reused native session IDs, and retain the default binding for retry after SDK finalization fails.

## Final adapter lifecycle check

```sh
scripts/run_tests.sh /workspace/agent_accountability_revision/integrations/hermes/tests/test_native_authorization.py -- -q --tb=short
```

Result: **20 passed, 0 failed**. Output: [tests/native-lifecycle-test-output.txt](tests/native-lifecycle-test-output.txt). The two added cases reproduce caller-context staleness when another thread finishes deferred finalization, including restoration of a nested outer session. They also exercise default dispatch before an explicit `current()` call can clean the context. Pending and failed finalization remain reachable.

The native patch is unchanged. Combining the final 20 integration cases with the 349 previously passing, unchanged upstream regressions gives 369 distinct verified cases; this is not a claim that all 369 ran in one final command.

The final 20-case command was rerun successfully against SDK authorization `schema_version=1` and the invocation evaluator requiring integer version 1. The focused output was refreshed; SDK and evaluator source hashes are recorded in `upstream.json`. The historical v2 evaluator was unchanged.
