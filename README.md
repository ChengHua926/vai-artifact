# VAI: verifiable agent insurance

Implementation for the paper "Holding Agents Accountable with Verifiable Agent Insurance" (under review).

An agent provider registers promises, with a reserve, in an escrow contract. An SDK in the agent harness records each tool call and permission decision in the provider's store and commits hashes of the records on chain. A user who finds a violation files a claim. The provider sends the claimed session's records to an independent verifier, which checks them against the on-chain hashes, evaluates the promise, and submits a verdict that the contract settles.

## Layout

| Path | Contents |
| --- | --- |
| `contracts/` | `Escrow.sol` (promises and reserves, session commitments and checkpoints, claims, verdicts, payouts) and its tests |
| `packages/commons/` | Record format, hashing, and the five promise predicates (AAP-1 to AAP-5) |
| `packages/sdk/` | Recording in the agent harness, checkpoints, the final commitment, and evidence delivery for a claim |
| `packages/store/` | The provider's trace store (HTTP and SQLite, provider-only access) |
| `packages/verifier/` | The verifier's evidence inbox and claim adjudication |
| `integrations/` | Hermes and OpenClaw adapters and the patches they need |
| `scripts/` | Provider and verifier services, deployment, and the local demo |
| `agent/` | The file agent used by the local demo |

## Claim flow

1. The user files a claim on chain for a session and a promise, with a bond.
2. Once the session's final hash is on chain, the provider signs the session's records and the promise parameters and sends them to the verifier. They must arrive within three days of filing.
3. The verifier checks them against the final hash, every checkpoint, and the parameter hash, selects the predicate by its registered hash, and submits the verdict.
4. Missing, late, or mismatching evidence is a violation. On a violation the contract pays the user the payout plus the bond, provided the reserve covers the payout. On a satisfied verdict it credits the bond to the provider.
5. If no verdict arrives within ten days, or the reserve no longer covers the payout, the user can withdraw the bond.

## Running locally

Requirements: Python 3.11 or later, Foundry, and an Anthropic API key for the demo agent's chat turns.

```sh
python -m venv .venv
.venv/bin/pip install -e packages/commons -e packages/sdk -e packages/store -e packages/verifier anthropic "uvicorn[standard]"
cd contracts && forge install foundry-rs/forge-std --no-git && forge build && forge test && cd ..
.venv/bin/python -m pytest -q packages
```

The demo runs a local chain, the provider's store, the agent, the verifier, and the provider's evidence service in five terminals. Create the provider's store token once with `.venv/bin/python -c 'import secrets; print("export STORE_TOKEN=" + secrets.token_urlsafe(32))' > .env.provider`, and put `ANTHROPIC_API_KEY=...` in `.env`.

```sh
anvil                                                                        # 1: local chain
source .env.provider && .venv/bin/uvicorn app:app --app-dir packages/store --port 8000   # 2: provider store
source .env && source .env.provider && .venv/bin/python scripts/start_demo.py            # 3: deploys, registers promises, opens the agent
.venv/bin/python scripts/verifier_service.py                                 # 4: verifier (after 3 has deployed)
source .env.provider && EVIDENCE_URL=http://127.0.0.1:8001 .venv/bin/python scripts/provider_service.py  # 5: provider evidence service
```

In terminal 3, `!rogue` makes the agent skip the consent step, a request such as "delete workspace/reports/q3.pdf" then violates AAP-1, and `/end` commits the session. `.venv/bin/python scripts/challenge.py` files a claim; the provider service delivers the evidence and the verifier settles the claim. `.venv/bin/python scripts/observe.py` shows the contract events and the store.

## Base mainnet deployment

The experiments in the paper use contract [`0x9eD99dF9702f6fdb0E5a4acad084Adb8342b4c4e`](https://basescan.org/address/0x9eD99dF9702f6fdb0E5a4acad084Adb8342b4c4e) on Base mainnet, deployed on September 25, 2026.

Fourteen claims over eleven Hermes sessions all settled as expected:

| Case | Claims | Verdict |
| --- | ---: | --- |
| Approved write within scope, checked for scope | 3 | No violation |
| Same approved write, checked for authorization | 3 | No violation |
| Approved write outside the promised scope | 3 | Violation |
| Native write without the promise's required approval | 3 | Violation |
| Evidence altered after the final commitment | 1 | Violation |
| Final trace contradicts an earlier checkpoint | 1 | Violation |

Checkpoint policies, with three sessions of 20 writes per policy and harness. Fees are per session at 2,682.79 USD/ETH; the wait runs from a record's creation to the first receipt of a commitment that covers it.

| Checkpoint policy | Hermes fee (USD) | OpenClaw fee (USD) | Hermes wait (s) | OpenClaw wait (s) |
| --- | ---: | ---: | ---: | ---: |
| Every 10 records or 30 s | 0.00512 | 0.00378 | 12.46 | 12.27 |
| Every 30 records or 60 s | 0.00378 | 0.00244 | 11.25 | 10.91 |
| Final commitment only | 0.00086 | 0.00087 | 12.03 | 11.14 |

The experiments sent 180 transactions in total, for 139.4 µETH in fees including the deployment.

## Annotations

The policy-compliance labels for the τ³-bench and ClawsBench runs in the evaluation are released separately: https://anonymous.4open.science/r/vai-annotations-00EB

## License

MIT (see `LICENSE`). The integration patches follow the licenses of the projects they modify (see `NOTICE.md`).
