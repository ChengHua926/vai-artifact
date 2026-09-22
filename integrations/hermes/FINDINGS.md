# Hermes: measured binding and limits

This revision replaces the earlier plugin-only scope demonstration. Native consent capture now requires a pinned Hermes source patch. The exact base revision, patch hash, and source paths are recorded in [upstream.json](upstream.json). Historical statements that the integration made no source edits or emitted no consent records no longer describe this implementation.

## What the patch adds

- A final `tool_dispatch` wrapper inside Hermes's existing execution-middleware path. It receives the effective arguments after ordinary middleware rewriting and rejects argument replacement at the final seam.
- Request IDs and decision provenance on the existing native approval pre/post hooks, plus an observation when a stored approval policy is actually reused. CLI and gateway retain their existing approval behavior.
- ACP command responses preserve the native option, request ID, session ID, and timeout. ACP structured-edit approval emits the same observations and runs inside the final wrapper, before mutation.
- Native pre-execution edit/code denial results explicitly state `executed: false`. The adapter translates only an explicit native nonexecution marker (`executed: false`, `blocked`, or `pending_approval`) into the SDK's blocked result. An ordinary error does not prove that no effect occurred.
- Refusing an argument rewrite at the immutable final seam also returns `executed: false`, because the handler has not run. A test confirms that an ordinary exception after an actual write remains an error outcome subject to the authorization predicate.

The Python adapter joins these observations only within an active invocation with matching native IDs and exact arguments or command text. It records native request/response evidence and a normalized authorization before the action outcome. It never turns a shell command into a structured file deletion or a broad path grant.

## What runs in the native tests

The tests call real `model_tools.handle_function_call`, Hermes's plugin manager and middleware chain, native command guards, ACP requester/callback bridges, native file/terminal handlers, SDK recording, and AAP-1. The plugin callbacks are registered directly into the live manager; plugin discovery and a full model-driven conversation are not tested.

Controlled inputs are user decisions, ACP transport responses, and automated-review verdicts. The optional external command scanner is replaced with an allow response. Shell commands operate only on disposable fixtures. Assertions compare recorded decisions and action IDs with actual file mutation or nonmutation.

The cases cover allow-once followed by an identical denied call and an identical completed call without approval; session/permanent policy reuse for actual terminal invocations; CLI once/session/always/deny; ACP command and edit timeout; automated approve/deny and reused automated policy; effective arguments after another middleware rewrites them; concurrent ACP sessions; nested sessions; turn continuation; and finalization during an active invocation. [TEST_RESULTS.md](TEST_RESULTS.md) separates integration cases from upstream regressions.

Additional regressions cover explicit party mismatch on session reuse, default-binding retention after failed finalization, and typed refusal at the immutable final seam without an AAP-1 fault. The finalization test injects a failure inside the real SDK finalization path and retries through the adapter.

Cross-thread regressions cover deferred finalization performed by the last active worker. The caller's default lookup drops the closed binding and restores any outer session. A pending or failed finalization retains its binding for retry.

## Boundary

| Path or claim | Evidence and limit |
|---|---|
| Structured `write_file` through native dispatch | Actual mutation, approval, and outcome are tested. AAP-1 checks the exact invocation; AAP-2 checks the declared path lexically. |
| Native `terminal` approval | Actual command execution and policy reuse are tested. The trace records command arguments, not all resulting effects. |
| `execute_code` | The native whole-script approval path emits observations where Hermes invokes it. Upstream guard regressions run; no full code-sandbox execution is claimed by these integration tests. Nested terminal calls do not become authorization for the outer script. |
| Lifecycle | Native hooks are invoked directly, including concurrent/nested ownership and delayed finalization. The model conversation loop is not run. Turn checkpoints use the SDK; native tests make no chain calls. |
| Other agent-loop entry points | The generic middleware helper contains the final seam. Exercising `handle_function_call` does not establish complete coverage of every gateway, subagent, MCP, or cron path. Each also needs an accountability session binding. |
| Direct registry dispatch, plugin helper bypasses, background effects | Calls that bypass the seam remain outside the recorded surface. |
| Tool faithfulness | A handler can perform effects absent from its arguments/result. The adapter does not independently attest handler behavior. |

A scoped path is not a resolved filesystem boundary. AAP-2 can normalize traversal syntax but cannot resolve symlinks from a trace alone. Runtime filesystem isolation is needed for semantic containment. The historical `seam_demo.py` filesystem snapshot can detect an out-of-trace mutation locally; the chain verifier cannot detect an effect absent from its submitted evidence.

This integration establishes an executable binding over tested native paths. It does not establish complete mediation of Hermes, automatic human consent for every native allow, or semantic interpretation of opaque shell/code effects.
