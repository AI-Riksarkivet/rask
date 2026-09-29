"""The JSON columns are JSON, so a filter can reach inside them.

`attributes`, `metadata` and `links` were `pa.string()`: valid JSON in every writer, and entirely
opaque to every reader. The ontology declares per-class attributes with real types
(`free`/`int`/`enum`/`bool`), enforces them at submit and publishes them — and no consumer could ask
"which annotations carry this attribute" without fetching every row and parsing client-side.
Declared, enforced, unqueryable.

These tests run against REAL Lance datasets built from the REAL schemas, because the whole claim is
about what the storage engine can do — a test over an in-memory Arrow table would prove the type
annotation and nothing about the filter.
"""

from __future__ import annotations

import json
from pathlib import Path

import lance
import pyarrow as pa

from annotator.annotations.schema import EMPTY_SCHEMA
from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA


def _schema(fields: object) -> pa.Schema:
    return fields if isinstance(fields, pa.Schema) else pa.schema(fields)


ANNOTATIONS = _schema(EMPTY_SCHEMA)
PUBLISHED = PUBLISHED_LABELS_SCHEMA


def test_the_three_columns_are_JSON_not_string() -> None:
    """The change itself. `# json` in a comment beside `pa.string()` is a wish, not a type."""
    assert ANNOTATIONS.field("metadata").type == pa.json_()
    assert ANNOTATIONS.field("links").type == pa.json_()
    assert PUBLISHED.field("attributes").type == pa.json_()


def test_the_extension_type_survives_ARROW_IPC(tmp_path: Path) -> None:
    """The hop that decides whether this works in production at all.

    The annotator never writes Lance directly — it posts Arrow IPC to the catalog, which writes.
    A type that degraded to string on the wire would leave the published table unfilterable while
    every unit test here still passed.
    """
    import io

    table = pa.Table.from_pylist([{"annotation_id": "a1", "attributes": json.dumps({"order": 4})}], schema=PUBLISHED)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)

    received = pa.ipc.open_stream(pa.BufferReader(sink.getvalue())).read_all()
    assert received.schema.field("attributes").type == pa.json_()

    # And it is still filterable after landing, which is what the catalog does with it.
    ds = lance.write_dataset(received, tmp_path / "ipc.lance")
    assert ds.to_table(filter="json_get_int(attributes, 'order') = 4")["annotation_id"].to_pylist() == ["a1"]
