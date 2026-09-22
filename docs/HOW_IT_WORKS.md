# How it works

A prototype of **economic accountability for AI agents**. You can't make an LLM agent provably
safe, so don't try to prevent every violation — make a broken promise *expensive*. A provider
stakes a bond behind behavioral promises; the affected party challenges a violation; a verifier
rules on a logged record of what the agent did; if the promise was broken, the bond is slashed and
the challenger paid. This document is how the pieces actually work, from first principles.

## 1. The whole thing in one breath

> A **promise** is a predicate over a **trace**. The agent's actions are logged into the trace
> through one chokepoint (the **SDK**). The promise, its parameters, and the trace are each pinned
> on-chain by a hash. A **challenge** names a `(session, promise)` pair; the **verifier** re-runs
> the predicate over the trace and the **escrow** slashes or forfeits. Nothing else is load-bearing.

Three invariants hold the design together:

- **I1 — Binding.** A promise is pinned to its exact logic + parameters by a hash; a session to its
  exact trace by a hash. No one can later argue a promise meant something else or swap the trace.
- **I2 — Complete mediation.** The SDK is the only path the agent acts through, so the trace is
  complete. (Assumed in this prototype; the provider runs the real SDK.) It requires a *non-opaque
  action vocabulary* — an `exec("anything")` tool would launder a violation past the predicate.
- **I3 — Deterministic adjudication.** A predicate is a pure function of the canonical trace, so the
  SDK and the verifier compute the *identical* verdict from the identical bytes.

## 2. A trace, and a promise

A trace is an append-only list of action records — exactly what crossed the boundary:

```python
ActionRecord(seq=2, session_id="0x7aeb…", tool="delete_file",
             args={"target": "workspace/stale.log"}, result="deleted …", ts=…)
```

A promise is a **pure predicate over that list** — a plain function, not a DSL:

```python
def evaluate(trace, params) -> Verdict:        # AAP-1  no_destructive_without_consent
    consented, active_scopes = set(), set()
    for r in sorted(trace, key=lambda x: x.seq):
        if r.tool == "user_consent" and r.result == "granted":
            consented.add(r.args.get("target"))              # per-action user consent
        elif r.tool == "user_grant" and r.result == "granted":
            active_scopes.add(r.args.get("scope"))           # scoped standing grant
        elif r.tool == "user_revoke":
            active_scopes.discard(r.args.get("scope"))
        elif r.tool in params["destructive_tools"] and not is_blocked(r):
            t = r.args.get("target")
            if t not in consented and not any(t.startswith(s) for s in active_scopes):
                return Verdict.violation(r.seq, "destructive action without user consent")
    return Verdict.satisfied()
```

A **promise = `(predicate_id, params, payout)`**. `predicate_id` selects one of a small catalog of
audited predicates (`aa_commons.registry`); `params` configures it (which tools are destructive, the
allowlist, the budget). That's the "syntax" — data, not code you write. Generality comes from
parameterization, not from a language. The catalog is shared and neutral, which is the point of §6.
Each entry is a numbered, versioned spec (`AAP-1`…`AAP-3`, see [`PROMISES.md`](PROMISES.md));
`python scripts/catalog.py` prints the live index and the exact source each on-chain hash commits to.

## 3. The SDK: one function

The SDK is a library the provider imports *into the agent's process* (it has to share the address
space to mediate it). Its surface is ~5 calls; the star is `guard`, the chokepoint every
consequential action flows through:

```python
acc = Accountability(provider_id, store, chain, provider_addr)
pid = acc.register_promise("no_destructive_without_consent", {...}, payout, reserve_wei)  # validates params; born funded
sess = acc.session(party=user_addr)          # openSession on-chain first; sess.receipt is the party's coverage proof
result = sess.guard(tool, args, executor)   # ← run the tool AND log {seq,tool,args,result,ts}; record streams to the store
sess.grant("user_grant", {...})              # user path (harness code): consent/grant/revoke records the agent never emits
sess.end()                                   # flush, read the store's records back, commitTrace(their hash) on-chain
```

`guard` is the entire integration boundary and the entire mediation guarantee: it runs the tool and
appends the record. Complete mediation (I2) then holds whenever the provider routes every effect
through `guard` — assumed here under honest-but-curious, not enforced (an `exec`-style tool would
bypass it).

**The adapter.** Any agent framework has a point where it dispatches a tool. To adopt the SDK you
wire that point to `guard` — that's the whole "adapter." In our agent it's literally the loop:

```python
for block in llm_response.tool_use_blocks:
    out = guarded_action(sess, components, block.name, block.args,
                         lambda: TOOLS[block.name](**block.args))   # enforce (§6) then guard
```

A different framework (e.g. a plugin-based one) writes a few-line shim that calls `guard` at its
dispatch hook. The promise/predicate layer never changes — it operates on the trace, not the agent.
This is also where the generality claim is honest *and* bounded: an agent whose effects don't pass
through one interceptable layer (scattered side effects, an `exec` tool) can't be completely
mediated — a stated limitation, not a bug.

## 4. Hashing: commit cheap, reveal later

A hash is a short, unforgeable name for arbitrary data. We use keccak256 (the EVM's hash) over a
*canonical* JSON encoding (sorted keys, no whitespace) so the same object always hashes identically
across machines and languages. Three uses, one idea:

- **predicateHash** = keccak(the predicate's *source*) — pins the exact logic, so a stranger can
  audit what a promise checks from the chain hash + the open source, and any change to the predicate
  changes the hash (meaning can't drift silently). This is what makes it an *ERC-like standard*, not
  a private label.
- **paramsHash** = keccak(params) — pins *the exact allowlist / scope / budget*.
- **traceHash** = keccak(canonical(trace)) — pins *the exact log* of a session.

Only the 32-byte hashes go on-chain (the trace is far too big). The data lives off-chain in the
store. When a challenge comes, the verifier fetches the data and **recomputes the hashes**: if they
match the on-chain commitments, the data is provably the committed data — so a merely-trusted store
can hold information it *cannot forge or swap*. That is invariant I1, made operational.

## 5. The verifier: the neutral judge

A small service that watches the escrow for `Challenged(session, promise)` events. For each one:

1. Read the on-chain `traceHash`, `predicateHash`, `paramsHash`.
2. Fetch the session's records (the trace) + the params from the store.
3. **Binding checks** — recompute the record and params hashes and compare to the *on-chain*
   values (never the store's self-reported fields). Anything that fails — store unreachable,
   payload malformed, either hash off — is the one "committed data not produced" case: no verdict
   while the provider is silent (`claimDefault` is the challenger's remedy), a violation once the
   provider has responded on chain and still cannot produce.
4. Run the predicate the on-chain `predicateHash` names — **imported from the same `aa_commons`
   the SDK uses**, so there is exactly one implementation and no drift (I3) — and submit the
   verdict. A hash that names no catalog predicate rules against the provider.

The escrow does the rest atomically: on a valid violation it slashes the provider's bond and pays
the challenger (compensation + bounty + bond refund); on an invalid one it forfeits the challenger's
bond to the provider (anti-spam). The verifier is a *trusted role* in this prototype — the
centralized shortcut to remove later via an optimistic challenge or a TEE.

## 6. Components, correlated failure, and the price of a promise (`q`)


mostly just the economics part of the system, this section, can safely ignore if not working on the economics.
An agent doesn't keep a promise by good intentions; specific **components** enforce it — a consent
gate, a scope filter, a shared runtime. The research model (`restaking.md`) abstracts this: the
agent is a set of promises and a set of components; an edge **`component → promise`** means that
component enforces that promise; **a promise holds only if every component on it holds.** A
component shared by several promises *couples* them — one failure breaks all of them.

```
runtime_hub   -> consent, scope      (shared hub: couples both)
consent_gate  -> consent
scope_filter  -> scope
```

This is where **correlated failure** comes from, and why sizing the bond is the interesting problem:
split promises across *independent* components and their failures rarely coincide, so the required
bond is a fraction of the summed payout caps; route them through *one shared* component and a single
failure can take the whole book. *Architecture creates the correlation.* The stake-sizing math is
the paper's separate module; this prototype does not compute it.

Enforcement lowers how often a promise breaks, but the bond only bites if a break becomes a
*provable* claim — call that probability `q`. Self-issued "consent" (the agent rubber-stamping
itself) drives `q ≈ 0`: even a delete the user never authorized still shows a consent record in the
trace, so the violation never becomes a claim and the bond prices nothing. Logging the *user's*
decision flips it — an unauthorized delete leaves no covering record, the predicate fires, and the
claim lands, so `q` is high. That is the difference between proving "the gate ran" and proving
accountability, and it is why AAP-1 logs the user's decision (§2).

**The simplified demo does not instantiate this component graph** — it's a plain agent; the model
above is for the stake-sizing analysis, not the demo. Concretely: the provider *enforces* consent
(AAP-1) by honoring standing grants and otherwise asking the user; a misbehaving build that skips
that step is the failure the bond covers. Scope (AAP-2) is checked after execution — a naive agent
that wanders outside `workspace/` is just caught by the predicate on challenge. Either way `guard`
logs everything, so a failure is an *enforcement* failure (logged, challengeable), never a
*mediation* failure (which would erase the evidence).

## 7. The escrow, and what's trusted

`Escrow.sol` holds only money + commitments. Promises are born funded (`registerPromise` is
payable; the reserve must cover a payout), topped up with `fundPromise`, and retired with a
cooldown before `withdrawReserve`. Sessions commit in two phases, `openSession` at start and
`commitTrace` at end; a challenge (payable, party only) is accepted while the promise is in force
and the session's window is open. Availability is attributed rather than assumed: the provider can
`respond`, silence past 3 days lets anyone `claimDefault`, and `withdrawChallenge` returns a bond
that can no longer be paid. The non-obvious guards, each closing a real attack:

- **Binding** — `challenge` requires `promise.provider == session.provider`, or a challenger could
  register their own high-payout promise and drain an unrelated provider on a genuine trace.
- **One slash per `(session, promise)`, one live challenge per pair** — a violation can't be
  challenged repeatedly, and a duplicate filing can't pin the reserve.
- **No exit ahead of a claim** — retirement starts a cooldown and an open challenge locks the
  reserve, so a provider can't watch the public `Challenged` event and race the money out.

Trusted in this prototype (all deferred, all named): the verifier role; the store (made
tamper-evident by the hashes); the provider runs the real SDK (honest-but-curious). Out of scope:
TEE-sealed traces, the subjective/LLM-judge path, on-chain re-execution of the predicate, a
withdrawal timelock, and the stake-sizing math (a separate module — this prototype *exhibits*
coupling; it does not price it).

## 8. End to end, with our agent

```
start_demo.py   deploy escrow on anvil, post bond, register AAP-1 consent + AAP-2 scope, open the REPL.
you> /grant workspace/tmp/             → standing consent: auto-accept deletes under tmp/ (user_grant logged).
you> delete the scratch log in tmp     → under tmp/, covered by the grant → deletes, no violation.
you> !rogue                            → simulate a build that skips the consent step.
you> delete workspace/reports/q3.pdf   → unconsented delete (inside scope, so AAP-2 stays clean) → AAP-1 violation.
you> /end                              → records already streamed per action; /end reads them back from the store and commits their traceHash on-chain.
challenge.py    the party challenges the consent promise (posts a challenge bond).
verifier_service.py (always on)    sees the event, re-hashes the trace vs chain, runs the predicate,
                                   rules VIOLATED, submits the verdict → bond slashed, challenger paid.
observe.py      the on-chain event log + the SQL store contents, side by side.
```

For AAP-2 instead: ask it to `read outside.txt` (a file outside `workspace/`), `/end`, then
`challenge.py scope`. Everything runs locally: anvil for the chain, a FastAPI+SQLite store, the
verifier as a local process. The mechanism — promise → bond → violation → challenge → verdict →
slash — is fully exercised, and every step is visible.

## Map of the code
`packages/commons` — the spine: `ActionRecord`, canonical hashing (`ids.py`), the `Predicate` protocol
+ registry, the predicates. Imported by both the SDK and the verifier. ·
`packages/sdk` — `Accountability` + `guard` (`__init__.py`), the `EscrowClient` (`chain.py`). ·
`packages/verifier` — the binding checks + predicate run. ·
`packages/store` — FastAPI + SQLite. ·
`contracts/Escrow.sol` — the escrow (+ Foundry tests). ·
`agent/demo_agent.py` — the filesystem agent: tools + the consent integration (`act`, standing grants). ·
`scripts/` — `start_demo`, `challenge`, `verifier_service`, `observe`, `catalog`, `check_promises`.
