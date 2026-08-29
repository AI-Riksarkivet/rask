"""The ingest boundary is TYPED: what `models` hands the repository is what the read path hands a client.

`Dataset.fields`, `Dataset.column_edges` and `Dataset.statistics` are the three domain values that cross
from the OpenLineage wire model into `LineageRepository`. They crossed as `list[dict[str, str]]`,
`list[dict[str, Any]]` and a bare 2-tuple, so every consumer re-derived the shape by hand —
`edge["name"]`, `col.get("type", "")`, `stats[0]` — while `schemas.SchemaField` already described the
same columns for the READ path (`dataset_schema` validates the persisted JSON straight into it).

The wire form is unchanged, and one test here pins that: the JSON persisted onto the ``WROTE`` edge must
stay exactly what `dataset_schema` reads back, or every schema already in the graph becomes unreadable.
"""

from __future__ import annotations

import json

from lineage.models import ColumnLineageEdge, Dataset, OutputStatistics
from lineage.schemas import SchemaField


def _output(**facets: object) -> Dataset:
    return Dataset.model_validate({"namespace": "gold", "name": "gold$pages", "facets": facets})


_SCHEMA_FACET = {
    "fields": [
        {"name": "id", "type": "int64"},
        {"name": "payload", "type": "blob", "description": "the bytes"},
    ]
}

_COLUMN_LINEAGE_FACET = {
    "fields": {
        "caption": {
            "inputFields": [
                {
                    "namespace": "silver",
                    "name": "silver$features",
                    "field": "embedding",
                    "transformations": [{"type": "INDIRECT", "subtype": "SORT", "description": "ranked", "masking": True}],
                }
            ]
        }
    }
}


def test_schema_fields_cross_the_boundary_as_the_read_paths_own_model() -> None:
    """`dataset_schema` already returns `SchemaField`; the write path fed the graph loose dicts of the
    same shape, so the two descriptions of one column could drift with nothing to catch it."""
    assert _output(schema=_SCHEMA_FACET).fields == [
        SchemaField(name="id", type="int64"),
        SchemaField(name="payload", type="blob", description="the bytes"),
    ]


def test_the_persisted_wrote_edge_schema_keeps_its_wire_form() -> None:
    """The JSON on the ``WROTE`` edge is read back by `dataset_schema`, so its shape is a storage format,
    not an internal detail — an omitted `description` must stay omitted, never a null."""
    persisted = json.dumps([f.model_dump(exclude_none=True) for f in _output(schema=_SCHEMA_FACET).fields])

    assert json.loads(persisted) == [
        {"name": "id", "type": "int64"},
        {"name": "payload", "type": "blob", "description": "the bytes"},
    ]
    assert [SchemaField.model_validate(f) for f in json.loads(persisted)] == _output(schema=_SCHEMA_FACET).fields


def test_column_lineage_edges_cross_the_boundary_as_a_model() -> None:
    """Seven string-keyed lookups on this value are spread across the repository's ingest and the FGA
    dependency-collector; a typo in any of them is a `KeyError` at ingest time, on a live event."""
    edge = _output(columnLineage=_COLUMN_LINEAGE_FACET).column_edges[0]

    assert isinstance(edge, ColumnLineageEdge)
    assert edge.out_field == "caption"
    assert edge.namespace == "silver"
    assert edge.name == "silver$features"
    assert edge.field == "embedding"
    assert edge.type == "INDIRECT"
    assert edge.subtype == "SORT"
    assert edge.description == "ranked"
    assert edge.masking is True


def test_output_statistics_cross_the_boundary_named_rather_than_positional() -> None:
    """`stats[0]` / `stats[1]` is rows-then-bytes by convention only; swapping them at the one call site
    that persists them would record a size as a row count and pass every type check."""
    stats = _output(outputStatistics={"rowCount": 8, "size": 132}).statistics

    assert stats == OutputStatistics(row_count=8, size_bytes=132)
    assert _output(outputStatistics={"size": 64}).statistics == OutputStatistics(row_count=None, size_bytes=64)
    assert _output(version={"datasetVersion": "1"}).statistics is None
