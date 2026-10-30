"""The segments on a store that hold the estate's own state, never a table's bytes ([[LH-532]]).

One set, read by every plane that judges a path: the catalog's destructive drop refuses a location crossing one
(``catalog.services.dataplane``), the maintenance purge refuses a trash record whose location crosses one
(``maintenance.services.purge``), and discovery never descends into one (``maintenance.services.discovery``). A
location crossing a control prefix names a record store, an outbox, a staging area or a backup, so a delete there
destroys the estate's own registries, and a walk into one costs round trips and records coverage gaps that block the
purge's clean-report gate.

Each name is the prefix one writer keeps under a control root: the namespace backend's ``__manifest``, service-kit's
record stores (``_trash``, ``_policies``, ``_protection``, ``_bases``, ``_locations``, ``_idempotency``, ``_gates``,
``_maintenance_due``, ``_warehouses``, ``_history_fences``, ``_commit_authors``, ``_commit_marks``, ``_file_identities``),
the catalog's (``_projects``, ``_erasure_obligations``, ``_erasure_releases``, ``_promotions``, ``_table_inputs``), the
lineage and control outboxes (``_lineage_outbox``, ``_control_outbox``), a distributed write's ``_staging`` and the
control-root backups (``_backups``).
"""

from __future__ import annotations

from typing import Final


CONTROL_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "__manifest",
        "_trash",
        "_projects",
        "_warehouses",
        "_policies",
        "_protection",
        "_bases",
        "_locations",
        "_idempotency",
        "_gates",
        "_maintenance_due",
        "_history_fences",
        "_commit_authors",
        "_commit_marks",
        "_file_identities",
        "_erasure_obligations",
        "_erasure_releases",
        "_promotions",
        "_table_inputs",
        "_lineage_outbox",
        "_control_outbox",
        "_staging",
        "_backups",
    }
)
