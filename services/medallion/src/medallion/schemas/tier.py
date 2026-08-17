"""What EVERY governed tier carries, whatever workload produced it (R3).

The medallion owns bronze→silver→gold: each tier's identity, the transition between them, and the
lineage those transitions emit. It does **not** own what the rows mean. A tier row is::

    {id, payload, stage, lineage, source_rowid}

and ``payload`` is **opaque** — the transform declares its shape, the tier does not.

WHY THIS EXISTS. ``schemas/htr.py`` (deleted 2026-08-17) pinned a workload-shaped gold contract *inside* the medallion:
``page_key``, ``region_polygons``, ``line_polygons``, ``reading_order``, ``confidences`` — nine of its
eleven columns describe transcribed page images. That makes the cascade a transcription pipeline
wearing a lakehouse's name: a second workload cannot use these tiers without bending its data into
HTR's shape or forking the movers. Audio, tabular records and embeddings have no ``page_width``.

WHAT IS ACTUALLY GENERIC, and why each one earns its place:

``id``
    Row identity within the dataset. Selection is BY id, never by row position — ``read_blobs`` and
    ``take_blobs`` silently drop null rows, so positional pairing misattributes every row after the
    first gap (docs/architecture/lance-blob-v2-findings.md).
``payload``
    The workload's own columns. Opaque here by construction: the moment the medallion names a field
    inside it, the medallion has an opinion about the workload again.
``stage``
    Which tier this row is in, stamped at write. The cascade multiplexes lanes onto shared topics, so
    a row that cannot say which tier it belongs to cannot be routed or audited.
``lineage``
    The producing run's provenance as Lance JSONB (R25b/R26). In the contract because gold is what an
    external consumer takes away: a dataset arriving without provenance forces the consumer back to
    our lineage graph, which is the coupling the consume layer exists to remove. Unlike a projection,
    provenance is unrecoverable once the row leaves the platform.
``source_rowid``
    Row-level provenance to the upstream tier's stable ``_rowid``, rooted at bronze. What makes a gold
    row traceable to the bytes it came from without a join through the graph.

NOTE ON THE OLD PIN. ``GOLD_CONTRACT_COLUMNS`` was imported by NOTHING in production — only two unit
tests, which assert it equals itself. So the "load-bearing contract" never constrained a writer: no
mover is checked against it, and a mover that dropped ``confidences`` would fail no gate. Treat it as
what it is — HTR's declaration of its own payload — not as the tier's schema.
"""

from __future__ import annotations


#: Columns every governed tier carries, whatever wrote it. A workload adds its own beside these; it
#: does not replace them, and a mover that drops one breaks provenance rather than a format.
TIER_COLUMNS: tuple[str, ...] = (
    "id",
    "payload",
    "stage",
    "lineage",
    "source_rowid",
)

#: The tier a row belongs to. Raw is deliberately absent: raw is the external world, never a governed
#: tier (R23), so a row stamped ``raw`` would be a row the medallion claims to govern and does not.
GOVERNED_STAGES: tuple[str, ...] = ("bronze", "silver", "gold")

#: The provenance column, unchanged from the HTR contract — it was always generic, merely declared in a
#: workload-specific file.
LINEAGE_COLUMN = "lineage"

#: Row-level provenance to the upstream tier's stable ``_rowid``.
SOURCE_ROWID_COLUMN = "source_rowid"
