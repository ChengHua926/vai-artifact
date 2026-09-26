/**
 * Controlled OpenClaw recording-cost measurement, without a model or human wait.
 * The sole mocked dependency is the gateway approval response: native dispatch
 * still requests and consumes allow-once, and the built-in write tool performs
 * each filesystem mutation. The AA hooks use their real HTTP transport.
 */
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { expect, it, vi } from "vitest";
import {
  getGlobalHookRunner,
  initializeGlobalHookRunner,
  resetGlobalHookRunner,
} from "../../openclaw/src/plugins/hook-runner-global.js";
import { createMockPluginRegistry } from "../../openclaw/src/plugins/hooks.test-helpers.js";
import { wrapToolWithBeforeToolCallHook } from "../../openclaw/src/agents/agent-tools.before-tool-call.js";
import { createWriteTool } from "../../openclaw/src/agents/sessions/tools/write.js";
import type { AnyAgentTool } from "../../openclaw/src/agents/tools/common.js";
import {
  createAaHooks,
  type NativeContext,
  type Observation,
} from "../integrations/openclaw/plugin/src/hooks.js";

const gateway = vi.hoisted(() => vi.fn());
vi.mock("../../openclaw/src/agents/tools/gateway.js", () => ({
  callGatewayTool: gateway,
}));

type Job = {
  helper_url?: string;
  helper_timeout_ms?: number;
  party?: string;
  session_id: string;
  directory: string;
  writes: number;
  payload_bytes?: number;
  enabled: boolean;
  pause_after?: number;
  pause_seconds?: number;
  inter_write_seconds?: number;
  mode?: string;
  checkpoint_records?: number;
  checkpoint_seconds?: number;
  repetition?: number;
  warmup?: boolean;
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const sha256 = (data: Buffer | string) => createHash("sha256").update(data).digest("hex");

async function runJob(job: Job) {
  if (!Number.isSafeInteger(job.writes) || job.writes < 1) throw new Error("Invalid writes");
  if ((job.payload_bytes ?? 1024) !== 1024) throw new Error("Study payload must be 1 KiB");
  if (!path.isAbsolute(job.directory)) throw new Error("directory must be absolute");
  if (!job.session_id) throw new Error("session_id is required");
  if (job.enabled && (!job.helper_url || !job.party)) {
    throw new Error("Enabled jobs require helper_url and party");
  }
  if (job.helper_timeout_ms !== undefined &&
      (!Number.isSafeInteger(job.helper_timeout_ms) || job.helper_timeout_ms < 1 || job.helper_timeout_ms > 120_000)) {
    throw new Error("helper_timeout_ms must be an integer between 1 and 120000");
  }
  if (job.enabled && job.mode) {
    const response = await fetch(`${job.helper_url}/measurement/configure`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode: job.mode, checkpoint_records: job.checkpoint_records ?? 10, checkpoint_seconds: job.checkpoint_seconds ?? 30 }),
    });
    if (!response.ok) throw new Error(`Configure failed: HTTP ${response.status}`);
  }
  await fs.mkdir(job.directory, { recursive: true });
  const existing = await fs.readdir(job.directory);
  if (existing.length) throw new Error(`Measurement directory is not empty: ${job.directory}`);
  resetGlobalHookRunner();
  gateway.mockReset();
  const requested = new Set<string>();
  let approvalsRequested = 0;
  let approvalsResolved = 0;
  gateway.mockImplementation(async (method: string, _options: unknown, params: { id?: string; toolCallId?: string }) => {
    if (method === "plugin.approval.request") {
      const id = `${job.session_id}:approval:${params.toolCallId}`;
      requested.add(id);
      approvalsRequested++;
      return { id };
    }
    if (method === "plugin.approval.waitDecision" && params.id && requested.delete(params.id)) {
      approvalsResolved++;
      return { id: params.id, decision: "allow-once" };
    }
    throw new Error(`Unexpected gateway invocation: ${method}`);
  });
  const ctx = { sessionId: job.session_id, runId: `${job.session_id}:run`, cwd: job.directory, config: {} };
  const warnings: string[] = [];
  const observations: { phase: string; action_id: string; event_id: string; tool_call_id?: string; wallclock_ms: number }[] = [];
  const aa = job.enabled ? createAaHooks({
    helperUrl: job.helper_url!,
    party: job.party!,
    ...(job.helper_timeout_ms !== undefined ? {
      fetchImpl: (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) =>
        fetch(input, { ...init, signal: AbortSignal.timeout(job.helper_timeout_ms!) }),
    } : {}),
    warn: (message) => warnings.push(message),
  }) : undefined;
  const hooks: Parameters<typeof createMockPluginRegistry>[0] = [{
    hookName: "before_tool_call",
    pluginId: "measurement-approval-policy",
    handler: async () => ({ requireApproval: { title: "Write", description: "Allow this write?" } }),
  }];
  if (aa) {
    hooks.push(
      { hookName: "session_start", pluginId: "aa-accountability", handler: (event, context) => aa.session_start(event, context as NativeContext) },
      { hookName: "session_end", pluginId: "aa-accountability", handler: (event, context) => aa.session_end(event, context as NativeContext) },
      { hookName: "before_tool_call", pluginId: "aa-accountability", handler: (event, context) => aa.before_tool_call(event as { toolName: string; toolCallId?: string }, context as NativeContext) },
      { hookName: "tool_observation", pluginId: "aa-accountability", handler: (event, context) => {
        const observation = event as Observation;
        observations.push({ phase: observation.phase, action_id: observation.actionId, event_id: observation.eventId, tool_call_id: observation.toolCallId, wallclock_ms: Date.now() });
        return aa.tool_observation(observation, context as NativeContext);
      } },
    );
  }
  initializeGlobalHookRunner(createMockPluginRegistry(hooks));
  const runner = getGlobalHookRunner()!;
  const openingStart = performance.now();
  await runner.runSessionStart({ sessionId: job.session_id }, ctx);
  const sessionOpenMs = performance.now() - openingStart;
  const tool = wrapToolWithBeforeToolCallHook(createWriteTool(job.directory) as AnyAgentTool, ctx, { emitDiagnostics: false });
  const payload = "x".repeat(1024);
  const actions: { index: number; tool_call_id: string; path: string; started_wallclock_ms: number; ended_wallclock_ms: number; duration_ms: number }[] = [];
  let intentionalWaitMs = 0;
  const startedWallclockMs = Date.now();
  const start = performance.now();
  for (let index = 0; index < job.writes; index++) {
    const target = path.join(job.directory, `write-${index.toString().padStart(4, "0")}.txt`);
    const toolCallId = `${job.session_id}:write:${index}`;
    const actionStart = performance.now();
    const actionWallclock = Date.now();
    await tool.execute(toolCallId, { path: target, content: payload }, undefined, undefined);
    actions.push({ index, tool_call_id: toolCallId, path: target, started_wallclock_ms: actionWallclock, ended_wallclock_ms: Date.now(), duration_ms: performance.now() - actionStart });
    if (index + 1 === job.pause_after && (job.pause_seconds ?? 0) > 0) {
      const waitStart = performance.now();
      await sleep(job.pause_seconds! * 1000);
      intentionalWaitMs += performance.now() - waitStart;
    }
    if (index + 1 < job.writes && (job.inter_write_seconds ?? 0) > 0) {
      const waitStart = performance.now();
      await sleep(job.inter_write_seconds! * 1000);
      intentionalWaitMs += performance.now() - waitStart;
    }
  }
  const toolLoopMs = performance.now() - start;
  const closeStart = performance.now();
  await runner.runSessionEnd({ sessionId: job.session_id, messageCount: 0 }, ctx);
  const sessionEndMs = performance.now() - closeStart;
  const elapsedMs = performance.now() - start;
  const endedWallclockMs = Date.now();
  resetGlobalHookRunner();

  // All validation and hashes are outside the timing interval.
  let helperSnapshot: unknown;
  if (job.enabled && job.mode) {
    const response = await fetch(`${job.helper_url}/measurement/snapshot?native_session_id=${encodeURIComponent(job.session_id)}`);
    if (!response.ok) throw new Error(`Snapshot failed: HTTP ${response.status}`);
    helperSnapshot = await response.json();
  }
  const files = [];
  for (const action of actions) {
    const data = await fs.readFile(action.path);
    expect(data.byteLength).toBe(1024);
    expect(data.toString("utf8")).toBe(payload);
    files.push({ path: action.path, bytes: data.byteLength, sha256: sha256(data) });
  }
  expect(approvalsRequested).toBe(job.writes);
  expect(approvalsResolved).toBe(job.writes);
  expect(requested.size).toBe(0);
  expect(warnings).toEqual([]);
  expect(observations.length).toBe(job.enabled ? 4 * job.writes : 0);
  if (job.enabled) {
    for (let index = 0; index < job.writes; index++) {
      expect(observations.slice(4 * index, 4 * index + 4).map((event) => event.phase)).toEqual([
        "execution_started", "approval_requested", "approval_resolved", "execution_completed",
      ]);
    }
  }
  return {
    ...job,
    payload_bytes: 1024,
    session_open_ms_excluded: sessionOpenMs,
    tool_loop_ms: toolLoopMs,
    session_end_ms: sessionEndMs,
    elapsed_ms: elapsedMs,
    intentional_wait_ms: intentionalWaitMs,
    started_wallclock_ms: startedWallclockMs,
    ended_wallclock_ms: endedWallclockMs,
    approvals_requested: approvalsRequested,
    approvals_resolved: approvalsResolved,
    native_observations: observations.length,
    observations,
    actions,
    files,
    total_written_bytes: files.reduce((sum, file) => sum + file.bytes, 0),
    payload_sha256: sha256(payload),
    warnings,
    helper_snapshot: helperSnapshot,
  };
}

it("measures controlled native writes through actual AA HTTP hooks", async () => {
  const jobPath = process.env.AA_MEASUREMENT_JOB;
  const outputPath = process.env.AA_MEASUREMENT_OUTPUT;
  if (!jobPath || !outputPath) throw new Error("AA_MEASUREMENT_JOB and AA_MEASUREMENT_OUTPUT are required");
  const input = JSON.parse(await fs.readFile(jobPath, "utf8")) as Job | { jobs: Job[] };
  const jobs = "jobs" in input ? input.jobs : [input];
  const results = [];
  for (const job of jobs) {
    results.push(await runJob(job));
    // Persist after each completed session so an unrelated later failure cannot
    // erase a successful mainnet session's measurements.
    await fs.writeFile(outputPath, JSON.stringify({
      schema_version: 1,
      node_version: process.version,
      methodology: {
        write_tool: "OpenClaw createWriteTool with default filesystem operations",
        dispatch: "wrapToolWithBeforeToolCallHook and native global hook runner",
        approval: "Controlled gateway request/waitDecision responses, allow-once; no human wait",
        recording: "Production createAaHooks with real fetch to Python helper",
        excluded: ["module loading", "registry initialization", "session opening", "post-timing file validation"],
      },
      jobs: results,
    }, null, 2) + "\n");
  }
}, 3_600_000);
