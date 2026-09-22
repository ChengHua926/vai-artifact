# Evaluation audit and practicality experiments — 2026-09-16

- [AgentDojo findings and reporting boundaries](agentdojo/READOUT.md)
- [Practicality results and Section 5.2 plan](practicality/analysis/READOUT.md)
- [AgentDojo figure preview](agentdojo/agentdojo-alarm-distinction.png)
- [Runtime overhead and verifier scaling](practicality/analysis/local-practicality.png)
- [Base checkpoint tradeoff](practicality/analysis/public-checkpoints.png)
- [Canonical receipts, fees and balance reconciliation](practicality/analysis/canonical-receipts.json)

This archive contains raw evidence, per-case analyses, scripts, source snapshots, machine metadata and plots. Frozen benchmark labels/results and the paper were not changed. The measurement code is committed locally through08d9695 on eval-practicality-mainnet; nothing from this turn was pushed or merged. Private wallet keys and passwords are excluded. The funded wallet and encrypted role keys remain in the protected .local/base-mainnet-eval-20260916 directory.

All14 public challenge decisions and9 cadence runs passed. All156 experiment transaction receipts were checked against canonical L2 blocks. Their fees sum to0.000133372217667810 ETH. Two unrelated incoming transfers are recorded separately; the full controlled balance conservation equation reconciles exactly. All local experiment services were stopped after completion.
