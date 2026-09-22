# OpenClaw integration

The adapter observes OpenClaw tool invocations and native approvals. A TypeScript plugin forwards observations to a Python sidecar. Python owns record encoding, argument hashes, trace hashes, predicate evaluation, and chain calls. Keeping the sidecar avoids a second canonical encoder that could disagree with the SDK.

The tested upstream version and full commit are in `upstream.json`. The observation hook requires the pinned patch in `patches/`, also published as fork commit [`84f693f005fd0f0beb1f99c97b24c6e12d21e2e6`](https://github.com/openclaw/openclaw/commit/84f693f005fd0f0beb1f99c97b24c6e12d21e2e6). This is an experimental integration patch, not a claim that upstream OpenClaw ships this hook.

## Capture boundary

- `agent-tools.before-tool-call.ts` starts the invocation before native policy checks and observes generic plugin approvals. It records the exact effective arguments at dispatch.
- `bash-tools.exec-host-gateway.ts` observes native gateway exec requests and decisions. Both inline and deferred approval paths are covered. A matched durable approval emits a policy application for the current invocation, with the selected native policy entries.
- `bash-tools.exec.ts` distinguishes known prelaunch refusals from foreground/background process completion. Returning `approval-pending` or `running` does not imply completion. The patched observer rejects node-host exec before execution because that host is outside the capture boundary.
- Native-session ID, run ID, and tool-call ID bind observations to an invocation. Separate sessions have separate queues and Python bindings.

An allow-once decision covers one invocation with matching tool and argument hash. Raw native decisions, origin, and once/persistent scope remain in the evidence. A persistent approval does not become a wildcard: a later invocation needs a native policy-application observation. Automatic review is recorded as `auto_review`; it does not count as human consent. The adapter does not infer file deletions or targets from shell text.

The default HTTP session registers AAP-1 for `exec` and AAP-2 for `write.path`. Set `AA_CONSENT_TOOLS` to the explicit comma-separated list of tools requiring consent. Chained sessions use `scripts/_config.py`'s payout; `AA_PAYOUT_WEI` overrides it before session creation. These are chosen example promises. Capturing a generic tool approval does not automatically declare every generic tool to require approval. Providers must register the tools their promise covers.

## Transport and lifecycle

The plugin opens a sidecar session before the first observed dispatch. If opening fails, its native before-tool hook refuses that invocation. It drops the refused invocation’s observations instead of replaying them into later coverage. A new invocation can retry after the helper recovers. Configure `party` with the covered user's address before starting; the adapter does not implement wallet onboarding. `helperUrl` defaults to `http://127.0.0.1:8799`.

`POST /session` takes `party` and `native_session_id`. `POST /observation` takes the native phase, stable event and invocation IDs, tool, arguments, and available decision/result fields. `POST /end` takes `native_session_id`.

After a session opens, failed observation deliveries stay queued in order and retry with the same event ID. Store/checkpoint outages after opening do not block execution. Python rejects conflicting replay and ignores identical replay. A native session-end event requests finalization; unresolved approvals/processes and queued evidence keep it open. The last deferred completion allows finalization. The sidecar also refuses an early close, and the SDK refuses to commit unshipped records.

Queues and routing state are process-local. Automatic retries cover a running process's transport outage, not a host crash. The trace store receives records progressively. On-chain checkpoint cadence and store retry behavior belong to the shared Python SDK. A checkpoint cannot recover an observation lost before delivery.

Keep the helper on loopback or behind an authenticated private transport. This prototype does not add an HTTP authentication layer. Provider custody keeps traces off the public chain; an authorized verifier still reads the evidence.

## Reproduce

The personal fork contains the patch on branch `research/accountability-2026-09-08` and tag `accountability-2026-09-08`:

```sh
git clone --depth 1 --branch accountability-2026-09-08 https://github.com/openclaw/openclaw.git openclaw
git -C openclaw rev-parse HEAD
```

The reported HEAD must be `84f693f005fd0f0beb1f99c97b24c6e12d21e2e6`. This checkout already includes the patch. [Published fork verification](../PUBLISHED_FORKS.md) records the exact parents, tree comparisons, and fresh tests. Install the pinned checkout's dependencies and run the commands below, omitting `git apply` for the published fork.

Alternatively, check out the full upstream commit from `upstream.json`, then apply the patch from the upstream root:

```sh
git apply --check /path/to/prototype/integrations/openclaw/patches/native-observation.patch
git apply /path/to/prototype/integrations/openclaw/patches/native-observation.patch
pnpm install --frozen-lockfile --ignore-scripts
node scripts/run-vitest.mjs src/agents/tool-observation.test.ts src/agents/aa-accountability.seam.e2e.test.ts src/agents/bash-tools.exec-host-gateway.test.ts
```

Use Node 24 and the upstream lockfile's package manager. The native tests exercise the approval owners and dispatch with model calls disabled. The real gateway test is `src/agents/bash-tools.exec-gateway-approval.e2e.test.ts`; it starts an isolated loopback gateway and runs `printf` after a native approval.

From the prototype root:

```sh
PYTHONPATH=packages/commons:packages/sdk:packages/store:packages/verifier .venv/bin/python -m pytest -q integrations/openclaw/tests
node --test integrations/openclaw/plugin/tests/hooks.test.ts
PYTHONPATH=packages/commons:packages/sdk:packages/store:packages/verifier .venv/bin/uvicorn app:app --app-dir integrations/openclaw/aa_helper --port 8799
```

The plugin uses the current typed `api.on` hooks and `openclaw/plugin-sdk/plugin-entry` import. Build its ESM entry with `pnpm build` from `plugin/`, then install it through OpenClaw's normal plugin loader. The stock unpatched version cannot provide the new observation hook.

`seam_test.py` and `run_onchain.py` remain deterministic scope examples. They feed explicit events into the Python adapter; they do not run a model or prove a complete native deployment.

## Limits

The integration covers gateway/local exec; it explicitly rejects node-host exec before execution while the observer is active. Unpatched or inactive-observer execution retains upstream behavior. Native shell approvals authorize a command invocation, not its inferred filesystem effects. A provider can bypass the integration or falsify observations; the patch does not establish complete mediation. No live model, production chat client, or persistent multi-process sidecar recovery is claimed by the local tests.
