"""SK-11 — two columnLineage builders and two different tuples both called `ColumnEdge`.

`service_kit.openlineage` owns the shared builder over a SEVEN-tuple; `service_kit.lancekit.openlineage`
carried a private `_column_lineage_facet` over a THREE-tuple that it also called `ColumnEdge`. One
distribution, one name, two incompatible shapes — which one a `list[ColumnEdge]` held depended on
which module the reader had open.

The duplicate was not merely redundant. It pinned `_schemaURL` from its own alias, so a spec bump
could apply to one emitter and not the other; and it emitted a facet for edges with an empty output
or input field, which is exactly the junk `(:Column {field:""})` the shared builder was written to
refuse. The write's narrow shape is now WIDENED onto the wire shape and handed to the one builder.
"""

from __future__ import annotations

from typing import Any

from service_kit.lancekit import openlineage as lancekit_ol
from service_kit.lancekit.openlineage import PRODUCER, WriteResult, build_run_event
from service_kit.openlineage import COLUMN_LINEAGE_FACET_SCHEMA_URL, column_lineage_facet


def _event(column_map: list[tuple[str, str, str]], inputs: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    return build_run_event(
        operation="MERGE_INSERT",
        job_namespace="media",
        job_name="annotate.merge_insert",
        inputs=[("media", "unit-1")] if inputs is None else inputs,
        output_namespace="media",
        output_name="annotations",
        event_time="2026-08-29T00:00:00+00:00",
        result=WriteResult(version=1, row_count=1, size_bytes=0, fields=[], column_map=column_map),
    )


def test_there_is_only_one_builder_and_one_name_for_each_shape() -> None:
    import service_kit.openlineage as shared_ol

    assert not hasattr(lancekit_ol, "_column_lineage_facet")
    # `ColumnEdge` now means ONE thing everywhere: the wire seven-tuple. The narrow write shape has
    # its own name, so `list[ColumnEdge]` no longer depends on which module the reader had open.
    assert lancekit_ol.ColumnEdge is shared_ol.ColumnEdge
    assert lancekit_ol.ColumnMapEdge == tuple[str, str, str]
    assert lancekit_ol.ColumnMapEdge != shared_ol.ColumnEdge


def test_a_well_formed_edge_reaches_the_shared_builder_unchanged() -> None:
    facet = _event([("id", "id", "IDENTITY"), ("shape", "polygon", "TRANSFORMATION")])["outputs"][0]["facets"]["columnLineage"]
    assert facet == column_lineage_facet(
        PRODUCER,
        [
            ("id", "media", "unit-1", "id", "DIRECT", "IDENTITY", False),
            ("shape", "media", "unit-1", "polygon", "DIRECT", "TRANSFORMATION", False),
        ],
    )
    assert facet["_schemaURL"] == COLUMN_LINEAGE_FACET_SCHEMA_URL
    assert facet["fields"]["id"]["inputFields"][0]["transformations"] == [{"type": "DIRECT", "subtype": "IDENTITY", "masking": False}]


def test_an_edge_with_no_output_field_no_longer_materialises_a_junk_column() -> None:
    assert "columnLineage" not in _event([("", "id", "IDENTITY")])["outputs"][0]["facets"]


def test_an_edge_with_no_input_field_no_longer_materialises_a_junk_column() -> None:
    assert "columnLineage" not in _event([("id", "", "IDENTITY")])["outputs"][0]["facets"]


def test_a_run_with_no_input_dataset_emits_no_column_facet() -> None:
    """The old private builder wrote `namespace: "", name: ""` inputFields in this case."""
    assert "columnLineage" not in _event([("id", "id", "IDENTITY")], inputs=[])["outputs"][0]["facets"]


def test_a_good_edge_still_survives_beside_a_malformed_one() -> None:
    facet = _event([("", "id", "IDENTITY"), ("shape", "polygon", "TRANSFORMATION")])["outputs"][0]["facets"]["columnLineage"]
    assert list(facet["fields"]) == ["shape"]
