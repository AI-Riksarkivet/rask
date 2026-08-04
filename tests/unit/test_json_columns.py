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
import pytest
from annotator.annotations.schema import EMPTY_SCHEMA
from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA


def _schema(fields: object) -> pa.Schema:
    return fields if isinstance(fields, pa.Schema) else pa.schema(fields)  # type: ignore[arg-type]


ANNOTATIONS = _schema(EMPTY_SCHEMA)
PUBLISHED = PUBLISHED_LABELS_SCHEMA


def test_the_three_columns_are_JSON_not_string() -> None:
    """The change itself. `# json` in a comment beside `pa.string()` is a wish, not a type."""
    assert ANNOTATIONS.field("metadata").type == pa.json_()
    assert ANNOTATIONS.field("links").type == pa.json_()
    assert PUBLISHED.field("attributes").type == pa.json_()


def _write(tmp_path: Path, schema: pa.Schema, rows: list[dict[str, object]], name: str) -> lance.LanceDataset:
    table = pa.Table.from_pylist(rows, schema=schema)
    return lance.write_dataset(table, tmp_path / name)


def test_a_filter_reaches_INSIDE_the_published_attributes(tmp_path: Path) -> None:
    """The point of the whole change: `order` is a declared, typed, published attribute, and this is
    the first time anything can select on it."""
    ds = _write(
        tmp_path,
        PUBLISHED,
        [
            {"annotation_id": "a1", "attributes": json.dumps({"order": 1, "quality": "good"})},
            {"annotation_id": "a2", "attributes": json.dumps({"order": 9, "quality": "poor"})},
            {"annotation_id": "a3", "attributes": "{}"},
        ],
        "published.lance",
    )

    late = ds.to_table(filter="json_get_int(attributes, 'order') > 3")["annotation_id"].to_pylist()
    assert late == ["a2"]

    good = ds.to_table(filter="json_get_string(attributes, 'quality') = 'good'")["annotation_id"].to_pylist()
    assert good == ["a1"]


def test_a_row_with_NO_such_attribute_is_simply_not_matched(tmp_path: Path) -> None:
    """`{}` is the shapeless-row default `_json_attributes` emits. It must be absent from the result,
    not an error and not a false positive."""
    ds = _write(
        tmp_path,
        PUBLISHED,
        [
            {"annotation_id": "a1", "attributes": json.dumps({"order": 2})},
            {"annotation_id": "a2", "attributes": "{}"},
        ],
        "sparse.lance",
    )

    assert ds.to_table(filter="json_exists(attributes, '$.order')")["annotation_id"].to_pylist() == ["a1"]


def test_a_NULL_column_survives_the_write(tmp_path: Path) -> None:
    """The insert path lets absent columns fall to null (`new_rows`), so a SQL null must round-trip
    as a SQL null — distinct both from `{}` and from the JSON `null` an empty string becomes (see
    `test_an_EMPTY_STRING_becomes_json_null_rather_than_an_error`)."""
    ds = _write(
        tmp_path,
        ANNOTATIONS,
        [{"id": "a1", "shape_type": "bbox", "metadata": '{"k":1}'}, {"id": "a2", "shape_type": "bbox"}],
        "nulls.lance",
    )

    got = ds.to_table()["metadata"].to_pylist()
    assert got == ['{"k":1}', None]


def test_the_annotations_metadata_column_is_filterable(tmp_path: Path) -> None:
    ds = _write(
        tmp_path,
        ANNOTATIONS,
        [
            {"id": "a1", "shape_type": "bbox", "metadata": json.dumps({"batch": "vasa-1"})},
            {"id": "a2", "shape_type": "bbox", "metadata": json.dumps({"batch": "vasa-2"})},
        ],
        "meta.lance",
    )

    assert ds.to_table(filter="json_get_string(metadata, 'batch') = 'vasa-1'")["id"].to_pylist() == ["a1"]


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


@pytest.mark.parametrize("bad", ["not json", "{oops"])
def test_MALFORMED_json_is_refused_at_write(bad: str, tmp_path: Path) -> None:
    """The failure mode this type introduces, pinned.

    Under `pa.string()` any garbage landed happily and only broke whoever parsed it later. Now it
    fails at the write, which is where the writer can still be found.

    `OSError` is not a stray catch: Lance surfaces this as
    `OSError(LanceError(Arrow): Failed to encode JSON)`. Arrow itself accepts the bad string — the
    extension type does not validate on construction — so the refusal happens where the JSONB is
    actually built, at `write_dataset`, not at `Table.from_pylist`.
    """
    table = pa.Table.from_pylist([{"id": "a1", "shape_type": "bbox", "metadata": bad}], schema=ANNOTATIONS)
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError, ValueError, OSError)):
        lance.write_dataset(table, tmp_path / "invalid.lance")


def test_an_EMPTY_STRING_becomes_json_null_rather_than_an_error(tmp_path: Path) -> None:
    """The one input that does NOT behave like the others, pinned because I assumed wrong.

    `""` is not valid JSON, and I expected the write to refuse it as it refuses `{oops`. It does
    not — Lance coerces it to JSON `null`, stored literally as `'null'`. That is a THIRD state
    beside "a real object" and "a SQL null", and the only reason it is harmless is that
    `json_get_*` and `json_exists` both decline to match it, so it can never become a false
    positive.

    Pinned rather than fixed: no writer emits `""` today (every one emits `{}`, `[]` or a
    `json.dumps` result), and pinning the real behaviour beats asserting the behaviour I wanted.
    If a writer ever does emit `""`, this test is where the reader finds out what happens.
    """
    ds = _write(
        tmp_path,
        ANNOTATIONS,
        [
            {"id": "a1", "shape_type": "bbox", "metadata": ""},
            {"id": "a2", "shape_type": "bbox", "metadata": json.dumps({"k": 1})},
        ],
        "empty.lance",
    )

    assert ds.to_table()["metadata"].to_pylist() == ["null", '{"k":1}']
    # Not a SQL null, and not a match either way — the row is simply never selected.
    assert ds.to_table(filter="json_get_int(metadata, 'k') = 1")["id"].to_pylist() == ["a2"]
    assert ds.to_table(filter="json_exists(metadata, '$.k')")["id"].to_pylist() == ["a2"]
