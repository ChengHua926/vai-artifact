"""Importing this package registers all built-in predicates.

The catalog (see docs/PROMISES.md / scripts/catalog.py):
  AAP-1  no_destructive_without_consent   — consent (per-action or scoped standing grant)
  AAP-2  action_within_declared_scope     — scope / boundary
  AAP-3  payment_within_mandate           — payment mandate (budget + merchant allowlist)
  AAP-4  egress_within_allowlist          — exfiltration / egress to approved recipients (content-blind)
  AAP-5  aggregate_within_cap             — aggregate count/sum within a per-session cap
Structural shapes: AAP-1/2/3/4 are per-action *membership* tests; AAP-5 is the *aggregate* (fold)
axis. All five are in the catalog (AAP-5 reinstated 2026-09-02; it runs wherever a deployment
declares a cap — the earlier parking note is eval/writeups/ARCHIVED_AAP5.md).
(The *content-labeling* exfil predicate — secret-data-to-any-sink, via value/taint labeling —
 remains deferred; AAP-4 ships only the destination-allowlist slice.)
"""
from . import action_within_declared_scope  # noqa: F401
from . import aggregate_within_cap  # noqa: F401
from . import egress_within_allowlist  # noqa: F401
from . import legacy_consent_v2  # noqa: F401 (readable by committed hash, not offered for new registration)
from . import no_destructive_without_consent  # noqa: F401
from . import payment_within_mandate  # noqa: F401
