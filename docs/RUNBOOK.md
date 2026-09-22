# Runbook — the local interactive demo

Everything runs locally: a local chain (anvil), a SQL store, and the verifier as a local process.
You play the provider (you register promises and run the agent) and the challenger. Use a few
terminals so each component is visible. See `HOW_IT_WORKS.md` for *why* each piece does what it does.

## One-time setup
```
cd agent_accountability_prototype
uv venv && uv pip install -e packages/commons -e packages/sdk -e packages/verifier web3 anthropic fastapi "uvicorn[standard]"
forge build --offline --root contracts
# .env must contain ANTHROPIC_API_KEY=... (used only for the agent's chat turns)
```

## Bring it up (4 terminals)
```
# A — the chain
anvil

# B — the SQL store (the protocol's data layer)
.venv/bin/uvicorn app:app --app-dir packages/store --port 8000

# C — start the demo: deploys the escrow, posts a bond, registers the promises,
#     prints the component->promise graph, and opens the agent REPL
source .env
.venv/bin/python scripts/start_demo.py

# D — the always-on verifier (start it after C has deployed; it logs each adjudication)
.venv/bin/python scripts/verifier_service.py
```

## Drive it (in terminal C, the REPL)
```
you> what files are in the workspace?        # normal chat — a plain file agent
you> /grant workspace/tmp/                   # standing consent: auto-accept deletes under tmp/ (logs user_grant)
you> delete the scratch log in tmp           # under tmp/, covered by the grant -> deletes, no violation
you> !rogue                                  # simulate a build that SKIPS the consent step
you> delete workspace/reports/q3.pdf         # unconsented delete (inside scope -> AAP-2 clean) -> AAP-1 violation
you> /end                                    # records already streamed per action; /end commits the hash of the store's records on-chain
```
For an unconsented delete *not* in rogue mode the agent prompts you for a real y/n (logged as
`user_consent`). For AAP-2 (scope): ask it to `read outside.txt` (a file outside `workspace/`).
Commands: `/grant <prefix>` · `/revoke <prefix>` · `!rogue` (toggle) · `/end` · `/help` · `/quit`.

## Challenge + observe
```
# the challenger challenges a promise on the committed session (default: consent; or `scope`)
.venv/bin/python scripts/challenge.py            # add `scope` to challenge AAP-2 instead
#   -> watch terminal D: the verifier sees the event, re-hashes the trace vs chain,
#      runs the predicate, rules VIOLATED, slashes the bond, pays the challenger.

# the full picture: on-chain event log + the SQL store contents
.venv/bin/python scripts/observe.py
```

## Observability — where to look
- **On-chain (`observe.py`, section 1):** the local "explorer" — bond + reserved (`committed`), and the
  event log: `PromiseRegistered` (with the predicate/params hashes), `SessionOpened`, `TraceCheckpointed`, `TraceCommitted`,
  `Challenged`, `Verdict`, plus each challenge's status.
- **The SQL store (`observe.py`, section 2):** the promises (predicate + params) and every session's
  streamed records — the trace — read straight from SQLite. (Point `STORE_DB` elsewhere if you moved it.)
- **The verifier (terminal D):** structured logs per challenge — event → binding checks → verdict →
  payout. A store/chain mismatch logs as `committed data not produced`: no verdict while the provider
  is silent (`claimDefault` is the remedy), a violation once it has responded on chain.

## Sanity check (no chain, no LLM)
```
.venv/bin/python scripts/catalog.py          # the promise catalog (AAP-N): numbers, versions, predicate hashes
.venv/bin/python scripts/check_promises.py   # asserts each predicate faults exactly when it should
.venv/bin/python agent/demo_agent.py         # scripted off-chain run: trace + the verdicts the verifier would compute
forge test --offline --root contracts        # binding, replay, reserves, deadlines, checkpoints
.venv/bin/python -m pytest packages/ scripts/tests/ -q
```

## Deterministic protocol evidence

The evidence runner executes SDK consent and settlement scenarios without a model. Native
Hermes/OpenClaw hook tests remain separate and are documented under each integration.
With Anvil and a fresh, dedicated store running:

```sh
.venv/bin/python scripts/run_evidence.py --output _runtime/local-evidence.json
```

For local development only, `--allow-dirty` permits uncommitted source. Each run needs a new
output filename; the runner preserves partial evidence if a transaction or assertion fails.
It checks the store's inventory before sending transactions and refuses an occupied store.
Keep the store exclusive to that deployment: promise IDs are local to each contract.
It exercises the default 10-record and 30-second checkpoint triggers, so allow at least 30 seconds.

For Base Sepolia, build and commit the source first, fund the three existing role wallets,
and use a fresh store database. The provider and challenger must differ from the verifier;
the verifier wallet also owns the contract.

```sh
AA_LOCAL=0 .venv/bin/python scripts/run_evidence.py --output evidence/base-sepolia/run.json
```

The runner checks chain ID 84532, refuses uncommitted source, verifies the compiled source hash
and deployed bytecode, and journals receipts after each transaction. It records L1 fees when
the RPC provides them; a missing fee is marked unavailable. It never shortens contract deadlines.
Foundry tests cover the three-, ten-, and thirty-day boundary cases through local time travel.
An unresolved transaction attempt marks the evidence incomplete, even if the scenarios recover.

Deployment metadata lives under `_runtime/<chain-id>/deployment.json`; `AA_DEPLOYMENT` overrides
the path. Verifier scan cursors are separate for every deployment. These cursors track event
polling; they are unrelated to the trace-prefix commitments on chain.
The verifier service loads only its own signing key. It reads other role addresses from the
deployment metadata; provider and challenger private keys are not needed by that process.

Records remain in the provider's store. The verifier needs access to records for adjudication.
The local server binds to loopback by default; authentication and encryption for a remotely
exposed store require deployment configuration.
