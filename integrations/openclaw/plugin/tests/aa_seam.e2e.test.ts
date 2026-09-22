import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  initializeGlobalHookRunner,
  resetGlobalHookRunner,
} from "../plugins/hook-runner-global.js";
import type { PluginHookToolObservationEvent } from "../plugins/hook-types.js";
import { createMockPluginRegistry } from "../plugins/hooks.test-helpers.js";
import { wrapToolWithBeforeToolCallHook } from "./agent-tools.before-tool-call.js";
import { createExecTool } from "./bash-tools.exec.js";
import type { AnyAgentTool } from "./tools/common.js";

const gateway = vi.hoisted(() => vi.fn());
vi.mock("./tools/gateway.js", () => ({ callGatewayTool: gateway }));

describe("accountability observations from native generic approval dispatch", () => {
  let events: PluginHookToolObservationEvent[];
  beforeEach(() => {
    events = [];
    gateway.mockReset();
    initializeGlobalHookRunner(
      createMockPluginRegistry([
        {
          hookName: "before_tool_call",
          pluginId: "approval-policy",
          handler: async () => ({
            requireApproval: { title: "Write", description: "Allow this write?" },
          }),
        },
        {
          hookName: "tool_observation",
          handler: async (event) => {
            events.push(event as PluginHookToolObservationEvent);
          },
        },
      ]),
    );
  });
  afterEach(resetGlobalHookRunner);
  it.each(["allow-once", "allow-always", "deny", null])(
    "records native decision %s and only runs allowed effects",
    async (decision) => {
      gateway.mockResolvedValueOnce({ id: "q1" }).mockResolvedValueOnce({ id: "q1", decision });
      const effects: unknown[] = [];
      const tool = wrapToolWithBeforeToolCallHook(
        {
          name: "write",
          execute: async (_id: string, args: unknown) => {
            effects.push(args);
            return { content: [], details: { ok: true } };
          },
        } as AnyAgentTool,
        { sessionId: "native-s1", runId: "r1" },
        { emitDiagnostics: false },
      );
      try {
        await tool.execute(
          "call1",
          { path: "workspace/a", content: "hello" },
          undefined,
          undefined,
        );
      } catch {
        /* native denial is an error result */
      }
      const allowed = decision === "allow-once" || decision === "allow-always";
      expect(effects).toHaveLength(allowed ? 1 : 0);
      expect(events.map((e) => e.phase)).toEqual([
        "execution_started",
        "approval_requested",
        "approval_resolved",
        "execution_completed",
      ]);
      expect(events[2]).toMatchObject({
        requestId: "q1",
        decision: decision ?? "timeout",
        scope: decision === "allow-always" ? "persistent" : "once",
        authority: "human",
        params: { path: "workspace/a", content: "hello" },
      });
      expect(new Set(events.map((e) => e.actionId)).size).toBe(1);
      if (!allowed) expect(events[3].result).toHaveProperty("blocked");
    },
  );
});

describe("native exec prelaunch nonexecution", () => {
  afterEach(resetGlobalHookRunner);
  it.each(["security", "input", "elevation", "host", "node"])(
    "records %s refusal without executing or fabricating an effect",
    async (kind) => {
      const events: PluginHookToolObservationEvent[] = [];
      gateway.mockReset();
      initializeGlobalHookRunner(
        createMockPluginRegistry([
          {
            hookName: "tool_observation",
            handler: async (event) => {
              events.push(event as PluginHookToolObservationEvent);
            },
          },
        ]),
      );
      const dir = fs.mkdtempSync(path.join(os.tmpdir(), "aa-refused-"));
      try {
        const target = path.join(dir, "effect");
        const tool = wrapToolWithBeforeToolCallHook(
          createExecTool({
            host: kind === "node" ? "node" : "gateway",
            security: kind === "security" ? "deny" : "full",
            ask: "off",
            cwd: dir,
          }),
          { sessionId: `refused-${kind}`, runId: "r" },
          { emitDiagnostics: false },
        );
        const args =
          kind === "input"
            ? {}
            : {
                command: `printf attempted > ${target}`,
                ...(kind === "elevation" ? { elevated: true } : {}),
                ...(kind === "host" ? { host: "invalid-host" } : {}),
              };
        await expect(tool.execute(`call-${kind}`, args, undefined, undefined)).rejects.toThrow();
        expect(fs.existsSync(target)).toBe(false);
        expect(gateway).not.toHaveBeenCalled();
        expect(events.map((e) => e.phase)).toEqual(["execution_started", "execution_completed"]);
        expect(events.at(-1)?.result).toMatchObject({
          blocked: { code: kind === "node" ? "unsupported-exec-host" : "exec-prelaunch" },
        });
        if (process.env.AA_TEST_OBSERVATIONS)
          fs.appendFileSync(
            process.env.AA_TEST_OBSERVATIONS,
            JSON.stringify({ native_session_id: `refused-${kind}`, events }) + "\n",
          );
      } finally {
        fs.rmSync(dir, { recursive: true, force: true });
      }
    },
  );
});
