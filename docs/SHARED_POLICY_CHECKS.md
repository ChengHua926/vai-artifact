# Shared policy checks

AAP-2, AAP-3 and AAP-5 support source-bound observation profiles in addition to their original parameters. A profile interprets an application's recorded tools and policy definitions. The shared library evaluates its explicit conditions. Current Tau and ClawsBench replay uses these catalog entries; it no longer registers a current benchmark-specific predicate.

## What is shared

| Catalog entry | Current version | Structured checks |
|---|---:|---|
| AAP-2, action scope | 4 | Membership, state preconditions, equality and ordering constraints |
| AAP-3, payment mandate | 2 | Recipient restrictions, allowed payment composition and exact amount relationships |
| AAP-5, aggregate cap | 2 | Counts, sums, per-object caps and consecutive-result limits |

AAP-1 and AAP-4 are unchanged. The original parameters of AAP-2/3/5 retain their behavior. The SDK snapshots source and parameter hashes at registration and resolves that source for each self-check; later parameter mutation is rejected. Their previous implementations remain resolvable by their original source hashes.

`aa_commons.constraints` supplies comparisons, collections, Boolean composition and exact decimal arithmetic. `aa_commons.sequence_checks` tracks consecutive successful operations with prior observed identical results. `aa_commons.policy_engine` evaluates conditions and emits the rule and offending step. None imports benchmark code or labels. Missing expressions, malformed operations and non-Boolean conditions raise rather than becoming an automatic pass.

Profiles still contain domain policy definitions, applicability, evidence requirements and observation reconstruction. For example, the airline profile determines which tool result describes a booking and which allowance table applies. The shared checker computes the permitted baggage count and compares it with the observed count. Moving comparisons into shared code does not make policy interpretation automatic.

## Registration and binding

Trusted application code registers an `ObservationProfile` with an ID, version, observation builder, fixed configuration, rule-to-family map and complete source bundle. It must include transitive domain helpers. Registration stores an immutable serialized descriptor; callers receive copies of configuration.

A promise then uses an ordinary catalog ID with parameters:

```python
params = {"observation_profile": profile.profile_hash, "rule": "the_rule"}
accountability.register_promise("action_within_declared_scope", params, payout_wei=1)
```

The commitments bind both layers:

1. `predicateHash` binds the shared evaluator and its helper sources.
2. `paramsHash` binds the exact profile hash and selected rule.
3. The profile hash binds its domain sources, observation builder, policy configuration, supported rules and reason format.
4. `traceHash` binds the records from which operands are reconstructed.

Both SDK and verifier must install and register the same trusted profile definitions. An unknown profile or wrong predicate family raises. Parameters cannot request imports, load a file or execute supplied code. Source commitment makes code changes detectable; it does not prove faithful capture or correct policy translation.

For the benchmark profiles, `eval.reference_v2.runtime.ensure_registered()` performs this installation. `promise_definition(benchmark, arm)` returns the catalog ID and committed parameters. The verifier's existing challenge processor resolves the shared predicate by hash and executes it with those parameters. Profile installation is explicit; no runtime download of code is performed.

## Evaluation evidence

The [shared replay](../eval/reference_v2/results_shared/report.md) compares the existing 388 captured runs against the exact frozen v4 evaluator. It checks every rule verdict, offending record, reason, fire, diagnostic, unsupported-evidence output and trace hash. The compatibility test also removes legacy adapter-computed answers and checks that results are unchanged. Unit tests vary underlying operands independently.

The selected task totals and same-reason attribution remain those in the frozen presentation. Labels, calibration weights and captures are not regenerated. The old `results_v4/` snapshot remains unchanged; new source hashes and promise bindings are saved separately in `results_shared/`.

These are offline replay results. A local SDK test exercises real file operations through `Session.guard`, capture and hash-resolved replay. It does not establish live deployment inside the benchmark agents. Rules lacking trustworthy consent or other required observations retain their previous coverage limitations.

## Historical commitments

AAP-2 v3, AAP-3 v1 and AAP-5 v1 remain in the historical registry. Historical benchmark v4 uses its exact saved source bundle and isolated dependency modules via `eval.reference_v2.legacy_v4`. It is available only by its old hash, not in the current catalog. Updating present-day adapters cannot change its decisions.

## Reproduce

From the prototype repository:

```sh
PYTHONPATH=packages/commons:packages/sdk:packages/verifier \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m eval.reference_v2.replay_shared --check
```

The summary and manifest record verification counts, source hashes, input hashes and promise bindings. This checks the committed captures without model calls, relabeling or chain transactions.

## Verification on September 16, 2026

The affected commons, SDK, verifier, Tau/ClawsBench replay and labeling suites passed 641 tests and 57 subtests, with five optional-fixture skips. The seven selected-presentation artifacts still reproduced exactly. AAP-1 and AAP-4 retained their original hashes; the replaced catalog versions and historical v4 evaluator still resolved their original hashes.

```sh
PYTHONPATH=packages/commons:packages/sdk:packages/verifier:packages/store \
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  packages/commons/tests packages/sdk/tests packages/verifier/tests \
  eval/reference_v2/tests eval/reference_v2/tau/tests \
  eval/clawsbench/analysis/tests eval/labeling/tests -q
```

AgentDojo was not rerun. Its native test dependency is not installed in this environment; the unchanged AAP-4 behavior is covered by the shared-library tests. No additional AgentDojo coverage claim is made by this migration.
