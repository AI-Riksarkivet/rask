"""Embed lineage coordinates *into the Lance file itself* at table creation (#21).

The catalog already links a table to its lineage by a convention — the canonical id is the lineage
``Dataset`` name and the ``WROTE`` edge carries the Lance version. But nothing is written *into the
data*, so a copied/moved Lance dataset loses its lineage coordinates. Here we stamp those coordinates
onto the Arrow **schema metadata** of the create payload, so the Lance file is **self-describing**:
``lineage.dataset_id`` / ``lineage.namespace`` can be read straight off the table and reconciled to the
lineage graph without the catalog. (The creator's identity is deliberately NOT embedded — see below.)

No run id is stamped: every announcement's run id is derived from its commit ([[LH-497]],
``catalog.core.commit_ids``), the table's uuid in ``lineage.dataset_id`` among its coordinates, so a stamp
made before the commit could only name an id no announcement carries.

**THE RESOLVABLE COORDINATE IS ``lineage.dataset_id``**, and the **creator stays recoverable** through
it: the lineage service's FGA-gated ``/creator`` endpoint takes a DATASET name and reads that CREATED
edge, which the static-metadata ingest writes exactly as the run-shaped create did. We deliberately do
**not** stamp the creator's
principal key into the file: it is identity/PII and the Lance file lives on object storage *outside* the
OpenFGA boundary, so embedding it would leak the principal to anyone with raw bucket access. (#22 audit)
"""

from __future__ import annotations

import pyarrow as pa


#: Schema-metadata keys; the lineage service / a consumer reads these straight off the Lance table. Only
#: opaque coordinates — no identity/PII (the creator is resolved via the gated /creator endpoint).
#: ``lineage.dataset_id`` is the table's uuid, which only the write that brings the table into being knows, so
#: ``catalog.services.dataplane.create_table`` stamps it there (ADR 0173 §10).
_KEY_NAMESPACE = "lineage.namespace"


def build_lineage_metadata(*, namespace: str) -> dict[str, str]:
    """The lineage coordinates to stamp into the Lance file's schema metadata at create (no PII)."""
    return {_KEY_NAMESPACE: namespace}


def stamp_lineage_metadata(table: pa.Table, metadata: dict[str, str]) -> pa.Table:
    """Return ``table`` with ``metadata`` merged into its schema metadata.

    Existing schema metadata is preserved; the ``metadata`` keys win on conflict. The columns are the
    same buffers, not a copy — only the schema's key/value metadata gains the lineage coordinates, which
    Lance persists as the table's schema metadata at version 1.
    """
    merged: dict[bytes, bytes] = {
        **(table.schema.metadata or {}),
        **{key.encode(): value.encode() for key, value in metadata.items()},
    }
    return table.replace_schema_metadata(merged)
