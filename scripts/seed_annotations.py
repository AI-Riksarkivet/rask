"""Seed the ``annotations`` table for the annotator wire — local, and the catalog.

Writes ``<db>/annotations.lance`` with a few sample shapes on a real frame. The
contract columns come STRAIGHT from the backend's ``EMPTY_SCHEMA`` (the single source
of truth — ``services/annotator/annotations/schema.py``); this script only prepends the
demo descriptor's identity columns. A test asserts the seeded dataset matches the
composition, so drift fails loudly.

When ``MEDIA_CATALOG_URL`` is set, the SAME rows are also seeded into the lance-ns
catalog (``create?mode=overwrite`` — the catalog analogue of the local overwrite),
under ``MEDIA_CATALOG_NAMESPACE`` (default: the db stem). That keeps catalog-mode
E2E runs seeded through the governed path, mirroring the local fixture exactly.

    uv run python scripts/seed_annotations.py [db_path] [doc_id]
"""

from __future__ import annotations

import io
import os
import sys
from datetime import UTC, datetime

import lance
import pyarrow as pa
from annotator.annotations.schema import EMPTY_SCHEMA


SCHEMA = pa.schema(
    [
        # identity (the demo descriptor's key fields) — the ONLY columns this script
        # defines; everything else is the backend contract, imported verbatim.
        ("doc_id", pa.string()),
        ("speech_id", pa.int64()),
        ("chunk_id", pa.int64()),
        ("frame_idx", pa.int64()),
        *EMPTY_SCHEMA,
    ]
)


def sample_table(doc_id: str) -> pa.Table:
    """3 sample rows: 2 model predictions (varying confidence/uncertainty) + 1 human-accepted."""
    now = datetime.now(UTC)
    rows = {
        "doc_id": [doc_id] * 3,
        "speech_id": [0] * 3,
        "chunk_id": [19] * 3,
        "frame_idx": [0] * 3,
        "id": ["a1", "a2", "a3"],
        "shape_type": ["rectangle", "rectangle", "polygon"],
        "x": [40.0, 210.0, 0.0],
        "y": [40.0, 150.0, 0.0],
        "width": [160.0, 120.0, 0.0],
        "height": [90.0, 80.0, 0.0],
        "rotation": [0.0, 0.0, 0.0],
        "polygon": [[], [], [120.0, 240.0, 260.0, 250.0, 190.0, 320.0]],
        "t_start": [0.0, 0.0, 0.0],  # image annotations have no time axis
        "t_end": [0.0, 0.0, 0.0],
        "text": ["regeringen", "principmodellen", "region"],
        # The TEXTUAL facet. Empty here on purpose: none of these three rows is a span, and a
        # non-span row must carry `-1` rather than 0 — 0 is a meaningful offset (the first
        # character), so zeros would seed three zero-length spans pointing at nothing.
        "parent_id": ["", "", ""],
        "char_start": [-1, -1, -1],
        "char_end": [-1, -1, -1],
        "label": ["text-line", "text-line", "figure"],
        "status": ["prediction", "prediction", "accepted"],
        "source": ["model:htr-trocr@v1", "model:htr-trocr@v1", "manual"],
        "reviewer": ["", "", "gabriel"],
        "confidence": [0.88, 0.61, 1.0],  # a2 is low-confidence → top of the review queue
        "uncertainty": [0.18, 0.72, 0.0],
        "model_version": ["htr-trocr@v1", "htr-trocr@v1", ""],
        "group": ["lines", "lines", "figures"],
        "group_id": ["", "", ""],
        "reading_order": [0, 1, -1],
        "difficult": [False, True, False],
        "links": ["[]", "[]", "[]"],
        "mask": ["", "", ""],
        "metadata": ["{}", "{}", "{}"],
        "created_at": [now] * 3,
        "updated_at": [now] * 3,
    }
    return pa.table(rows, schema=SCHEMA)


def seed(db_path: str, doc_id: str) -> str:
    """OVERWRITE ``<db_path>/annotations.lance`` with the sample rows; returns the uri."""
    uri = f"{db_path.rstrip('/')}/annotations.lance"
    lance.write_dataset(
        sample_table(doc_id),
        uri,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return uri


def seed_catalog(catalog_url: str, namespace: str, doc_id: str) -> str:
    """Seed the SAME rows through the catalog: namespace create (idempotent) + table
    ``create?mode=overwrite`` with the Arrow-IPC stream body — the governed analogue
    of the local overwrite, so catalog-mode E2E starts from the identical fixture."""
    import httpx

    table = sample_table(doc_id)
    buf = io.BytesIO()
    with pa.ipc.new_stream(buf, table.schema) as writer:
        writer.write_table(table)
    table_id = f"{namespace}$annotations"
    # Auth-on catalogs need a bearer — same env the catalog-mode transports honor.
    token = os.environ.get("MEDIA_CATALOG_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=catalog_url, timeout=30, headers=headers) as client:
        ns = client.post(f"/v1/namespace/{namespace}/create", json={})
        if ns.status_code >= 400 and "exist" not in ns.text.lower():
            ns.raise_for_status()  # a REAL failure, not the idempotent already-exists
        created = client.post(
            f"/v1/table/{table_id}/create",
            params={"mode": "overwrite"},
            content=buf.getvalue(),
            headers={"Content-Type": "application/vnd.apache.arrow.stream"},
        )
        created.raise_for_status()
        # Re-assert the maintenance policy: policies live OUTSIDE the table
        # (control-root records), but re-seeding should converge the whole state.
        client.post(
            f"/v1/table/{table_id}/policy/set",
            json={"retention_days": 30, "retain_versions": 100, "compact_enabled": True},
        ).raise_for_status()
    return table_id


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "transcripts_v2.lance"
    doc = sys.argv[2] if len(sys.argv) > 2 else "fe00cd746463ad2c"
    out = seed(db, doc)
    ds = lance.dataset(out)
    print(f"seeded {ds.count_rows()} annotations → {out} (v{ds.version})")
    print("columns:", [f.name for f in ds.schema])
    catalog_url = os.environ.get("MEDIA_CATALOG_URL", "")
    if catalog_url:
        namespace = os.environ.get("MEDIA_CATALOG_NAMESPACE") or db.rstrip("/").rsplit("/", 1)[-1].removesuffix(".lance")
        table_id = seed_catalog(catalog_url, namespace, doc)
        print(f"seeded the catalog too → {catalog_url} {table_id} (mode=overwrite)")
