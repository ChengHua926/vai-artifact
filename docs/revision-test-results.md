# Revision validation, 2026-09-08

This log records validation before and after the source commits and public-network run.
All commands used the revision checkout. No benchmark inputs or cohort memberships changed.

| Layer | Command / evidence | Result |
|---|---|---|
| Python protocol and scripts | `PYTHONPATH=packages/commons:packages/sdk:packages/store:packages/verifier .venv/bin/python -m pytest packages/ scripts/tests/ -q` | Final aggregate: 151 passed (119 package tests and 32 script tests). Earlier scoped runs are recorded below. |
| Solidity | `forge test --offline --root contracts` | 34 passed, including two checkpoint tests |
| Hermes | `integrations/hermes/upstream.json` and `TEST_RESULTS.md` | 367 passed before the final adapter-only lifecycle fix; the final focused run passed 20 integration cases. The 349 upstream regressions were unchanged. |
| OpenClaw | `integrations/openclaw/upstream.json` | 81 native, six transport, and 11 Python tests passed; TypeScript check and plugin build passed |
| Cross-language nonexecution | `integrations/openclaw/tests/check_native_refusals.py` over five actual native observations | Five refusals recorded as nonexecution; no consent violation |
| Local protocol | `scripts/run_evidence.py --allow-dirty` against Anvil 31337 and an isolated store | Eight scenarios passed; 39 confirmed transactions; no unresolved attempt |

Native tests run real framework dispatch/approval paths with controlled user decisions and
transport. They include actual fixture file operations and an isolated OpenClaw gateway command.
They do not establish live-model, production-client, or every-entrypoint coverage. Each integration
records its tested and unsupported paths, upstream revision, and patch digest.

The local protocol run exercises settlement, count/timer checkpoints, withheld evidence, and a
final trace that contradicts an anchored prefix. Deadline branches use Foundry time travel and
are not public-network results. Independent reviewers checked the core, each adapter, and the
evidence scripts; all reported defects were repaired and scoped regressions passed.

Source measurements use `scripts/measure_implementation.py`: physical lines including comments
and blanks, excluding tests and dependencies. Native implementation and test patch changes are
reported separately. The successful public run identifies the committed source and artifact hashes in
`evidence/base-sepolia-2026-09-08/README.md`.

After the first public deployment, focused regressions cover receipt-bounded state reads, unavailable deployment code/roles, and authorization schema version 1. The contract and native patches did not change.

The second public deployment exposed Base's generic JSON-RPC encoding of an unavailable
receipt block. Four additional chain-read cases and two deployment-check cases cover that
response and ensure unrelated RPC errors still propagate. The focused run passed 36 tests.

The third run exposed implicit latest-state gas estimation after a challenge receipt. Three
regressions now check the receipt-bounded estimate, one broadcast after transient read lag,
and zero broadcasts after a real contract rejection. The complete package suite passed 119
tests after this repair. Script tests total 32; the contract and native patches remain unchanged.

The final aggregate command passed all 151 Python package and script tests after the successful
Base run. Its complete output is `evidence/base-sepolia-2026-09-08/python-tests.txt`.
