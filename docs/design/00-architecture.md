# Stage-1 Architecture & Design Decisions

> **Historical design log.** Records the early spine; some specifics have since moved on — e.g.
> the predicate catalog is now three numbered specs (AAP-1 consent *v2 / user consent*, AAP-2
> scope, AAP-3 mandate; see `../PROMISES.md`), and the demo agent dropped the component graph.
> The current truth is `../HOW_IT_WORKS.md`, `../RUNBOOK.md`, `../PROMISES.md`. Invariants below still hold.
>
> Status: proposed (pending review). Owner: prototype.
> Companion to the research notes in the `agent_accountability` repo
> (`walk_through`, `agent_assurance_memo_june_7`, `required_stakes`, `restaking`).

This document fixes the **formal spine** of the prototype. The guiding principle: the
agent is a disposable demo vehicle and *should* be simple; the rigor that lets this
**generalize** lives in the boundary objects — the trace schema, the predicate interface,
and the on-chain binding. Simplicity of the agent ≠ a toy system, as long as those
boundaries are typed and content-addressed.

---

## 0. Scope (stage 1)

**In:** objective promises only; honest-but-curious (HbC) provider; trusted centralized
verifier; plaintext trace store; escrow on Base Sepolia; end-to-end slash demo.

**Deferred (do not build now):** TEE/enclave trace sealing; subjective / LLM-judge path;
on-chain re-evaluation of predicates (the optimistic dispute backstop); correlated-loss /
pool-solvency pricing (a separate, already-drafted module); a promise DSL.

---

## 1. The system in one paragraph

A **commitment + adjudication game over an append-only action log.** A provider stakes a
bond and registers promises (predicates over traces). An agent runs sessions through the
SDK, which mediates and logs every consequential action into a trace. A party to a session
challenges a `(session, promise)` pair; a verifier computes the predicate over that
session's trace and the escrow settles — slash + pay on a valid violation, forfeit the
challenge bond on an invalid one.

## 2. Three invariants (the spine)

- **I1 — Binding (no equivocation).** An on-chain promise binds unforgeably to *(predicate
  logic, parameters)* via content hashes; a session binds to its trace via a `traceHash`.
  The provider cannot later claim a different predicate meant something else, or swap the
  trace after a challenge.
- **I2 — Complete mediation.** The SDK is the *only* path through which the agent takes
  consequential actions ⇒ the trace is complete. *Assumed* under HbC in stage 1.
  **Precondition for objective checkability:** a **non-opaque action vocabulary** — no
  `execute_code`/`exec`-style tool. With it, detection `q = 1`; an opaque tool makes a
  violation *mediated but uncheckable* (`q < 1`), worsening as agent capability rises.
  See §12.
- **I3 — Deterministic adjudication.** A predicate is a *pure, deterministic* function of
  the canonical trace. The verifier and any future on-chain disputer compute the identical
  verdict from the identical bytes.

## 3. Formal objects & identifiers

| Object | Identity | Notes |
|---|---|---|
| Provider | Ethereum address | posts the bond |
| Predicate | `predicateHash = keccak(canonical(spec_id, version))` | pure fn; lives **once** in `core`, shared by SDK + verifier |
| Promise | `promiseId` (registry-assigned) → commits `(predicateHash, paramsHash, payout)` | `params` is **data** (JSON), not a language |
| Session | `sessionId` (uuid) → records `(provider, party, traceHash)` | `party` = the address allowed to challenge (privacy) |
| Trace | `traceHash = keccak(canonical(trace))` | flat hash now; Merkle root later for succinct on-chain dispute |
| Challenge | `challengeId` (escrow-assigned) → `(sessionId, promiseId, challenger, bond)` | challenger submits **no evidence**; verifier fetches the trace |
| Verdict | `(challengeId, violated: bool)` | submitted by the trusted verifier role |

**How a challenge is addressed:** by the pair **`(sessionId, promiseId)`** — the session
binds the trace, the promise binds the predicate. The challenger asserts; the verifier
proves. (This is the answer to the earlier "challenge by IDs?" question.)

## 4. The two core types (anti-toyish)

```python
# packages/commons — the single source of truth, imported by BOTH sdk and verifier.

@dataclass(frozen=True)
class ActionRecord:
    seq: int            # monotonic within a session
    session_id: str
    tool: str           # action name (the vocabulary must be non-opaque — see I2)
    args: dict          # canonicalized
    result: Any         # serializable (or a hash, if large/sensitive)
    ts: int             # epoch ms — MUST NOT affect a verdict unless the promise is about timing

# canonical(record) = JSON with sorted keys, no whitespace, fixed number format
# => traceHash is reproducible byte-for-byte across SDK and verifier (I3)

class Predicate(Protocol):
    spec_id: str        # stable id
    version: int
    def evaluate(self, trace: list[ActionRecord], params: dict) -> "Verdict": ...

Verdict = Literal["SATISFIED"] | Violation  # Violation(seq=..., reason=...)
```

A **promise = `(Predicate.spec_id@version, params, payout)`**. Its on-chain identity is
`predicateHash` + `paramsHash`; the verifier looks up the *same* function by hash. This is
how arbitrary objective promises generalize **without a DSL**.

## 5. The SDK — what it actually is

A **pip-installable Python library the provider imports into their agent process.** It must
run *in-process* with the agent — that is what makes complete mediation (I2) possible; a
chokepoint can only mediate what shares its address space. (Yes: "an npm package, but for
Python." Structured as a real package from day one; published to PyPI only when we want
external adopters — until then it installs from the repo.)

Two layers:

1. **Mediation core (pure, local, dependency-light):** the `execute()` chokepoint, the
   trace structure, canonical serialization, the predicate interface. Must be obviously
   correct; this is the part that generalizes.
2. **Protocol client (glue):** `web3` calls (register promise, post/top-up bond, commit
   `traceHash`) + an HTTP client that ships the trace to the store on session end.

**Public API (the entire surface ≈ 5 calls):**

```python
sdk = Accountability(provider_key, escrow_addr, store_url)
sdk.register_promise(predicate="no_destructive_without_approval",
                     params={"destructive_tools": ["delete_file"], "approval_tool": "request_approval"},
                     payout_wei=...)            # → promiseId (on-chain)
sdk.post_bond(amount_wei=...)
sess = sdk.start_session(party=user_addr)       # → sessionId; commits party on-chain
result = sess.execute(tool_name, args, tools)   # historical API: run + log
sess.end()                                       # canonicalize → ship trace → commit traceHash
```

The SDK is **tool-agnostic**: the provider declares which actions are consequential and
registers their handlers; the SDK mediates an abstract action, not any specific agent. That
is what lets the same SDK wrap our demo agent today and (later) Hermes via its plugin hook.

## 6. Promises: a predicate registry, NOT a DSL

Settled stance (from the research notes): **no DSL.** A DSL is premature abstraction, and
it is explicitly *not* the contribution. Instead:

- `core` holds a small registry of **versioned predicate functions**.
- A promise selects one by id and supplies **parameters as JSON**.
- On-chain binding is `keccak(predicate source/spec) = predicateHash` and
  `keccak(canonical(params)) = paramsHash`.

Generality comes from *parameterization + content-addressing*, not from a language. If
recurring patterns ever emerge, a DSL becomes a convenience layer over this registry — later,
and never the contribution.

**Stage-1 predicates (two):**

- `P1 no_destructive_without_approval(params: {destructive_tools, approval_tool})` — for each
  destructive call, a matching granted approval must appear earlier in the trace. (Ordering.)
- `P2 funds_only_to_allowlist(params: {send_tool, allowlist})` — the extractive case in this
  historical design. The current SDK records execution and supports settlement afterward;
  application permission checks remain separate.

## 7. On-chain escrow (Solidity · Foundry · Base Sepolia)

Minimal state:

```
bonds[provider]                          -> uint256                  (one bond, many promises)
promises[promiseId]                      -> {provider, predicateHash, paramsHash, payout, active}
sessions[sessionId]                      -> {provider, party, traceHash}
challenges[challengeId]                  -> {sessionId, promiseId, challenger, bond, status}
verifier                                 -> address (trusted role, stage 1)
```

Functions:

```
postBond() payable
registerPromise(predicateHash, paramsHash, payout) -> promiseId
commitSession(sessionId, party, traceHash)                       // SDK, at session end
challenge(sessionId, promiseId) payable -> challengeId           // require msg.sender == sessions[sessionId].party  (privacy)
submitVerdict(challengeId, violated)                             // only verifier
  // violated  -> pay challenger `payout` from bonds[provider]; refund challenge bond
  // !violated -> forfeit challenge bond to provider
```

**Stage-1 simplifications (each a labeled shortcut to remove later):**
- Verifier is a **trusted role** (centralized shortcut; later: optimistic dispute / TEE).
- Slash pays `payout` from the shared bond; **reverts if under-funded** — no correlated-loss
  / solvency math (that's the separate pricing module; out of scope).
- `party` is recorded at `commitSession` and gates `challenge` — the entire stage-1 privacy
  mechanism (later: a signed session receipt the party presents).
- `payout` is a single transfer (compensation + bounty combined; the challenger *is* the
  harmed party under the privacy rule).

~150 lines of Solidity. Generalizable (any promise via hashes), not bespoke to our agent.

## 8. Off-chain services

- **Trace store** — `FastAPI + SQLite`. Endpoints: `PUT /sessions/{id}` (SDK ships trace +
  metadata), `GET /sessions/{id}` (verifier fetches). The SDK talks **HTTP**, never the DB
  directly — the store is the protocol's neutral infra, the SDK is the provider's, and they
  meet at a boundary.
- **Verifier** — `Python + web3.py`. Watches the escrow for `Challenge` events → fetches the
  trace → checks `traceHash` → runs the predicate **from `core`** → `submitVerdict`.

## 9. Decisions on the open questions

| Question | Decision | Why (Karpathy = minimal / Ilya = right invariant) |
|---|---|---|
| Storage: SQL vs Neon? | **SQLite** (FastAPI) | Zero external deps, reproducible. A trusted prototype store needs nothing more. Swap to Postgres/Neon = one SQLAlchemy URL when concurrency/persistence demand it. Don't couple now. |
| Deploy: VPS vs Railway? | **localhost first**, Railway/Render when remote needed | Don't provision infra to prove a mechanism. The only inherently-remote piece is the chain (Base Sepolia). |
| Language: Python vs TS? | **Python off-chain; Solidity on-chain; Foundry tooling** | TS is *not* banned (it's fine for web3). The real constraint: the predicate must have **one** implementation, shared SDK↔verifier — so both are Python. Two implementations could diverge (violates I3). |
| Contract tooling | **Foundry** | Solidity-native tests, fast, no JS context-switch; the off-chain consumer is Python (web3.py) and just needs the ABI. (Hardhat/TS is a fine alternative if preferred.) |
| Promise DSL? | **No** — predicate registry + JSON params | Premature; not the contribution. §6. |
| Repo layout | **One monorepo** (§10) | Prototype velocity + single-source interfaces beat repo fragmentation. The SDK is already a clean package, so splitting it to PyPI later is cheap. |

## 10. Repo structure (monorepo)

```
agent-accountability-prototype/
├── packages/
│   ├── core/        # THE SPINE: ActionRecord, canonical serialization, Predicate iface,
│   │                #   predicate registry, identifier/hash helpers. (pip package)
│   ├── sdk/         # provider-side: execute() chokepoint, lifecycle, protocol client  (deps: core)
│   ├── verifier/    # event watcher + predicate runner + verdict submitter             (deps: core)
│   └── store/       # FastAPI + SQLite trace store
├── contracts/       # Foundry: Escrow.sol (bond, promise registry, challenge, settlement)
├── agent/           # demo agent (Anthropic SDK) wired with sdk + a violation scenario
├── docs/design/     # this doc + subsequent decision notes
└── scripts/         # end-to-end demo runner, deploy, wallet
```

`core` being shared by `sdk` and `verifier` is the structural guarantee behind I3 (one
predicate implementation).

## 11. Stage-1 acceptance demo

Provider posts a bond and registers `P1` + `P2`. The agent runs a session and is induced
(via task/prompt) to (a) `delete_file` without a prior approval and/or (b) `send_funds` to a
non-allowlisted address. The `party` challenges the `(session, promise)`. The verifier rules
**valid** → escrow slashes the bond and pays the challenger. A spurious challenge on a
satisfied promise rules **invalid** → the challenge bond is forfeited. The destructive action
is a **real, irreversible** effect (a real scratch-dir delete; a balance-moving funds stub) so
the slash maps to real harm — not a `print`.

## 12. The q-precondition (carry into the paper)

`agent_assurance_memo_june_7.md:63` assumes `q ≈ 1` "because covered actions pass through a
monitor before side effects," and compensation **and** deterrence both scale by `q`. The
engineering finding **"routes-through ≠ predicate-checkable"** is precisely the statement
that `q < 1` in general: an action inside an opaque `execute_code` tool is *mediated* (logged)
yet *evades* a name-keyed predicate. So:

> **Complete mediation + a non-opaque action vocabulary ⟹ objective promises are decidable
> (`q = 1`). An opaque/code-exec tool breaks this even though the action is logged.**

The demo agent has no opaque tool ⇒ `q = 1` by construction. This is stated as a **precondition**,
not assumed silently. (Hermes' `execute_code` is where the obstacle is later *exhibited*;
out of stage-1 scope.) Refinement of Saltzer–Schroeder complete mediation,
not a new theorem; the quantitative contribution remains the capital decomposition.
