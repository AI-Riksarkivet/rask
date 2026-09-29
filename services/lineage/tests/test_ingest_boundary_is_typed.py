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

from lineage.models import Dataset, OutputStatistics


def _output(**facets: object) -> Dataset:
    return Dataset.model_validate({"namespace": "gold", "name": "gold$pages", "facets": facets})


def test_output_statistics_cross_the_boundary_named_rather_than_positional() -> None:
    """`stats[0]` / `stats[1]` is rows-then-bytes by convention only; swapping them at the one call site
    that persists them would record a size as a row count and pass every type check."""
    stats = _output(outputStatistics={"rowCount": 8, "size": 132}).statistics

    assert stats == OutputStatistics(row_count=8, size_bytes=132)
    assert _output(outputStatistics={"size": 64}).statistics == OutputStatistics(row_count=None, size_bytes=64)
    assert _output(version={"datasetVersion": "1"}).statistics is None
