# ClawsBench GLM-5.2 capture pipeline

This pipeline runs the env0 Standard60 tasks with:

- OpenClaw `2026.7.1` on Node `24.15.0`
- `openrouter/z-ai/glm-5.2`
- benchmark/domain skills off
- the exact official ClawsBench meta as OpenClaw `AGENTS.md` system context
- one rollout per task, no automatic retries or verifier feedback loop

Each rollout keeps BenchFlow ACP/provider artifacts, native untruncated
OpenClaw session files and export bundle, and env0 state/diff/action-log
snapshots before the agent, before the verifier, and after the verifier.

Dry-run:

```bash
bash agent_accountability_prototype/eval/clawsbench/capture/run_glm52_standard60.sh --dry-run
```

Preflight without starting a task:

```bash
bash agent_accountability_prototype/eval/clawsbench/capture/run_glm52_standard60.sh --preflight-only
```

Full resumable Standard60 run:

```bash
# from the repository's parent directory
bash agent_accountability_prototype/eval/clawsbench/capture/run_glm52_standard60.sh
```

Re-running the same command skips only capture-valid tasks under the immutable
run lock. A configuration change fails closed and requires a new run root.
The live run writes progress, cost coverage, logs, and canonical results under
`eval/clawsbench/corpus/`.

## Standard60 repair and seal

The 2026-07-20 run preserves every original attempt. Its repair pipeline:

- treats terminal/post-verifier differences as recorded phase annotations,
  not automatic capture failures;
- replays the deterministic `email-ambiguous-cleanup` verifier from hashed
  terminal captures;
- reruns only `multi-doc-embedded-override`, whose original Docker
  environment never started;
- disconnects OpenClaw before the repair rollout's terminal snapshot;
- writes a 60-task canonical selection without editing original rollouts.

Inspect the repair plan without Docker or OpenRouter:

```bash
bash agent_accountability_prototype/eval/clawsbench/capture/run_standard60_repair.sh --dry-run
```

Run or resume the one-task repair:

```bash
bash agent_accountability_prototype/eval/clawsbench/capture/run_standard60_repair.sh --run-missing
```

Seal after the repair succeeds:

```bash
bash agent_accountability_prototype/eval/clawsbench/capture/run_standard60_repair.sh --seal
```

The canonical outputs are `sealed-corpus.json` and `sealed-summary.json` in
`eval/clawsbench/corpus/`.

## Local evidence viewer

The viewer treats `sealed-corpus.json` as the only canonical task selector,
while retaining every attempt and source artifact:

```bash
cd agent_accountability_prototype/eval/viewer/app
npm run dev
```

Open `http://localhost:3050/?b=clawsbench`. Starting the viewer refreshes the
small ignored manifests under `eval/clawsbench/data/` when the sealed corpus is
available. The 581 MB raw corpus remains in place and is served on demand
through an allowlisted localhost API; it is never copied into `public/` or Git.

The deployed viewer falls back to tracked normalized manifests for all 60
canonical tasks when the local corpus is absent. Raw artifact bodies remain
local. Regenerate the tracked fallback from the sealed corpus in two steps:

```bash
cd agent_accountability_prototype
eval/.venv/bin/python -m eval.clawsbench.export
cd eval/viewer/app
node scripts/publish-clawsbench.mjs
```

To regenerate only the ClawsBench manifests:

```bash
cd agent_accountability_prototype
eval/.venv/bin/python -m eval.clawsbench.export
```

The viewer intentionally reports captured evidence without defining promises
or making coverage judgments. ACP events and Env0 API logs remain separate
layers because the capture has no stable call ID joining them.
