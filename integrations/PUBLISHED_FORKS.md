# Native patches against the pinned upstream commits

The patches under `hermes/patches/` and `openclaw/patches/` are the exact native changes recorded in the integration manifests. Apply each to its pinned upstream parent to obtain the patched tree; the patched commit ids below identify those trees. Each patched commit has one parent: the pinned upstream commit below. Upstream history, licenses, and package versions are preserved. Both forks use branch `research/accountability-2026-09-08` and annotated tag `accountability-2026-09-08`; the full commit hashes are the reproduction pins.

| Framework | Published commit | Pinned upstream parent | Git tree |
| --- | --- | --- | --- |
| Hermes | [`9801de7052b9f746791f96600ea19b9399de4b13`](https://github.com/NousResearch/hermes-agent/commit/9801de7052b9f746791f96600ea19b9399de4b13) | `5e01a5dbf1b7bc0144d9057be706da1ea9f065c3` | `0c0fe75966cf74db5cf1f5569455de584829bc19` |
| OpenClaw | [`84f693f005fd0f0beb1f99c97b24c6e12d21e2e6`](https://github.com/openclaw/openclaw/commit/84f693f005fd0f0beb1f99c97b24c6e12d21e2e6) | `89c90210fb90c3c1d1bd54d56cd7be00e59aeed4` | `6cf7c7b0b536d0a2f1181f013a7ccde56abc5efe` |

The Hermes patch remains SHA-256 `4fdcbfc6818300061f201697e97c6982398823fc8b12f5a8f9f5ee73d791a46e` (7 files, 180 additions, 37 deletions). The OpenClaw patch remains SHA-256 `ae5f71a22017be674774e833a7920966215e2119d6f5275dc0908bec231b40a8` (10 files, 831 additions, 9 deletions, including tests).

## Verification on 2026-09-08

For each framework, a temporary Git index applied the recorded patch to the pinned parent and produced the same complete Git tree as the native working checkout and published commit. This checks the whole tree, including new test files and unchanged licenses. GitHub API reads confirmed both public commits' parents and trees. Public fetches then supplied the commits for detached checkouts in the isolated reproduction clones.

Fresh verification used clean local clones with existing installed dependencies linked into them; dependency installation from scratch was not tested. The prototype adapter and tests came from commit `876e9d37e80678fe113aa6de7de3de299275695b`. Hermes used Python 3.12.13. OpenClaw's final native run used Node 24.19.0 and pnpm 11.19.0. No model calls, blockchain transactions, or production approval UI were involved.

| Check | Result |
| --- | --- |
| Hermes native integration plus six relevant upstream test files | 369 passed: 20 integration cases and 349 upstream regressions |
| Hermes integration rerun after checking out the public commit | 20 passed |
| OpenClaw six native test files, including the real isolated gateway approval test | 81 passed |
| OpenClaw observation and native seam rerun after checking out the public commit | 12 passed |
| OpenClaw core typecheck, `node scripts/run-tsgo.mjs -p tsconfig.core.json` | Passed |
| OpenClaw Python helper tests | 11 passed |
| OpenClaw plugin transport tests on Node 24.19.0 | 6 passed |

The complete targeted commands were:

```sh
# From hermes-agent, beside agent_accountability_revision:
scripts/run_tests.sh ../agent_accountability_revision/integrations/hermes/tests/test_native_authorization.py tests/tools/test_approval_plugin_hooks.py tests/acp/test_edit_approval.py tests/acp/test_approval_isolation.py tests/tools/test_approval.py tests/tools/test_execute_code_approval_cluster.py tests/hermes_cli/test_plugins.py -- -q --tb=short

# From openclaw:
node scripts/run-vitest.mjs src/agents/tool-observation.test.ts src/agents/aa-accountability.seam.e2e.test.ts src/agents/bash-tools.exec-host-gateway.test.ts src/agents/bash-tools.exec.background-abort.test.ts src/agents/bash-tools.exec-foreground-failures.test.ts src/agents/bash-tools.exec-gateway-approval.e2e.test.ts
node scripts/run-tsgo.mjs -p tsconfig.core.json

# From agent_accountability_revision:
PYTHONPATH=packages/commons:packages/sdk:packages/store:packages/verifier .venv/bin/python -m pytest -q integrations/openclaw/tests
node --test integrations/openclaw/plugin/tests/hooks.test.ts
```

Two environment limits were observed and retained in this report. The first Hermes clone was under macOS `/private/var/`; its native sensitive-path guard refused four real file-write cases (365 passed, 4 failed). Moving the unchanged clones into an ordinary workspace produced the 369-pass result above. An additional OpenClaw `pnpm build` attempt stopped before compilation because pnpm wanted to reconcile the linked dependency directory and refused without a TTY (`ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`). Dependencies were preserved; this publication does not claim a fresh full build.

See the [Hermes](hermes/README.md#reproduce-the-native-tests) and [OpenClaw](openclaw/README.md#reproduce) instructions for clean fork checkout commands and integration limits. The native test evidence remains separate from the synthetic Base Sepolia evidence.
