# OpenClaw integration

A TypeScript plugin observes OpenClaw tool invocations and native approvals and forwards them to a local Python sidecar (`aa_helper/`) that runs the SDK. Python owns record encoding, hashing, predicate evaluation, and chain calls.

## Patch

The plugin requires OpenClaw 2026.6.9 at upstream commit `89c90210fb90c3c1d1bd54d56cd7be00e59aeed4` with the patch in `patches/`, which adds the observation hook. From a checkout of that commit, with Node 24:

```sh
git apply /path/to/this/repo/integrations/openclaw/patches/native-observation.patch
pnpm install --frozen-lockfile --ignore-scripts
node scripts/run-vitest.mjs src/agents/tool-observation.test.ts src/agents/aa-accountability.seam.e2e.test.ts src/agents/bash-tools.exec-host-gateway.test.ts
```

## Plugin and sidecar

From this repository's root:

```sh
PYTHONPATH=packages/commons:packages/sdk:packages/store:packages/verifier python -m pytest -q integrations/openclaw/tests
node --test integrations/openclaw/plugin/tests/hooks.test.ts
PYTHONPATH=packages/commons:packages/sdk:packages/store:packages/verifier uvicorn app:app --app-dir integrations/openclaw/aa_helper --port 8799
```

Build the plugin with `pnpm build` in `plugin/` and install it through OpenClaw's plugin loader. Set `party` to the covered user's address.

## Behavior

- An allow-once decision covers one invocation with the same tool and argument hash. A persistent approval needs a native policy-application event for each later invocation.
- Automated review is recorded as `auto_review` and does not count as human consent.
- The plugin opens the sidecar session before the first observed dispatch and refuses the call if opening fails.
- Node-host execution is refused while the observer is active, because it is outside the capture boundary.
