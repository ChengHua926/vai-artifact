# Tau observation and native-policy corrections for v2

The v2 reducer in `eval/reference_v2/tau/` evaluates each call against its pre-call observations, then applies the successful result. The original corpus, generated results, `eval/tau` implementation, annotated v1 policy documents, and core AAP predicates are unchanged.

## Changed behavior

- Successful native entity reads and writes replace the observed order, user, or reservation snapshot. Booking results establish the new reservation. Proposed arguments alone never establish post-state. An opaque successful entity result invalidates its previous snapshot. Rejected results do not update state.
- A boolean `captured_error` is authoritative. Only exports without that boolean use the historical `Error` prefix fallback.
- Airline `read_before_write` is removed. Native modification and cancellation policy requires obtaining the identifiers; it does not universally require `get_reservation_details` when the identifiers are already available.
- `user_id_from_user` applies to booking, modification, and cancellation, the sections containing that requirement. Compensation has no corresponding user-ID repetition requirement.
- `build_inputs` retains `scope_checks`, `mandate_checks`, `count_caps`, and `rejected`, and adds `unsupported_checks`: a mapping from call sequence to records with `arm`, `reason`, and an optional target. These records are not predicate fires.
- Unknown profile payment methods, flight statuses, reservation fields, ownership, or user-turn evidence produce explicit unsupported records. A known payment-method collection, including an observed empty collection, still supports exclusion. Certificate creation invalidates a previously observed payment-method collection because the native tool adds a payment method but returns prose rather than a replacement profile.
- Cancellation eligibility fires only after all permitted alternatives have been ruled out using observations. An observed qualifying alternative passes that arm. Otherwise each unavailable condition produces its own reason. The flown arm reports same-day, absent, and malformed date evidence as unsupported.
- Certificate checks retain the existing relaxation across the user's observed reservations. A positive eligibility condition or matching observed amount can pass. Proving ineligibility or excluding an amount requires the profile's complete reservation list and the relevant observations for every listed reservation. Missing context is not an inferred zero-dollar mandate.

## Preserved rules and limitations

Retail authentication, single-user ownership, explicit status-check requirements, item modification lockout, identical-item checks, cancellation reasons, and per-order count limits remain. Retail's native policy explicitly says to check order status before acting, so never establishing a status remains a process violation. A known snapshot whose status field is unavailable is unsupported, not a guessed status violation.

No new consent parser, route/trip check, baggage-price formula, payment-composition check, or other policy capability was added. The existing date-based flown rule remains a date approximation: past dates trigger it; same-day dates cannot establish it. Insurance alone now leaves cancellation eligibility unsupported because the covered reason is not structurally bound; an independently established business, recent-booking, or airline-cancellation alternative still passes. Certificate amounts still accept either $50 or $100 times passenger count and do not parse the complaint or select a specific reservation. These relaxations can miss violations; they are not evidence that the full native policy was satisfied.

The historical v1 policy annotations describing absent observations as out-of-scope are not the v2 semantics. The separate v2 registered verifier must execute and hash this reducer and `promises.py`, not merely hash a membership predicate over previously computed inputs.

## Verification

`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider eval/reference_v2/tests/test_tau_evidence.py`

39 v2 reducer tests passed. New regressions were first observed failing: 27 initial failures, then additional missing-evidence/process cases. The cabin-upgrade regression also observes a noncancelled flight status so it cannot pass solely because cancellation evidence became unknown. No original artifact was regenerated.

## Rule-first completion, 2026-09-11

The later approved correction keeps the original generated `results/` immutable and extends the same reducer. New output must use a separate destination and a new source commitment.

- Retail product information now shares the authentication gate: `list_all_product_types`, `get_product_details`, and `get_item_details`. Product records are not assigned fictitious user ownership. Confirmed previously missed runs include GLM ext-016/ext-103/ext-287 and Qwen ext-282. The predicate applies to the whole corpus, not a task allowlist.
- Native assistant message boundaries activate `one_tool_at_a_time` and `tool_with_response` for both domains. Only public `content` is considered, never provider reasoning fields. Consecutive single-call assistant messages are allowed. The native executor runs tools sequentially even within one assistant message. Each action therefore sees the successful result of preceding actions; the multi-call message is checked separately as a protocol breach.
- Airline booking checks now include the five-passenger cap and payment composition: at most one distinct observed certificate, one distinct credit card, and three distinct gift cards; split amount rows for the same method do not create additional methods. A known count exceeding its cap proves a breach even if other types are unknown; incomplete type observations cannot prove the complete composition passes. Flight modification payment types must be an observed gift card or credit card.
- `route_preserved` compares replacement segment endpoints observed in prior searches or reservation records, in captured itinerary order. It does not trust the reservation header alone: the native tool can preserve the header while changing the actual segment route. Same flight identities pass; unknown endpoints are explicit unsupported checks. This catches GLM ext-049 and Qwen ext-061/ext-084/ext-160.
- `baggage_allowance` computes nonfree bags from the observed membership, cabin, passenger count, and explicit proposed baggage counts. The policy table is regular basic/economy/business = 0/1/2 free bags per passenger, silver adds one, gold adds two. It catches Qwen ext-123 (Gold/economy, one passenger, three bags incorrectly marked one nonfree), among others.
- `baggage_allowance_after_cabin_change` applies that same current-cabin allowance to the final observed reservation at an explicitly completed episode. It allows intervening bag adjustments, ignores canceled reservations, and does not fire on an unfinished prefix. GLM ext-272 and Qwen ext-162 leave three bags with zero nonfree after changing two regular passengers from business to economy, whose allowance is two bags. This is an explicit interpretation that the baggage table remains applicable after a cabin change; it is not a claim that the policy spells out a separate post-change reconciliation procedure.
- Literal handoff checks only implement the specified tool/notice sequence. The exact prescribed notice must have a preceding successful transfer. A successful transfer must eventually be followed by that notice before the captured episode completes. Whitespace is normalized, surrounding prose is allowed, and intervening messages do not themselves violate an invented immediacy requirement. Repeating the notice does not require another transfer. Arbitrary handoff applicability remains semantic.

Assistant and completed-episode checks use supplemental `message_events` with negative benchmark sequence numbers: `-(native_turn_index + 1)`, and `-(len(turns) + 1)` for the explicit end. They retain public content, native turn index, and linked tool-action sequence(s), and are evaluated by the same scope engine. Missing terminal evidence is never inferred from a prefix. Ordinary tool preconditions still use only prior observations. Supported positive and negative checks are retained so reporting can distinguish a passed condition, absent support, and missing implementation.

### Reference limitations discovered by the correction

The frozen Tau labeling builder (`eval/labeling/build_tau_bundle.py`, native-message loop) splits each assistant message into a text step followed by one step per tool call, without preserving a parent-message or batch identifier. A labeler cannot reliably distinguish one simultaneous native message from several sequential messages. For example:

- Clean GLM ext-002, retail task 51: native turn 11 contains public text and calls 4/5/6; frozen labeling steps 12/13/14/15 display these as separate rows.
- Clean GLM ext-017, airline task 26: native turn 5 contains text and calls 2/3; frozen steps 6/7/8 are separate rows.

The raw provider payload retains these public `content` fields separately from `reasoning_content`. Among calls with at least one successful result, native boundaries establish tool-plus-text in 148 GLM tasks (103 positive, 45 clean reference) and 8 Qwen tasks (all positive); multi-call messages occur in 130 GLM tasks (92 positive, 38 clean) and 20 Qwen tasks (all positive). These overlap. They are native protocol findings with a label-input representation mismatch; preserve them separately from substantive action/state findings and do not relabel or suppress them to preserve small clean-fire counts. Those counts describe the earlier successful-call subset. Final protocol checks attach to the completed native assistant-message emission, independently of tool outcomes: Qwen therefore has 21 multi-call tasks and 9 tool-plus-text tasks, all positive references. The added cases are ext-266 (two rejected calls in one message) and ext-164 (public text with a rejected update); GLM task counts are unchanged. Ordinary authentication and mutation checks retain landed-action semantics. The frozen labeling prompt excluded rejected calls, so this additional protocol comparison also exposes a reference-definition mismatch.

Another concrete reference error: Qwen ext-051 has no native tool calls. Its apparent `<transfer_to_human_agents>...</transfer_to_human_agents>` invocation is literal assistant text. Citations describing a successful transfer are unsupported. Similarly, consecutive one-call messages (such as the ext-284 allegation) do not establish concurrency.

### Scope and residual boundaries

The new checks close the confirmed missing implementations above. Semantic refusal of a permissible operation, free-text payment selection, complete item-list confirmation, complaint applicability, and unsupported factual assertions do not become structural merely because they cite a deterministic-looking policy sentence. Full same-product/variant availability can be checked when relevant product observations exist, but the reviewed residual citations either already have an identical-item witness or allege a semantic refusal; no additional landed variant mismatch was established. Prescribed cabin uniformity is already enforced by a single shared `cabin` argument; no per-segment cabin change is expressible. A statement claiming otherwise is a factual/semantic allegation, not an unimplemented segment-cabin detector.

The original pre-call evidence limitations remain: missing status/profile/endpoint observations are unknown; consent, complaint, and requested-action bindings are not synthesized from prose. Newly observed body state may establish subsequent checks but never its own earlier precondition.

### Completion regressions

`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider eval/reference_v2/tests/test_tau_policy_completion.py eval/reference_v2/tests/test_tau_evidence.py`

73 tests passed at the completion checkpoint. New behavioral tests were observed failing before implementation, including product authentication, native concurrency/content, native sequential tool execution, payment composition, baggage allowances, route ordering, literal transfer prerequisites, episode closure, repeated notices, and intervening text before a valid notice. Full registered-runtime parity and output reproducibility are verified by the parent replay, separately from these reducer tests.

Directly observed flight states now override the past-date heuristic: a known canceled, available, delayed, or not-yet-departed flight is not treated as flown solely because its date is in the past. Same-day or malformed dates without a decisive status remain unknown. Insurance-only cancellation eligibility is unsupported without a bound covered reason; it is never emitted as a fully supported pass. Both corrections have regression coverage.

### Pinned native executor ordering

The captured Tau commit is `7ac89f5128bc9ff86e37cf49a54687b35083e598` (`eval/paper_main_v1/cohort.lock.json`). At that exact revision, `src/tau2/orchestrator/orchestrator.py:313-329` executes `for tool_call in tool_calls`, calls synchronous `environment.get_response`, and appends its result before continuing. `src/tau2/environment/environment.py:446-472` runs the actual tool and synchronizes tools before returning. The orchestrator returns the collected results to the model only after the loop (`orchestrator.py:885-891`). Thus the LLM chose the batch without seeing sibling results, but the mediator/environment has each successful result before executing the next action. This evaluation follows the agreed action/effect-time semantics, not a stronger model-planning-knowledge restriction.

Consequently, a successful authentication/status read in a batch supports subsequent actions, and a successful item modification in that batch activates status/lockout checks before subsequent address mutations. The native batch remains a separate protocol violation. Replaying against one frozen pre-batch state would miss GLM ext-003 seq14 and ext-023 seq7, both address changes after a successful item modification.

Pinned source SHA-256 values (current local files match the pinned Git revision):
- `src/tau2/orchestrator/orchestrator.py`: `6c689e5d1037dd08aef8834b46f58efa14359824cbebf7084de0172e5f580cba`
- `src/tau2/environment/environment.py`: `ba862a9e5b8aa9e0360cf313b5d0f26bb8f7eea53a93bdfe75d22d6627244f8a`

### Exact extra-bag fee and native message emission

The separate `baggage_fee_after_cabin_change` arm checks an actual unpaid fee. A wrong bag count alone is not a substitute for this arithmetic. For an explicitly closed episode, the arm requires a complete current reservation fare, passenger count, insurance value, and payment history; the first pre-cabin-change ledger must equal its complete fare + $30 per insured passenger + $50 per recorded nonfree bag. The final history must preserve that baseline as a prefix, with unchanged passenger count and insurance. These guards reject incomplete ledgers and preexisting deficits. The final check requires net payments minus current fare and insurance to cover $50 times the required current nonfree bags. Surplus payment does not create a refund obligation.

GLM ext-272 and Qwen ext-162 provide the exact evidence: original fare `2*(1859+1679) = $7076`, insurance `$60`, initial payment `$7136`; new fare `2*(140+101) = $482`; a `$6594` fare refund leaves `$542 = $482+$60`, with no bag payment. Two regular economy passengers with three bags need one nonfree bag, leaving a `$50` shortfall. The native tools at the pinned Tau commit separately price baggage (`tools.py:548-587`) and flight changes (`tools.py:592-691`); the captured complete ledger makes the missing charge computable without inventing a named billing component. Both tasks fire the distinct fee arm with structured `fee_witness` values and a linked cabin-change action. No clean-reference task gains a fee fire.

Protocol obligations now use native assistant-message events even when every requested tool fails: emitting a multi-call/public-text message is already a completed action. Checks use the original message boundary and linked tool sequences, with one event per message, not one duplicate fire per landed call. Rejection still suppresses ordinary tool-effect checks and state updates. The additional rejected-call evidence does not change or repair the frozen committee labels.

### Current once-per-order evaluation criterion

The approved primary comparison counts successful operations only, matching `eval/labeling/prompts/tau_v1.md`. `COUNT_CAPS` now uses `mode="effects"`: rejected requests neither consume the cap nor trigger a breach. This is an explicit scope choice for our policy-compliance evaluation, not a claim that native Tau generally exempts failed attempts. The existing two item tools and order partition are unchanged. No shared-engine change is needed.

The updated selected results are in `eval/reference_v2/presentation_successful_operations/`. They recompute the count predicate over all 328 captured Tau cases, preserve other selected fires, exclude the previously rejected message-format checks, and preserve frozen task labels. This is an auditable projection of the captured replay, not a new end-to-end framework execution. The previous results below remain historical.

### Historical literal once-per-order request cap

The native retail policy says exchange/modify order tools can only be called once per order, and the item-modification section repeats that the action can only be called once. Neither wording exempts rejected requests. The historical v3 `once_per_order` cap used `mode="attempts"` for the existing item-tool group only: `exchange_delivered_order_items` and `modify_pending_order_items`, partitioned by order ID. This was an operational request-count finding; it did not claim a rejected mutation landed. Authentication, status, ownership and other ordinary effect checks still ignored rejected effects, and rejected results never updated observed state. Address/payment operations were not added to this group; an address change before an item modification remained allowed.

This explicit literal interpretation adds the once arm on 27 captured tasks: five GLM positive-reference tasks and 22 Qwen tasks (21 positive, one clean). The clean Qwen ext-236 case makes two rejected exchange requests for the same non-delivered order `#W4967593`, at seq10 and seq12. Both return “Non-delivered order cannot be exchanged.” The repeated requests breach the literal call cap while the frozen labeling prompt excludes failed calls; preserve this reference-criteria mismatch rather than calling it a landed mutation or changing its label. Qwen ext-045 similarly makes a rejected second modification after an earlier successful modification; the second request now has the appropriate request-count witness. The more permissive successful-only reading is no longer silently substituted for the stated call cap.

Regression tests cover a rejected second call after either a successful or rejected first call, distinct order partitions, and allowed address-before-items ordering. Final Tau reducer test checkpoint: 73 passed.
