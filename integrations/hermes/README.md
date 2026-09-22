# Hermes integration

The adapter records effective tool invocations, native authorization decisions, and outcomes. It requires Hermes commit `5e01a5dbf1b7bc0144d9057be706da1ea9f065c3` (manifest `0.16.0`) plus the patch in [patches/](patches/), or the identical published fork commit [`9801de7052b9f746791f96600ea19b9399de4b13`](https://github.com/NousResearch/hermes-agent/commit/9801de7052b9f746791f96600ea19b9399de4b13). [upstream.json](upstream.json) identifies the patch and measured native paths; [FINDINGS.md](FINDINGS.md) describes the boundary.

## Reproduce the native tests

The personal fork contains the patch on branch `research/accountability-2026-09-08` and tag `accountability-2026-09-08`. Clone the tag into a `hermes-agent` directory beside the prototype checkout named `agent_accountability_revision`:

```sh
git clone --depth 1 --branch accountability-2026-09-08 https://github.com/NousResearch/hermes-agent.git hermes-agent
git -C hermes-agent rev-parse HEAD
```

The reported HEAD must be `9801de7052b9f746791f96600ea19b9399de4b13`. This checkout already includes the patch. [Published fork verification](../PUBLISHED_FORKS.md) records the exact parents, tree comparisons, and fresh tests. On macOS, put this directory layout in an ordinary workspace: the native file guard refuses writes beneath `/private/var/`, including the default temporary-directory location used by some test setups.

Alternatively, use a checkout of the pinned upstream commit and apply the patch once from that checkout:

```sh
git apply --check ../agent_accountability_revision/integrations/hermes/patches/hermes-5e01a5d-native-authorization.patch
git apply ../agent_accountability_revision/integrations/hermes/patches/hermes-5e01a5d-native-authorization.patch
```

The test environment uses Hermes's installed dependencies plus `pytest==9.0.2`, `pytest-asyncio==1.3.0`, and `agent-client-protocol==0.9.0`. From the Hermes checkout, use its required test runner:

```sh
scripts/run_tests.sh ../agent_accountability_revision/integrations/hermes/tests/test_native_authorization.py -- -q --tb=short
```

The test file resolves the sibling revision checkout and its Python packages itself. It sets a temporary `HERMES_HOME`, executes real native file/terminal handlers against disposable fixtures, and controls user answers and ACP transport responses. It makes no model or blockchain calls. [TEST_RESULTS.md](TEST_RESULTS.md) records the verified regression command and result.

## Runtime binding

`aa_hermes.register(ctx)` registers the final `tool_dispatch` execution wrapper, approval observers, and lifecycle callbacks. The patched final wrapper runs after ordinary argument-rewriting middleware and before native approval and dispatch. Its arguments cannot be rewritten. Native approval still decides whether execution proceeds.

Each invocation receives a new action ID. The adapter records the native request, raw response, normalized authorization, and native outcome under that binding. It keeps native tool-call, request, and session IDs when present. The SDK hashes the effective argument object. An earlier allow-once never covers a later call, even when its arguments match. Session and permanent reuse require a fresh native policy-applied event for the actual invocation. Automated review remains `auto_review`; AAP-1 does not count it as human authorization.

The adapter registers AAP-2 lexical scope and AAP-1 invocation authorization. `authorization_tools` selects which tool calls require authorization; the default is `write_file`, `patch`, `terminal`, and `execute_code`. Native approval policy can allow some of these calls without a prompt. The adapter records no invented grant for those calls, so they can violate the declared promise. Terminal/code approvals cover only their exact invocation; the adapter infers no filesystem effects from shell or code strings.

Native `on_session_end` fires after each turn. The adapter calls `checkpoint(force=False)` there and retains the session. `on_session_finalize` and reset finalize it, waiting for active invocations to finish. Context-local bindings keep nested and concurrent sessions separate. A call without a matching accountability session produces a warning and remains uncovered.

An explicit party must match an existing native session's party; lifecycle callbacks may omit it when reusing that session. If finalization fails, the default binding remains available so `end_session()` can retry without a native session ID. A refusal to rewrite arguments at the final seam records explicit nonexecution. An exception after a handler ran remains an error outcome and is not treated as proof of nonexecution.

Default lookup prunes context-local entries against the shared session registry. When another thread completes deferred finalization, the caller sees no closed session; a surviving outer session becomes its default again.

Lifecycle-created sessions are off chain by default. For a funded deployment, call `begin_session(native_session_id=..., store=..., chain=..., provider_addr=..., bond_wei=..., payout_wei=...)` before the matching native session-start event. Reusing that ID retains the configured session. Configure new IDs explicitly after a reset; the reset callback does not copy financial settings. SDK checkpoints use the core cadence and retry behavior. These native tests exercise lifecycle hooks without submitting transactions; the repository evidence runner tests chain behavior separately.

`seam_demo.py` and `run_onchain.py` remain scope demonstrations using the patched final seam. They are separate from the native authorization tests and default to a demo directory under the home directory.
