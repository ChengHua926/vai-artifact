import assert from "node:assert/strict";
import { test } from "node:test";
import { createAaHooks } from "../src/hooks.ts";

function helper() {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  let failEvent = false;
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname;
    const body = JSON.parse(String(init.body));
    calls.push({ path, body });
    return {
      ok: !(failEvent && path === "/observation"),
      status: failEvent ? 503 : 200,
    } as Response;
  }) as typeof fetch;
  return {
    calls,
    fetchImpl,
    fail: (value: boolean) => {
      failEvent = value;
    },
  };
}
const ctx = (sessionId = "s1") => ({ sessionId, runId: "r1" });
const event = (phase: string, actionId = "a1", extra = {}) => ({
  phase,
  actionId,
  eventId: `${actionId}:${phase}`,
  toolName: "exec",
  toolCallId: actionId,
  params: { command: "printf approved" },
  ...extra,
});

test("deferred execution keeps session open until the actual completion, then closes", async () => {
  const h = helper();
  const hooks = createAaHooks({ helperUrl: "http://h", party: "0xu", fetchImpl: h.fetchImpl });
  await hooks.tool_observation(event("execution_started"), ctx());
  await hooks.tool_observation(event("approval_requested", "a1", { requestId: "req1" }), ctx());
  await hooks.session_end({}, ctx());
  assert.equal(
    h.calls.some((c) => c.path === "/end"),
    false,
  );
  await hooks.tool_observation(
    event("approval_resolved", "a1", {
      requestId: "req1",
      decision: "allow-once",
      scope: "once",
      authority: "human",
    }),
    ctx(),
  );
  assert.equal(
    h.calls.some((c) => c.path === "/end"),
    false,
  );
  await hooks.tool_observation(event("execution_completed", "a1", { result: { ok: true } }), ctx());
  assert.equal(h.calls.at(-1)?.path, "/end");
});

test("concurrent native sessions route independently, including identical tool call ids", async () => {
  const h = helper();
  const hooks = createAaHooks({ helperUrl: "http://h", party: "0xu", fetchImpl: h.fetchImpl });
  await Promise.all([
    hooks.tool_observation(event("execution_started"), ctx("s1")),
    hooks.tool_observation(event("execution_started"), ctx("s2")),
  ]);
  await hooks.tool_observation(event("execution_completed", "a1", { result: "one" }), ctx("s1"));
  await hooks.session_end({}, ctx("s1"));
  await hooks.session_end({}, ctx("s2"));
  assert.deepEqual(
    h.calls.filter((c) => c.path === "/end").map((c) => c.body.native_session_id),
    ["s1"],
  );
});

test("failed event stays ahead of later observations and close; retry uses the same event id", async () => {
  const h = helper();
  const hooks = createAaHooks({
    helperUrl: "http://h",
    party: "0xu",
    fetchImpl: h.fetchImpl,
    warn: () => {},
  });
  await hooks.tool_observation(event("execution_started"), ctx());
  h.fail(true);
  await hooks.tool_observation(
    event("approval_resolved", "a1", {
      requestId: "q",
      decision: "deny",
      scope: "once",
      authority: "human",
    }),
    ctx(),
  );
  await hooks.tool_observation(
    event("execution_completed", "a1", { result: { blocked: true } }),
    ctx(),
  );
  await hooks.session_end({}, ctx());
  assert.equal(
    h.calls.some((c) => c.path === "/end"),
    false,
  );
  h.fail(false);
  await hooks.session_end({}, ctx());
  const obs = h.calls.filter((c) => c.path === "/observation");
  assert.equal(obs.at(-2)?.body.event_id, "a1:approval_resolved");
  assert.equal(obs.at(-1)?.body.event_id, "a1:execution_completed");
  assert.equal(h.calls.at(-1)?.path, "/end");
});

test("missing native identity records nothing instead of contaminating a default session", async () => {
  const h = helper();
  const hooks = createAaHooks({
    helperUrl: "http://h",
    party: "0xu",
    fetchImpl: h.fetchImpl,
    warn: () => {},
  });
  await hooks.tool_observation(event("execution_completed"), {});
  assert.equal(h.calls.length, 0);
});

test("outage retry resumes delivery and closes without another native event", async () => {
  const h = helper();
  const hooks = createAaHooks({
    helperUrl: "http://h",
    party: "0xu",
    fetchImpl: h.fetchImpl,
    warn: () => {},
    retryDelayMs: 2,
  });
  h.fail(true);
  await hooks.tool_observation(event("execution_started"), ctx());
  await hooks.tool_observation(event("execution_completed", "a1", { result: { ok: true } }), ctx());
  await hooks.session_end({}, ctx());
  h.fail(false);
  for (let i = 0; i < 30 && !h.calls.some((c) => c.path === "/end"); i++)
    await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(h.calls.at(-1)?.path, "/end");
});

test("initial open failure blocks dispatch and never replays the uncovered attempt", async () => {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  let available = false;
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname;
    const body = JSON.parse(String(init.body));
    calls.push({ path, body });
    return { ok: available, status: available ? 200 : 503 } as Response;
  }) as typeof fetch;
  const hooks = createAaHooks({
    helperUrl: "http://h",
    party: "0xu",
    fetchImpl,
    warn: () => {},
    retryDelayMs: 1,
  });
  const failedId = JSON.stringify(["s1", "r1", "failed"]);
  await hooks.tool_observation(
    event("execution_started", failedId, { toolCallId: "failed" }),
    ctx(),
  );
  assert.equal(
    (await hooks.before_tool_call({ toolName: "exec", toolCallId: "failed" }, ctx()))?.block,
    true,
  );
  available = true;
  await hooks.tool_observation(
    event("execution_completed", failedId, {
      toolCallId: "failed",
      result: { blocked: "no coverage" },
    }),
    ctx(),
  );
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(calls.filter((c) => c.path === "/session").length, 1);
  assert.equal(
    calls.some((c) => c.path === "/observation"),
    false,
  );
  const nextId = JSON.stringify(["s1", "r1", "next"]);
  await hooks.tool_observation(event("execution_started", nextId, { toolCallId: "next" }), ctx());
  assert.equal(
    await hooks.before_tool_call({ toolName: "exec", toolCallId: "next" }, ctx()),
    undefined,
  );
  assert.deepEqual(
    calls.filter((c) => c.path === "/observation").map((c) => c.body.action_id),
    [nextId],
  );
  await hooks.tool_observation(
    event("execution_completed", nextId, { toolCallId: "next", result: { ok: true } }),
    ctx(),
  );
  await hooks.session_end({}, ctx());
});
