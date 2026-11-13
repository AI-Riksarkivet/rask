"""The segments on a store that hold the estate's own state, never a table's bytes ([[LH-532]]): each spelled here once.

One set, read by every plane that judges a path: the catalog's destructive drop refuses a location crossing one
(``catalog.services.dataplane``), the maintenance purge refuses a trash record whose location crosses one
(``maintenance.services.purge``), and discovery never descends into one (``maintenance.services.discovery``). A
location crossing a control prefix names a record store, an outbox, a staging area or a backup, so a delete there
destroys the estate's own registries, and a walk into one costs round trips and records coverage gaps that block the
purge's clean-report gate.

Every store that keeps state under a control root takes its prefix from here rather than spelling it, so a store the set
does not name cannot exist. The set is in four classes, which a control-root backup reads
(``scripts/control_root_backup.py``): the records it copies and a restore brings back, the queues it skips because a
restore would publish their events again, the leases and caches it skips because each lapses on the store's clock, and
the three that are not control-root records at all.
"""

from __future__ import annotations

from typing import Final


# The records: one object each, the estate's governance and bookkeeping.
TRASH: Final = "_trash"
PROJECTS: Final = "_projects"
WAREHOUSES: Final = "_warehouses"
POLICIES: Final = "_policies"
PROTECTION: Final = "_protection"
BASES: Final = "_bases"
LOCATIONS: Final = "_locations"
GATES: Final = "_gates"
MAINTENANCE_DUE: Final = "_maintenance_due"
COMMIT_AUTHORS: Final = "_commit_authors"
COMMIT_MARKS: Final = "_commit_marks"
FILE_IDENTITIES: Final = "_file_identities"
#: Maintenance's own copy of the committed-file records it has read.
MAINTENANCE_FILES: Final = "_maintenance_files"
CLONE_ENDINGS: Final = "_clone_endings"
ERASURE_OBLIGATIONS: Final = "_erasure_obligations"
ERASURE_RELEASES: Final = "_erasure_releases"
PROMOTIONS: Final = "_promotions"
TABLE_INPUTS: Final = "_table_inputs"
ROLES: Final = "_roles"
PRINCIPALS: Final = "_principals"
#: The tuples the catalog wrote from what git declares, one record each ([[XC-213]]).
DECLARED_GRANTS: Final = "_declared_grants"

# The queues: events staged before they are published.
LINEAGE_OUTBOX: Final = "_lineage_outbox"
CONTROL_OUTBOX: Final = "_control_outbox"
#: A door's announcements, filed before the change they announce and removed once announced ([[LH-576]]).
ANNOUNCEMENT_INTENTS: Final = "_announcement_intents"

# The leases and caches: each record holds for a time judged on the store's clock, then lapses.
COMMIT_TURNS: Final = "_commit_turns"
HISTORY_FENCES: Final = "_history_fences"
IDEMPOTENCY: Final = "_idempotency"

#: The namespace backend's own index of a root: a Lance dataset, not a record (``lance-dir.md``).
NAMESPACE_INDEX: Final = "__manifest"
#: Where a distributed write lands its output before the merge that converges it into the tier.
STAGING: Final = "_staging"
#: The control-root backups.
BACKUPS: Final = "_backups"


RECORD_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        TRASH,
        PROJECTS,
        WAREHOUSES,
        POLICIES,
        PROTECTION,
        BASES,
        LOCATIONS,
        GATES,
        MAINTENANCE_DUE,
        COMMIT_AUTHORS,
        COMMIT_MARKS,
        FILE_IDENTITIES,
        MAINTENANCE_FILES,
        CLONE_ENDINGS,
        ERASURE_OBLIGATIONS,
        ERASURE_RELEASES,
        PROMOTIONS,
        TABLE_INPUTS,
        ROLES,
        PRINCIPALS,
        DECLARED_GRANTS,
    }
)
"""The records a control-root backup copies and a restore brings back."""

QUEUE_PREFIXES: Final[frozenset[str]] = frozenset({LINEAGE_OUTBOX, CONTROL_OUTBOX, ANNOUNCEMENT_INTENTS})
"""The outboxes and the announcements filed for them: a restore would publish events the estate already acted on, so a
queue's recovery is to be empty."""

LEASE_PREFIXES: Final[frozenset[str]] = frozenset({COMMIT_TURNS, HISTORY_FENCES, IDEMPOTENCY})
"""The records that lapse: a restored one would hold a turn, a fence or an answer whose time has passed."""

CONTROL_PREFIXES: Final[frozenset[str]] = RECORD_PREFIXES | QUEUE_PREFIXES | LEASE_PREFIXES | {NAMESPACE_INDEX, STAGING, BACKUPS}
"""Every segment that holds the estate's own state, which no table's location crosses."""
