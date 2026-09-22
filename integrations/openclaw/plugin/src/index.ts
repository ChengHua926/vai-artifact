import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createAaHooks } from "./hooks.js";

export default definePluginEntry({
  id: "aa-accountability",
  name: "Agent Accountability",
  description:
    "Record native tool execution and authorization through the Python accountability sidecar.",
  configSchema: {
    jsonSchema: {
      type: "object",
      additionalProperties: false,
      properties: { party: { type: "string" }, helperUrl: { type: "string" } },
    },
  },
  register(api) {
    const cfg = (api.pluginConfig ?? {}) as { party?: string; helperUrl?: string };
    const hooks = createAaHooks({
      helperUrl: cfg.helperUrl ?? process.env.OPENCLAW_AA_HELPER_URL ?? "http://127.0.0.1:8799",
      party: cfg.party ?? process.env.OPENCLAW_AA_PARTY,
    });
    api.on("session_start", hooks.session_start);
    api.on("tool_observation", hooks.tool_observation);
    api.on("before_tool_call", hooks.before_tool_call);
    api.on("session_end", hooks.session_end);
  },
});
