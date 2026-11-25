"""The segments on a store that hold the estate's own state, never a table's bytes ([[LH-532]]): each spelled here once.

One set, read by every plane that judges a path: the catalog's destructive drop refuses a location crossing one
(``catalog.services.tables.destroy``), the maintenance purge refuses a trash record whose location crosses one
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

from typing import Final


__all__ = [
    "ANNOUNCEMENT_INTENTS",
    "BACKUPS",
    "BASES",
    "BRANCH_TRASH",
    "BUCKET_CLAIMS",
    "CLONE_ENDINGS",
    "COMMIT_AUTHORS",
    "COMMIT_MARKS",
    "COMMIT_PENDING",
    "COMMIT_TURNS",
    "CONTRACTS",
    "CONTROL_OUTBOX",
    "CONTROL_PREFIXES",
    "DECLARED_GRANTS",
    "ERASURE_OBLIGATIONS",
    "ERASURE_RELEASES",
    "FILE_IDENTITIES",
    "HISTORY_FENCES",
    "IDEMPOTENCY",
    "LABELS",
    "LABEL_DEFINITIONS",
    "LEASE_PREFIXES",
    "LINEAGE_OUTBOX",
    "LOCATIONS",
    "MAINTENANCE_DUE",
    "MAINTENANCE_FILES",
    "NAMESPACE_INDEX",
    "POLICIES",
    "PRINCIPALS",
    "PROJECTS",
    "PROTECTION",
    "QUEUE_PREFIXES",
    "RECORD_PREFIXES",
    "ROLES",
    "STAGING",
    "TABLE_INPUTS",
    "TIMERS",
    "TRASH",
    "WAREHOUSES",
]


# The records: one object each, the estate's governance and bookkeeping.
TRASH: Final = "_trash"
#: The branches a recoverable delete hid, one record a branch under its table's uuid ([[LH-429]]).
BRANCH_TRASH: Final = "_branch_trash"
PROJECTS: Final = "_projects"
WAREHOUSES: Final = "_warehouses"
#: Which project owns each bucket, one write-once claim a bucket, under the warehouse registry's own prefix.
BUCKET_CLAIMS: Final = f"{WAREHOUSES}/bucket-claims"
POLICIES: Final = "_policies"
PROTECTION: Final = "_protection"
BASES: Final = "_bases"
LOCATIONS: Final = "_locations"
MAINTENANCE_DUE: Final = "_maintenance_due"
COMMIT_AUTHORS: Final = "_commit_authors"
COMMIT_MARKS: Final = "_commit_marks"
FILE_IDENTITIES: Final = "_file_identities"
#: Maintenance's own copy of the committed-file records it has read.
MAINTENANCE_FILES: Final = "_maintenance_files"
CLONE_ENDINGS: Final = "_clone_endings"
ERASURE_OBLIGATIONS: Final = "_erasure_obligations"
ERASURE_RELEASES: Final = "_erasure_releases"
#: A table's data contract, keyed by its uuid (ADR 0212).
CONTRACTS: Final = "_contracts"
#: A project's label definitions, and the labels attached to each table or namespace (ADR 0211).
LABEL_DEFINITIONS: Final = "_label_definitions"
LABELS: Final = "_labels"
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
#: The commits a door was about to make, one entry a request, which the commit reconciler reads its tables from ([[XC-204]]).
COMMIT_PENDING: Final = "_commit_pending"
HISTORY_FENCES: Final = "_history_fences"
IDEMPOTENCY: Final = "_idempotency"
#: The window each fixed timer last claimed, one record a timer (``lakehouse.timers``).
TIMERS: Final = "_timers"

#: The namespace backend's own index of a root: a Lance dataset, not a record (``lance-dir.md``).
NAMESPACE_INDEX: Final = "__manifest"
#: Where a distributed write lands its output before the merge that converges it into the tier.
STAGING: Final = "_staging"
#: The control-root backups.
BACKUPS: Final = "_backups"


RECORD_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        TRASH,
        BRANCH_TRASH,
        PROJECTS,
        WAREHOUSES,
        POLICIES,
        PROTECTION,
        BASES,
        LOCATIONS,
        MAINTENANCE_DUE,
        COMMIT_AUTHORS,
        COMMIT_MARKS,
        FILE_IDENTITIES,
        MAINTENANCE_FILES,
        CLONE_ENDINGS,
        ERASURE_OBLIGATIONS,
        ERASURE_RELEASES,
        CONTRACTS,
        TABLE_INPUTS,
        ROLES,
        PRINCIPALS,
        DECLARED_GRANTS,
        LABEL_DEFINITIONS,
        LABELS,
    }
)
"""The records a control-root backup copies and a restore brings back."""

QUEUE_PREFIXES: Final[frozenset[str]] = frozenset({LINEAGE_OUTBOX, CONTROL_OUTBOX, ANNOUNCEMENT_INTENTS})
"""The outboxes and the announcements filed for them: a restore would publish events the estate already acted on, so a
queue's recovery is to be empty."""

LEASE_PREFIXES: Final[frozenset[str]] = frozenset({COMMIT_TURNS, COMMIT_PENDING, HISTORY_FENCES, IDEMPOTENCY, TIMERS})
"""The records that lapse: a restored one would hold a turn, a pending commit, a fence, an answer or a timer's window whose
time has passed."""

CONTROL_PREFIXES: Final[frozenset[str]] = RECORD_PREFIXES | QUEUE_PREFIXES | LEASE_PREFIXES | {NAMESPACE_INDEX, STAGING, BACKUPS}
"""Every segment that holds the estate's own state, which no table's location crosses."""
