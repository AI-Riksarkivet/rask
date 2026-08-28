"""Embed lineage coordinates *into the Lance file itself* at table creation (#21).

The catalog already links a table to its lineage by a convention — the canonical id is the lineage
``Dataset`` name and the ``WROTE`` edge carries the Lance version. But nothing is written *into the
data*, so a copied/moved Lance dataset loses its lineage coordinates. Here we stamp those coordinates
onto the Arrow **schema metadata** of the create payload, so the Lance file is **self-describing**:
``lineage.dataset_id`` / ``lineage.namespace`` / ``lineage.create_run_id`` can be read straight off the
table and reconciled to the lineage graph without the catalog. (The creator's identity is deliberately
NOT embedded — see below — but stays recoverable via the run id.)

``create_run_id`` is the *same* run id the catalog emits in the OpenLineage create event, so the file
points at its exact creating run in the graph — and the **creator stays recoverable** from that run via
the lineage service's FGA-gated ``/creator`` endpoint. We deliberately do **not** stamp the creator's
OIDC ``sub`` into the file: it is identity/PII and the Lance file lives on object storage *outside* the
OpenFGA boundary, so embedding it would leak the principal to anyone with raw bucket access. (#22 audit)
"""

from __future__ import annotations

import pyarrow as pa

from service_kit.lancekit.arrow_ipc import encode_arrow_stream


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


def inject_into_arrow_stream(stream: bytes, metadata: dict[str, str]) -> bytes:
    """Return the Arrow IPC stream with ``metadata`` merged into its schema metadata.

    Existing schema metadata is preserved; the ``metadata`` keys win on conflict. The record batches
    are unchanged — only the schema's key/value metadata gains the lineage coordinates, which Lance
    persists as the table's schema metadata at version 1.
    """
    reader = pa.ipc.open_stream(stream)
    table = reader.read_all()
    merged: dict[bytes, bytes] = {
        **(table.schema.metadata or {}),
        **{key.encode(): value.encode() for key, value in metadata.items()},
    }
    table = table.replace_schema_metadata(merged)
    return encode_arrow_stream(table)
