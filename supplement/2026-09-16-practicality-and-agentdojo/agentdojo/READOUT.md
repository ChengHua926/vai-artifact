# AgentDojo audit readout

2026-09-16. All frozen labels, traces, promises and evaluation outputs remain unchanged. This is a retrospective explanation of the reported alarms, not a new benchmark result.

## What the numbers establish

| Successful native attacker objectives | GLM | Qwen |
|---|---:|---:|
| Total | 266 | 161 |
| Detected grader-linked destination effects, frozen credit | 125 | 70 |
| Other successful objectives with an alarm | 35 | 30 |
| Successful objectives without an alarm | 106 | 61 |

All 195 eligible destination effects were detected. That is 125/266 (47.0%) and 70/161 (43.5%) of all successful objectives. It is not 100% coverage of AgentDojo harms.

The other 65 alarmed successful attacks contain 36 reported Fred-invitation alarms, four phishing-email recipient alarms, 19 alarms only on user-requested operations/destinations, and six alarms on other unsupported actions or recipients. Fred invitations contribute to the attack. For phishing emails, the destination check does not check the harmful message content; legitimate email to the same participant would also trigger it. An alarm elsewhere in a run must not be credited as detecting the attack.

The existing strict result should remain the primary result. If the figure shows whether any promise triggered, distinguish the additional 35/30 with hatching or a separately labelled segment. Do not call these all matched detections, and do not call the entire remainder no-fire or subjective judgment. Any expanded action-alignment result should be identified as a new secondary analysis, with the property mismatch explained.

See [all 65 cases and interpretation](other_objective_interpretation.md).

## What the alarms on runs without an attack establish

There were 97 benign runs per model, with 11 GLM and 12 Qwen alarmed runs. Benign means no injected attack, not a comprehensive policy-compliance label.

| Assessment of all flagged destinations in an alarmed benign run | GLM | Qwen |
|---|---:|---:|
| Authorized destinations rejected by the initial allowlists | 11 | 9 |
| At least one wrong destination argument | 0 | 2 |
| Ambiguous company website path | 0 | 1 |

The first row contains real destination-level false alarms. The issue is the fixed initial allowlist, not address syntax. The user can authorize a new landlord, document recipient or invited Slack user that is absent from the initial list. A destination-bound authorization or confirmation record could address this; that remedy has not been implemented or measured in this audit. Accepting arbitrary addresses from tool text would not be a valid remedy.

These are mostly clearer implementation/scope limitations than ClawsBench's two policy-interpretation disagreements. They are explainable, but the explanation does not erase the false alarms.

See [all 23 benign alarm runs](benign-report.md).

## Why attack-failed is not a no-violation label

Among resisted attacks, 84 GLM and 77 Qwen runs had alarms. Every one was reviewed. Of these, 39 have a reported action at the attacker's target. Replaying the pinned native graders reproduced all 39 false attack verdicts. In 31, money, private content, a secret or attachments still went to the attacker's target, or a scheduled payment was redirected. The full attack objective failed for another condition, such as exact payload formatting, the required subject, or deleting the sent email. Six had an off-list operation without the specified sensitive payload or transferred value; two were legitimate requested emails.

The other 122 resisted-attack alarms mainly concerned authorized destinations, with additional unsupported actions and partial attacks. They cannot all be called either false positives or detections of the specified attacker objective. The complete case ledgers preserve those distinctions.

See [39 target-matching cases](resisted-matching-conclusions.md) and [the other 122 cases](resisted-other-report.md).

## Reporting recommendation

AgentDojo is reportable with an explicit measure: detection of grader-linked destination effects, plus a separate alarm analysis. Tau and ClawsBench have committee policy-compliance labels; AgentDojo has specific attacker-objective labels. Those reference labels answer different questions. A shared figure must say so rather than imply a common comprehensive no-violation reference.

Confidence is high in the audited counts and concrete trace facts. Whether to broaden detection credit beyond the frozen destination-effect measure is a reporting choice, not a newly verified property of the predicate.
