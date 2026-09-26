"""Embed lineage coordinates *into the Lance file itself* at table creation (#21).

The catalog already links a table to its lineage by a convention — the canonical id is the lineage
``Dataset`` name and the ``WROTE`` edge carries the Lance version. But nothing is written *into the
data*, so a copied/moved Lance dataset loses its lineage coordinates. Here we stamp those coordinates
onto the Arrow **schema metadata** of the create payload, so the Lance file is **self-describing**:
``lineage.dataset_id`` / ``lineage.namespace`` / ``lineage.create_run_id`` can be read straight off the
table and reconciled to the lineage graph without the catalog. (The creator's identity is deliberately
NOT embedded — see below — but stays recoverable via the run id.)

``create_run_id`` is the id the catalog generated for the create. It does NOT resolve to a Run node: a
create changes the table's definition and nothing executed, so it goes on the wire as an OpenLineage
``DatasetEvent`` which has no run ([[LIN-004]]). That costs nothing a reader had — `prune_runs` deletes
Run nodes anyway, so this pointer was never durable, while the ``(:User)-[:CREATED]->(:Dataset)`` edge
"is never removed — it IS the creator's record" (`cypher.py`; measured on the live graph 2026-09-11:
1150 CREATED edges, 1145 of 1247 datasets carrying one).

**THE RESOLVABLE COORDINATE IS ``lineage.dataset_id``**, and the **creator stays recoverable** through
it: the lineage service's FGA-gated ``/creator`` endpoint takes a DATASET name and reads that CREATED
edge, which the static-metadata ingest writes exactly as the run-shaped create did. We deliberately do
**not** stamp the creator's
OIDC ``sub`` into the file: it is identity/PII and the Lance file lives on object storage *outside* the
OpenFGA boundary, so embedding it would leak the principal to anyone with raw bucket access. (#22 audit)
"""

from __future__ import annotations

import pyarrow as pa


#: Schema-metadata keys; the lineage service / a consumer reads these straight off the Lance table. Only
#: opaque coordinates — no identity/PII (the creator is resolved via the gated /creator endpoint).
_KEY_DATASET_ID = "lineage.dataset_id"
_KEY_NAMESPACE = "lineage.namespace"
_KEY_CREATE_RUN_ID = "lineage.create_run_id"


def build_lineage_metadata(*, table_id: str, namespace: str, run_id: str) -> dict[str, str]:
    """The lineage coordinates to stamp into the Lance file's schema metadata at create (no PII)."""
    return {
        _KEY_DATASET_ID: table_id,
        _KEY_NAMESPACE: namespace,
        _KEY_CREATE_RUN_ID: run_id,
    }


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
