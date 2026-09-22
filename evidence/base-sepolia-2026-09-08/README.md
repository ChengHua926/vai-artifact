# Base Sepolia revision run, 2026-09-08

**The complete run is [run-4.json](run-4.json): eight scenarios passed, 39 transactions,
and no unresolved transaction attempt.** Six challenges settled: five violation payouts and
one satisfied verdict. Source revision: `94a1fc990b29edecbbbc2d3203eaeb7586efc22e`.

Contract: [`0xFDd1aD23A31454355709C60Fd5edd44AED829Ffc`](https://sepolia.basescan.org/address/0xFDd1aD23A31454355709C60Fd5edd44AED829Ffc).
The deployer/verifier/admin, provider, and challenger used distinct keys; one experiment operator
controlled those roles. This checks key and authority separation, not organizational independence.

The [summary](summary.json) derives costs from [archived receipts](receipts.json). The run used
6,245,260 gas. Execution plus archived L1 fees were 38022817448622 wei
(0.00003802281745 ETH). Initial L1 fee fields differed from later reads for
24 receipts; both observations are preserved. Client transaction-call duration had median
0.681s and maximum 2.532s. Timing starts before the attempt journal write and ends after
the confirmed-attempt journal update. It includes local journaling, sender-lock waits,
transaction preparation, signing, RPC, and receipt polling; it is neither broadcast-to-receipt
latency nor L1 finality. After appending one record, the client observed the timer
checkpoint 33.15s later. After the tenth append,
it observed the count checkpoint 2.88s later.

The six settlement cases cover valid consent, reused once permission, execution after denial,
scope escape, unavailable evidence after a provider response, and a final trace contradicting
an anchored prefix. The other two cases exercise count and timer checkpoints. The runner uses
synthetic authorization decisions and controlled store faults. Native framework approvals are
tested separately in the pinned integrations. Deadline and reserve edge cases remain local
Foundry evidence; this run never advances chain time.

[Escrow.json](Escrow.json) is the exact compiled artifact; its SHA256 is
`bd156e5ce4dfe0ec38d7824fe071c4e9749bd6664a448b5ce7142dce466e402c`. Its runtime matches the deployed bytecode.
[implementation-counts.json](implementation-counts.json) separates adapter source, native source
patches, and tests. [repository-interest.json](repository-interest.json) preserves dated GitHub
counts without treating them as active-user counts.

The prototype stores these controlled traces privately on loopback during execution. This
archive publishes only the synthetic protocol evidence. It is not a claim that arbitrary user
traces should be public.

## Earlier attempts and recovery

The first attempt is preserved in `run.json`. Its deployment transaction succeeded, but the
immediate bytecode assertion failed before any promise registration or session execution.
The read-only follow-up in `attempt-1-followup.json` found the exact expected bytecode at both
the deployment block and the later head. The original mismatching response was not captured.

The subsequent revision pins deployment checks to the receipt block and bounds retries while
that block is unavailable. State reads also use a block no earlier than a receipt the client
has already observed. This handles RPC read lag; it does not claim blockchain finality.

The second attempt (`run-2.json`) also stopped after deployment and before any promise
registration. Base returned the unavailable receipt block as JSON-RPC error -32001 with
`block not found: <hex block>`, which web3.py exposed as a generic RPC exception. The next
revision recognizes that exact response only when its block matches the requested snapshot.
Neither failed attempt exercised a session or settlement.

The third attempt (`run-3.json`) completed six scenarios and 31 transactions. It stopped when
web3.py estimated the provider response against implicit latest state and reported `not open`
for a challenge whose receipt had just arrived. This was a pre-broadcast estimation failure;
challenge 5 remained open. The next revision estimates keyed transactions at the same
receipt-bounded snapshot used for state reads. It preserves genuine contract rejections and
does not automatically resend transactions.

[run-3-cleanup.json](run-3-cleanup.json) records the final settlement of the interrupted run's
open challenge. Its real trace was available and satisfied the scope promise; the controlled
withholding step had not happened. The archive includes the synthetic record, promise parameters,
and chain commitments read at cleanup block 46567122. Replaying those inputs verifies the
committed trace and parameter hashes and reproduces the satisfied verdict. Cleanup is separate
from the successful eight-scenario run.

## Reproduction

Check out the source revision above, install the packages as in `docs/RUNBOOK.md`, and build
with `forge build --offline --root contracts` (Solidity 0.8.24, optimizer enabled, 200 runs).
Start the store with a new dedicated SQLite database. Set `AA_LOCAL=0`, `AA_RPC_URL` to a
Base Sepolia RPC, and `STORE_URL` to that store. Run `scripts/run_evidence.py --output <new.json>`
from a clean checkout with the three funded test signers. The script checks chain 84532 and
refuses an occupied store or an existing output file. Deployed addresses and transaction hashes
will differ. Native tests and required patches are described in each integration's `upstream.json`.
