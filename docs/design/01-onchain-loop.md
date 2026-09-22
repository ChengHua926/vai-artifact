# On-chain Loop — Decisions & Status

> **Historical decision log.** The current end-to-end flow is the interactive demo — see
> `../HOW_IT_WORKS.md` and `../RUNBOOK.md`. Since this was written, the single-process
> `scripts/demo_chain.py` was folded into the multi-process flow (`start_demo` + `verifier_service`),
> and the file-based `LocalStore` was removed in favor of one store client (`HttpStore` → the
> FastAPI/SQLite service). The decisions below still hold.
>
> Status: **working locally on anvil** (the full slash loop). Base Sepolia deploy deferred.
> Companion to `00-architecture.md`. Records the decisions made wiring the on-chain half,
> several of them from the Ilya/Karpathy review agents.

> Historical note (2026-09-02): decision log from the first on-chain loop. The API names below were updated to the current contract (two-phase commitment, per-promise reserve, availability paths); for the mechanism itself read `contracts/src/Escrow.sol` and `scripts/promise.py`.

## What runs

`scripts/demo_chain.py` against a local `anvil`:
provider `registerPromise`×3 (born funded) → SDK `openSession` → `session.guard()` produces a `delete_file`-without-
consent trace → `commitTrace` (traceHash on-chain) → challenger `challenge` → verifier
adjudicates → **escrow drains the promise's reserve by the payout and pays the challenger
payout + bond refund.** Verified: the promise's `reserve` drops by exactly `payout`; `Verdict.paidToChallenger == payout + challengeBond`.

## Key decisions

1. **Binding fix (soundness, the critical one).** `Escrow.challenge` now requires
   `promises[promiseId].provider == sessions[sessionId].provider`, re-asserted in `submitVerdict`.
   *Why:* without it, a party to victim V's session could register their **own** high-payout
   promise and drain V's bond on a genuine trace — the verifier and hash checks all pass. The
   mechanism must prove "*this provider committed to this predicate on this session*," not merely
   "this trace violates this predicate." Proven by `test_cross_provider_challenge_reverts`.

2. **Three binding checks in the verifier (I1 made operational).** Before judging, the verifier
   recomputes and compares to the **on-chain** values (never the store payload's self-reported
   fields): `trace_hash(records) == session.traceHash`, `params_hash(params) == promise.paramsHash`,
   `predicate_hash_for(id) == promise.predicateHash`. This is what makes a *trusted* store sound —
   it can hold data it cannot forge. Implemented as explicit `raise` (not `assert`) so `python -O`
   cannot strip the guarantee. (`packages/verifier/aa_verifier/__init__.py`.)

3. **Verifier imports `aa_commons` directly (I3).** It runs the *same* predicate the SDK does —
   `registry.get(id).evaluate(...)` — so there is exactly one implementation, no drift. (This is
   why `core` is separate from `sdk`: the verifier needs the predicate without the provider-side code.)

4. **Params live in the store, keyed by promiseId.** Only `paramsHash` is on-chain. So the SDK
   ships `{promiseId, predicate_id, params}` to the store on `registerPromise`; the verifier fetches
   by promiseId and hash-checks. (`Accountability.register_promise` → `store.put_promise`.)

5. **Session id = a bytes32-shaped hex string** (`"0x"+32 bytes`). Usable directly on-chain *and*
   reversible to the store key (`Web3.to_hex(event.sessionId)`), so the verifier can fetch the trace.

6. **Local chain = anvil's unlocked default accounts; no manual signing.** Calls are plain
   `.transact({"from": sender})`. Manual key signing / nonce management is the deferred Base Sepolia
   work (the funded toy wallets in `.env`/`secrets/` are for that). IDs (`promiseId`/`challengeId`)
   are read from **event receipts** (`process_receipt`), not Solidity return values.
   `bytes32` args are `Web3.to_bytes(hexstr=…)` on already-keccak'd hex — never re-hashed.

7. **Store: file `LocalStore` for the local anvil loop; the FastAPI **SQL** store (`packages/store`,
   SQLite) over `HttpStore` for the live path.** The store exposes `/sessions` *and* `/promises`
   (the latter so the verifier can fetch + hash-check params over the network). One store, keyed by
   `STORE_DB`; swap to Postgres/Neon by changing that. Railway-ready (`Procfile`, binds `$PORT`).

8. **Gas-robust assertions.** Assert on the `bonds(provider)` mapping delta and the `Verdict` event,
   not raw challenger ETH balance (which is off by the gas the challenger paid to file).

9. **Final adjudications close the `(sessionId, promiseId)` pair.** A payout marks the pair `resolved`
   before transfer (CEI); a satisfied verdict also marks it resolved when the session has a final
   trace commitment. `challenge` rejects either resolved outcome. This prevents duplicate payouts
   and reopening a cleared trace to obtain a default payout during a later outage. The guard is per
   pair, so other promises and sessions remain challengeable. Withdrawal without a verdict, or a
   satisfied verdict before a final commitment, leaves the pair retryable. Covered by
   `test_replay_same_pair_reverts`, `test_satisfied_final_trace_cannot_be_rechallenged_for_default_payout`,
   and the withdrawal and nonfinal-satisfaction regression tests.

## Stage-1 trust shortcuts (unchanged, documented)

- **Concurrent duplicate challenges** on the same pair beyond the first to settle are rejected at
  verdict time, but those challengers' bonds aren't auto-refunded (left in their Open challenge).
  A real system would refund or block at open with liveness; out of scope for the local proof.

- Verifier is a **trusted role** (centralized) — removed later via optimistic dispute / TEE.
- `party` is recorded **unilaterally by the provider** at `openSession` (no party-consent proof; the open receipt lets the party check it at session start);
  the signed-receipt upgrade is deferred and **load-bearing**, not novel.
- No correlated-loss bond sizing, no optimistic on-chain re-check — both deferred.

## Components (local)

`contracts/` Escrow.sol (8 Foundry tests) · `packages/commons` (predicates, hashing) · `packages/sdk`
(guard chokepoint + `chain.EscrowClient`) · `packages/verifier` (binding checks + predicate) ·
`scripts/demo_chain.py` (the loop). The off-chain agent demo is `agent/demo_agent.py`.

## Live deployment (Base Sepolia) — additional decisions

10. **Bond-lock (soundness, the second critical fix).** An open challenge reserves its payout
    (`locked[provider] += payout`), released at verdict; `withdrawBond` requires `bonds - locked >=
    amount`. *Why:* once the loop is live, a provider could watch the public `Challenged` event and
    race a `withdrawBond` to zero before the verdict → no slash. Proven by
    `test_open_challenge_locks_bond_against_withdrawal`. (Found by the live-deploy Ilya review.)

11. **Keyed signing for the real network.** `EscrowClient` supports both anvil unlocked accounts
    (`.transact`) and Base Sepolia keyed accounts (`build_transaction` → `sign_transaction` →
    `send_raw_transaction`; `deploy()` too). The node auto-fills EIP-1559 fees; nonces use
    `'pending'` and each call waits for its receipt (sequential), so no nonce races.

12. **Deploy as owner == verifier; provider/challenger/verifier are distinct wallets.** Avoids the
    "one EOA in multiple roles" tell — the provider can't `setVerifier`, and a slash visibly moves
    ETH between independent parties.

13. **Always-on verifier listener** (`scripts/verifier_service.py`): poll `Challenged` every ~6s;
    checkpoint `last_processed_block`; clamp `to_block = min(head - 2, from + 1999)` (the public
    RPC's 2000-block `get_logs` cap + a small reorg lag) and guard the inverted-range idle case
    (it errors, not `[]`). **Process, then advance** — idempotency is the chain's job (the
    status/`resolved` guards reject re-slash), so a crash mid-window just re-skips on retry. Skip
    advance-safe reverts (`not open`, `pair already resolved`, legacy `pair already slashed`, and `reserve insufficient`);
    a binding-check failure logs an **ALERT** (tamper signal). Cut: a low-gas warning (verifier ETH
    funds hundreds of verdicts at ~0.011 gwei).

14. **RPC trust (stage-1 shortcut).** The contract trusts the verdict boolean; the binding checks
    are only as honest as the RPC feeding `get_session`/`get_promise`. Mitigation later: a
    trusted/own RPC or cross-checking two. Documented, not yet enforced.

**Verified live:** Escrow `0x82d53b92…DD20` (block 42896462). provider bonded 0.0003 ETH → agent
deleted `workspace/stale.log` without approval → challenger challenged the consent promise → the
listener ruled VIOLATED and submitted the verdict → bond slashed to 0.0002 ETH, challenger paid
0.00015 ETH. Confirmed independently via `cast`. See `docs/RUNBOOK.md` for the walkthrough.
