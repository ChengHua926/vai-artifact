/** Native observations go to Python; this plugin never canonicalizes or hashes evidence. */
export type AaHookOptions = {
  helperUrl: string;
  party?: string;
  fetchImpl?: typeof fetch;
  warn?: (message: string) => void;
  retryDelayMs?: number;
};
export type NativeContext = { sessionId?: string; sessionKey?: string; runId?: string };
export type Observation = {
  phase: string;
  actionId: string;
  eventId: string;
  toolName: string;
  params: Record<string, unknown>;
  toolCallId?: string;
  requestId?: string;
  decision?: string | null;
  scope?: string;
  authority?: string;
  policyId?: string;
  origin?: unknown;
  result?: unknown;
};
type State = {
  opened: boolean;
  closing: boolean;
  closed: boolean;
  pending: Set<string>;
  rejected: Set<string>;
  outbox: { path: string; body: Record<string, unknown> }[];
  queue: Promise<void>;
  retry?: ReturnType<typeof setTimeout>;
};

export function createAaHooks(opts: AaHookOptions) {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const warn = opts.warn ?? ((message: string) => console.warn(`[aa-accountability] ${message}`));
  const sessions = new Map<string, State>();
  const key = (ctx: NativeContext) => ctx.sessionId ?? ctx.sessionKey;
  function state(id: string): State {
    let value = sessions.get(id);
    if (!value) {
      value = {
        opened: false,
        closing: false,
        closed: false,
        pending: new Set(),
        rejected: new Set(),
        outbox: [],
        queue: Promise.resolve(),
      };
      sessions.set(id, value);
    }
    return value;
  }
  async function post(path: string, body: Record<string, unknown>): Promise<boolean> {
    try {
      const response = await fetchImpl(`${opts.helperUrl}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(5000),
      });
      if (response.ok) return true;
      warn(`helper ${path}: HTTP ${response.status}`);
    } catch (error) {
      warn(`helper ${path}: ${String(error)}`);
    }
    return false;
  }
  function retry(id: string, s: State) {
    if (s.retry || s.closed) return;
    s.retry = setTimeout(() => {
      s.retry = undefined;
      s.queue = s.queue.then(() => drain(id, s));
    }, opts.retryDelayMs ?? 2000);
    s.retry.unref?.();
  }
  async function open(id: string, s: State) {
    if (s.opened) return true;
    if (s.closed || s.closing || !opts.party?.trim()) return false;
    s.opened = await post("/session", { party: opts.party, native_session_id: id });
    return s.opened;
  }
  async function drain(id: string, s: State) {
    if (s.closed || !s.opened) return;
    while (s.outbox.length) {
      if (!(await post(s.outbox[0].path, s.outbox[0].body))) {
        retry(id, s);
        return;
      }
      s.outbox.shift();
    }
    if (s.closing && s.pending.size === 0) {
      s.closed = await post("/end", { native_session_id: id });
      if (!s.closed) retry(id, s);
      else if (s.retry) clearTimeout(s.retry);
    }
  }
  function enqueue(ctx: NativeContext, update: (s: State, id: string) => void | Promise<void>) {
    const id = key(ctx);
    if (!id) {
      warn("missing native session identity; event is not recorded");
      return Promise.resolve();
    }
    const s = state(id);
    s.queue = s.queue.then(async () => {
      await update(s, id);
      await drain(id, s);
    });
    return s.queue;
  }
  return {
    session_start: (_event: unknown = {}, ctx: NativeContext = {}) =>
      enqueue(ctx, async (s, id) => {
        await open(id, s);
      }),
    async before_tool_call(event: { toolName: string; toolCallId?: string }, ctx: NativeContext) {
      const id = key(ctx);
      const s = id ? state(id) : undefined;
      if (s) await s.queue;
      const actionId = JSON.stringify([id, ctx.runId ?? "", event.toolCallId]);
      if (
        !id ||
        !event.toolCallId ||
        !s?.opened ||
        s.closed ||
        s.closing ||
        s.rejected.has(actionId) ||
        !s.pending.has(actionId)
      ) {
        return {
          block: true,
          blockReason:
            "Accountability coverage could not open before this invocation. Retry as a new invocation after the helper recovers.",
        };
      }
    },
    tool_observation(event: Observation, ctx: NativeContext) {
      return enqueue(ctx, async (s, id) => {
        if (s.closed) {
          warn(`late event ${event.eventId} for closed session ${id}`);
          return;
        }
        if (s.rejected.has(event.actionId)) return;
        if (event.phase === "execution_started") {
          if (s.closing || !(await open(id, s))) {
            s.rejected.add(event.actionId);
            warn(`coverage not opened for ${event.actionId}; this invocation must not execute`);
            return;
          }
          s.pending.add(event.actionId);
        }
        if (!s.opened) return;
        if (event.phase === "execution_completed") s.pending.delete(event.actionId);
        s.outbox.push({
          path: "/observation",
          body: {
            native_session_id: id,
            event_id: event.eventId,
            action_id: event.actionId,
            phase: event.phase,
            tool: event.toolName,
            args: event.params,
            request_id: event.requestId,
            decision: event.decision,
            scope: event.scope,
            authority: event.authority,
            policy_id: event.policyId,
            origin: event.origin,
            result: event.result,
          },
        });
      });
    },
    session_end: (_event: unknown = {}, ctx: NativeContext = {}) =>
      enqueue(ctx, (s) => {
        s.closing = true;
      }),
  };
}
