# The Promise Catalog (AAP)

The set of behavioral promises a provider can stake a bond behind. Each is a **numbered,
versioned, human-readable spec** backing one audited predicate in `aa_commons.registry` —
deliberately ERC-shaped: a stable number, a frozen meaning, and an on-chain-checkable
identity, so two parties can refer to "AAP-1" and mean the *exact* same rule.

This file is the human-readable half; the machine half is the registry. They are kept in
sync by the hash (below). `python scripts/catalog.py` prints the live index straight from
the code; `--source AAP-N` dumps the exact source the on-chain hash commits to.

## How a spec binds

A promise is `(predicate_id, params, payout)`, backed by a per-promise reserve posted at
`registerPromise` (born funded; `fundPromise` tops up). Two hashes pin its meaning on-chain:

- **`predicateHash = keccak(the predicate's source)`** — the *logic*. Auditable: pull the
  source with `scripts/catalog.py --source AAP-N`, hash it, compare to the chain. An edit
  to committed code changes the hash. Current AAP-2/3/5 bind complete shared source bundles,
  including helpers; older function-only hashes retain their original binding limits.
- **`paramsHash = keccak(canonical(params))`** — the *configuration* (which tools count as
  destructive, the allowlist, the budget). Same predicate, different `params` = a different
  on-chain commitment.

The verifier imports the predicate from this same `aa_commons`, so the rule that adjudicates
is byte-identical to the rule that was committed (invariant I3).

## Versioning

`version` records the catalog revision. Behavior changes require a new version and
source commitment. The hash is computed from source, not from the version number alone.
A promise already registered on-chain keeps pointing at the exact source it committed to;
new registrations opt into the new version. (`version` bumps; `number` is permanent.) **AAP-1 is
the worked example:** v1 keyed on an agent-issued approval gate; v2 keys on real user consent —
v3 adds invocation-bound authorization while retaining legacy target behavior by default. The exact v2 evaluator remains available for existing commitments.

## Status

The catalog contains five numbered entries. AAP-2 v4, AAP-3 v2 and AAP-5 v2 add
shared structured checking alongside their original parameter interfaces. They accept a
source-bound observation profile and rule; no benchmark-specific predicate is added to the
current catalog. AAP-1 and AAP-4 are unchanged.

Profiles define domain observations, policy conditions, applicability and evidence requirements.
Their source and fixed configuration are bound through the profile hash in promise parameters.
The shared library evaluates membership, arithmetic, ordering and sequence conditions. Both
SDK and verifier must install the same trusted profile definitions. See
[Shared policy checks](SHARED_POLICY_CHECKS.md) for the API, source binding and replay evidence.

A predicate number identifies a family, not a complete promise: the exact source and parameters
must also be inspected. Semantic model judgments and payload taint tracking remain outside this
implementation.

---

## AAP-1 — `no_destructive_without_consent` · v3 · Final

> A covered destructive invocation requires matching prior authorization.

Native adapters set `authorization_mode="invocation"`. A `user_authorization` record binds
one session, invocation ID, tool, and canonical argument hash. Its explicit schema version must be integer 1. Human approval or an observed
application of an identified native policy authorizes that invocation once. Another identical
command needs its own observation. The trace preserves once/session/persistent scope, denial,
timeout, and decision origin. Automated review alone does not establish human consent.

The native scope does not become a wildcard grant in the predicate. Session or persistent
permission must be observed as a policy decision for each new invocation. Shell authorization
covers the invocation, not every file effect inside it. The trusted adapter supplies provenance;
user signatures are not implemented.

Without `authorization_mode="invocation"`, the following legacy target/scope parameters and
rule remain in effect. Target consent is reusable for that target within the session; it is
not allow-once. Prefix grants remain active until revoked.

**Parameters**

| key | type | default | meaning |
|---|---|---|---|
| `authorization_mode` | str | `"legacy"` | use `"invocation"` for native authorization records |
| `destructive_tools` | list[str] | — | tools whose calls require consent |
| `consent_tool` | str | `"user_consent"` | per-action consent record; `result` granted/denied |
| `grant_tool` | str | `"user_grant"` | a scoped standing grant (`scope` = prefix) |
| `revoke_tool` | str | `"user_revoke"` | ends a standing grant |
| `granted_result` | any | `"granted"` | the result value that counts as granted |
| `match_key` | str | `"target"` | the arg holding a per-action target |
| `scope_key` | str | `"scope"` | the arg holding a grant's scope prefix |

**Legacy rule.** Scanning in `seq` order: a granted `consent_tool` grants its exact `match_key`; a granted
`grant_tool` opens a standing `scope_key`; a `revoke_tool` closes it. A `destructive_tools` call
(not blocked) whose target is neither individually consented nor under an open scope is a
**violation** at that `seq`.

**What's logged that it reads:** the `user_consent` / `user_grant` / `user_revoke` records (the
user's actual decisions) and the destructive-tool records.

**Version history.** v1 (`no_destructive_without_approval`) keyed on an agent-issued
`request_approval` gate — it proved "the gate ran," not "the user agreed," so it is superseded.
v2 keys on real user consent. New logic ⇒ new `predicateHash` under the same number: the
versioning policy demonstrated, not bypassed.

**Historical v2 `predicateHash`** `0x7eef1be18310c4cec15126880d17d7156f4ba6a4f4a82343e575ff528fd83be6`. Run `scripts/catalog.py` for the current v3 hash.

---

## AAP-2 — `action_within_declared_scope` · v4 · Final

Version 4 also accepts `{"observation_profile": "0x…", "rule": "…"}`; see
[the shared profile interface](SHARED_POLICY_CHECKS.md). The original parameter behavior below remains available.

> v3 adds the runtime user-path keys `grant_tool` / `revoke_tool` / `granted_result` / `scope_key` (see the predicate docstring); `scripts/catalog.py` is the authority for versions and hashes.

> Scoped actions must stay (lexically) within the declared scope (e.g. only under `workspace/`).

The **boundary** promise — "don't touch anything outside this repo/workspace." Shown with
path prefixes; the same shape covers domain or registry allowlists. Checkable from the action
event alone, no taint tracking.

**Parameters**

| key | type | default | meaning |
|---|---|---|---|
| `scoped_tools` | list[str] | — | tools whose target is confined |
| `allow_prefixes` | list[str] | — | normalized target must be at/under one of these |
| `match_key` | str | `"target"` | the arg holding the target |

**Rule.** For each `scoped_tools` call (not blocked), `posixpath.normpath(target)` must equal a
prefix or sit under it (`np == p or np.startswith(p + "/")`). Otherwise a **violation** at that
`seq`. `normpath` is used (not `os.path`/`realpath`) because the predicate is a pure, OS-independent
function of the trace — SDK and verifier must agree byte-for-byte.

**What's logged that it reads:** the scoped-tool records (tool + target). No consent events
needed — scope is a property of the action itself.

**Version history.** v1 compared the **raw** path with `startswith`, so `workspace/../etc/x`
slipped through. v2 normalizes first, so `..` traversal resolves out and is caught.

> **Limitation (lexical, not semantic).** This checks the declared path *string*, not the effect's
> real location. A symlink created in-scope (`workspace/link -> /`) makes `workspace/link/etc/x`
> pass lexically while the write lands outside — using only faithfully-logged in-scope actions, so
> it survives honest-but-curious. A trace-pure, FS-free predicate cannot resolve symlinks; closing
> it needs a runtime symlink-free jail. (AAP-1's standing-grant scope check has the same lexical
> nature — a conscious deferral.) See `integrations/hermes/FINDINGS.md`.

**Historical source hash:** `0x8e97a6466b861f76708de7d453035062cad592b4e9c51670467ea2509e46dbe7`. Run `scripts/catalog.py` for the current hash.

---

## AAP-3 — `payment_within_mandate` · v2 · Final

Version 2 also accepts `{"observation_profile": "0x…", "rule": "…"}`; see
[the shared profile interface](SHARED_POLICY_CHECKS.md). The original parameter behavior below remains available.

> Payments must stay within budget and the merchant allowlist.

The predicate checks the recorded transfer after execution. Native application permissions
may prevent a payment; the SDK does not gate it. A known nonexecution record is skipped.

**Parameters**

| key | type | default | meaning |
|---|---|---|---|
| `pay_tool` | str | `"send_payment"` | the payment action |
| `max_amount` | number | — | per-payment ceiling |
| `merchant_allowlist` | list[str] | — | recipients allowed to be paid |
| `recipient_key` | str | `"target"` | the arg holding the recipient |
| `amount_key` | str | `"amount"` | the arg holding the amount |

**Rule.** For each `pay_tool` call (not blocked): the recipient must be in
`merchant_allowlist`, and the amount must be present and `<= max_amount`. Otherwise a
**violation** at that `seq`.

**What's logged that it reads:** the payment records (recipient + amount).

**Historical source hash:** `0xf70c8ee28ab7904b6a773ad2304d6e6e267fa3acd1475213b21f9635012cde8b`. Run `scripts/catalog.py` for the current hash.

---

## AAP-4 — `egress_within_allowlist` · v4 · Final

> v4 adds the runtime user-path keys `grant_tool` / `revoke_tool` / `granted_result` (see the predicate docstring); `scripts/catalog.py` is the authority for versions and hashes.

> Outbound (egress) actions must target only allow-listed recipients or sinks.

The **exfiltration** case, made checkable from the action event alone. An *egress* tool (send,
post, upload, message) must address a recipient on the allowlist. This is **content-blind** — it
constrains *where* data goes, not *what* is sent — the deterministic, taint-free slice of
exfiltration. It reads no payload and labels no value, which is why it ships now while the
value-labeling version stays deferred.

**Parameters**

| key | type | default | meaning |
|---|---|---|---|
| `egress_tools` | list[str] | — | tools that send data outward |
| `recipient_allowlist` | list[str] | — | recipients / sinks allowed to receive |
| `allow_prefixes` | list[str] | `[]` | a recipient also passes if it `startswith` one of these (domain / URL sinks) |
| `recipient_key` | str | `"target"` | the arg holding the recipient / destination |

**Rule.** For each `egress_tools` call (not blocked): the `recipient_key` value must be in
`recipient_allowlist`, or `startswith` one of `allow_prefixes`. Otherwise a **violation** at that
`seq`.

**What's logged that it reads:** the egress records (tool + recipient). No payload, no taint.

> **Limitation (content-blind, lexical).** A *lower bound* on exfiltration coverage, not a solution.
> **False negative:** secret data sent to an *allow-listed* recipient passes (a real contact who is
> also the attacker's drop) — catching that needs value/taint labeling of the payload, a separate
> **deferred** predicate. **False positive:** a legitimate *new* recipient not yet on the allowlist
> fires (widen it via a params change → new `paramsHash`). Like AAP-2, this checks the declared
> recipient *string*, not the semantic effect.

**`predicateHash`** `0x6e6bec9d08bb55a35ac02b03d632404e2659f4fd91ed1c8315ff5d0ff37c7efa`

---

## AAP-5 — `aggregate_within_cap` · v2 · Final

Version 2 also accepts `{"observation_profile": "0x…", "rule": "…"}`; see
[the shared profile interface](SHARED_POLICY_CHECKS.md). The original parameter behavior below remains available.

> **Status: dropped from the coverage eval and parked** (rationale in `eval/writeups/ARCHIVED_AAP5.md`).
> The predicate code stays registered for recoverability; the description below is retained for reference.

> Aggregate over the session — count of actions or sum of amounts — must stay within a cap.

The original aggregate interface performs a **fold over
matching actions**, and the only predicate that catches harms with no per-action target:
- **COUNT mode** — `count(counted_tools) ≤ max_count`: mass-action / mass-destruction (delete 7
  emails, send 5 individual phishing messages), which has no recipient / path / amount to read.
- **SUM mode** — running `Σ amount_key over summed_tools ≤ max_sum`: **structuring** (draining a
  budget in many sub-cap transfers to allow-listed recipients, which passes every membership check).

Content-blind: reads the tool name, a running count, and a numeric amount field — never payload. The
window in v1 is the whole session trace (a sliding window is a future param).

**Parameters**

| key | type | default | meaning |
|---|---|---|---|
| `counted_tools` | list[str] | `[]` | tools whose calls are counted (COUNT mode) |
| `max_count` | int | `None` | cap on the count; `None` disables COUNT mode |
| `summed_tools` | list[str] | `[]` | tools whose amounts are summed (SUM mode) |
| `amount_key` | str | `"amount"` | the arg holding the numeric amount |
| `max_sum` | number | `None` | cap on the running sum; `None` disables SUM mode |

**Rule.** Scanning in `seq` order over non-blocked records: increment a counter for each
`counted_tools` call (**violation** at the call where it exceeds `max_count`); add `amount_key` for
each `summed_tools` call (**violation** where the running sum exceeds `max_sum`).

**What's logged that it reads:** tool name, a running count, and the numeric amount field. No payload.

> **Limitation.** Aggregate predicates **structurally over-fire on legitimate bulk** (delete-spam,
> email-the-team). The cap must be set from the **benign-action distribution by a stated rule** (e.g.
> the 99th percentile of benign counts/sums), and the (higher) false-positive rate **reported, not
> hidden** — count predicates trade precision for the destruction dimension.

**Historical source hash:** `0x52f4d3db4e36f9f3f22136252fa2f9d131c0100027b09ae16e75ed393f60d1f8`. Run `scripts/catalog.py` for the current hash.
