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

MEMBERSHIP, NOT ORDER — and this file used to imply otherwise. ``TIER_COLUMNS`` was a TUPLE spelling
``(id, payload, stage, lineage, source_rowid)``, which reads as a column order while constraining
nothing; the note above already admitted the pin "never constrained a writer". On 2026-08-30 that
ambiguity cost the estate its gold tier: ``scripts/ray_stage_job.py`` held two hand-built orders for
one dataset (the schema it created the destination with, and the schema its transform emitted),
``lance_ray`` casts blocks to the destination BY POSITION, and every tabular cascade died at gold
with ``LanceError(Arrow): … field names are not matching`` over the same five columns.

So the question is settled the other way, deliberately: **a tier's column ORDER is owned by
``service_kit.lakehouse.stage_stamp.stamp_stage``**, and a destination schema is DERIVED from that
stamp rather than rebuilt beside it.

SCOPED HONESTLY: that now holds for the RAY driver (``scripts/ray_stage_job.py``), which is where the
gold tier died. It is NOT yet true of every writer. The in-process driver's blob path still builds the
order by hand — ``medallion/services/compute.py`` ``_carry_forward`` and ``_carry_forward_external``
append ``source_rowid``/``stage`` themselves, and ``transform_stage`` appends ``lineage`` after
``derive_artifacts`` — so one media lane yields two silver schemas depending on
``MEDALLION_RAY_ENABLED``:
    in-process : id, payload, source_rowid, stage, thumbnail, embedding, lineage
    ray driver : id, payload, source_rowid, stage, lineage, thumbnail, embedding
(measured over one real image bronze; they reconverge at gold, which is why it has not bitten). That
divergence PRE-DATES this change and is untouched by it. Routing those three sites through
``stamp_stage`` is what would make the sentence above unconditional; until then, read it as the ray
driver's rule, not the estate's. Promoting a canonical
order here instead would not have removed an order, it would have added one: a governed row also
carries the workload's own columns (``source_uri``, ``sha256``, a blob payload, derived artifacts)
that these five names say nothing about, so this file could only ever pin a prefix and leave the rest
to the writer. It is therefore a ``frozenset``: what every tier CARRIES, in no order at all.
"""

from __future__ import annotations

from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, SOURCE_ROWID_COLUMN, STAGE_COLUMN


#: Columns every governed tier carries, whatever wrote it. A workload adds its own beside these; it
#: does not replace them, and a mover that drops one breaks provenance rather than a format.
#:
#: A SET, so nothing can read a column order out of it — see the module docstring. Where the columns
#: physically sit is `stamp_stage`'s answer, given once for every writer.
TIER_COLUMNS: frozenset[str] = frozenset({"id", "payload", STAGE_COLUMN, LINEAGE_COLUMN, SOURCE_ROWID_COLUMN})

#: The tier a row belongs to. Raw is deliberately absent: raw is the external world, never a governed
#: tier (R23), so a row stamped ``raw`` would be a row the medallion claims to govern and does not.
#:
#: A TUPLE, unlike ``TIER_COLUMNS``, because this one IS ordered: it is the cascade's direction.
GOVERNED_STAGES: tuple[str, ...] = ("bronze", "silver", "gold")

__all__ = ["GOVERNED_STAGES", "LINEAGE_COLUMN", "SOURCE_ROWID_COLUMN", "STAGE_COLUMN", "TIER_COLUMNS"]
