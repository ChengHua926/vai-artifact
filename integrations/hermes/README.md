# Hermes integration

The adapter records each tool invocation's effective arguments, the native approval decisions, and the outcomes, and passes them to the SDK.

## Patch

The adapter requires Hermes commit `5e01a5dbf1b7bc0144d9057be706da1ea9f065c3` (version 0.16.0) with the patch in `patches/`. From a checkout of that commit:

```sh
git apply /path/to/this/repo/integrations/hermes/patches/hermes-5e01a5d-native-authorization.patch
```

The patch adds a final dispatch wrapper, which runs after argument-rewriting middleware and before native approval and dispatch, and a bridge for the ACP edit-approval path. `upstream.json` lists the patched files and the tested paths.

## Tests

With Hermes's dependencies plus `pytest==9.0.2`, `pytest-asyncio==1.3.0`, and `agent-client-protocol==0.9.0`, run from the Hermes checkout:

```sh
scripts/run_tests.sh /path/to/this/repo/integrations/hermes/tests/test_native_authorization.py -- -q --tb=short
```

The tests run Hermes's real file and terminal handlers against temporary fixtures with controlled user answers. They make no model or blockchain calls.

## Behavior

- Each invocation gets a new action ID. An allow-once approval never covers a later call, even with identical arguments.
- The adapter registers AAP-2 (scope) and AAP-1 (invocation authorization). `authorization_tools` selects the tools that need approval; the default is `write_file`, `patch`, `terminal`, and `execute_code`.
- Automated review is recorded as `auto_review` and does not count as human authorization.
- A turn end requests a checkpoint if one is due; finalization waits for active calls to finish.
