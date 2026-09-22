# OpenClaw integration findings

## Current evidence

The pinned native patch adds an observation hook while keeping native approval decisions with their existing owners. It rejects unsupported node-host exec before execution. It carries an invocation ID through generic plugin approval, gateway exec approval, and actual completion. The Python sidecar records AAP-1 invocation evidence and AAP-2 scope evidence using the same canonical encoder as the verifier.

The upstream version, commit, patch digest, observed paths, and test commands are in `upstream.json`. These identifiers supersede the historical before-tool-call-only seam result.

The focused tests distinguish:

- a request pending versus an executed command;
- a human allow-once decision versus automatic review or timeout;
- one approved invocation versus a second unapproved invocation;
- identical transport replay versus conflicting evidence;
- separate native sessions, even when tool-call IDs repeat;
- session-end notification versus finalization after deferred completion;
- native security, input, elevation, and host refusals versus executed commands;
- exact-command durable grants versus matched segment grants;
- failed initial coverage opening versus an outage after the session opens.

## Scope and interpretation

The default consent example covers `exec`. It records the user's native decision for the entire command and its exact arguments. It does not translate shell text into a structured deletion promise. Generic plugin approval capture is also implemented and tested, while each registered promise still names the tools it covers.

A provider's durable native approval can authorize a later invocation only when OpenClaw reports the matched policy for that invocation. Raw scope remains evidence about native policy; the predicate never widens one record to every future call.

AAP-2 remains lexical containment of the recorded `write.path`. Path traversal is detected after normalization. Symlinks, opaque commands, side effects outside the tool seam, and dishonest recording remain outside that claim. AAP-5's optional count cap remains illustrative, without an empirical calibration claim.

## Integration effort

The implementation consists of a TypeScript transport plugin, a Python HTTP sidecar/adapter, and a pinned native observation patch. Logging through existing hooks and adding native consent capture are distinct integration costs. No model retraining is involved. Source and patch counts should be measured from the final committed revision, excluding tests.

The patch is required for native consent/completion fidelity; this is not a zero-source-change consent integration. Node-host exec is explicitly refused before execution while observation is active. Live model conversations, production client approval UX, and recovery after a sidecar/plugin process crash are unverified. The helper defaults to loopback and does not supply its own authentication layer.
