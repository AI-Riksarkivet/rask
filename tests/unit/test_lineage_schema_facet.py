"""§9 P4 — blob-aware lineage SchemaDatasetFacet (the type helper + the medallion emitter)."""

from __future__ import annotations

from typing import cast

import pyarrow as pa
from lance import blob_field

from medallion.schemas.events import build_run_event
from service_kit.lakehouse import schema


def test_type_label_renders_media_types() -> None:
    arrow_schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            blob_field("payload"),
            pa.field("thumbnail", pa.large_binary()),
            pa.field("embedding", pa.list_(pa.float32(), 8)),
            pa.field("caption", pa.string()),
        ]
    )
    labels = {field["name"]: field["type"] for field in schema.facet_fields(arrow_schema)}
    assert labels == {
        "id": "int64",
        "payload": "blob",  # not the verbose extension repr
        "thumbnail": "binary",
        "embedding": "array<float>",
        "caption": "string",
    }


def test_lancekit_mirror_labels_json_the_same_way() -> None:
    # ASSERTS the vendored mirror stays in step: the same column labelled by
    # ``service_kit.lancekit.openlineage`` (the ratch/annotation emit path) and by ``service_kit.lakehouse.schema``
    # (the medallion emit path) must reach the lineage graph as the SAME type string.
    from service_kit.lancekit import openlineage as lancekit_ol

    arrow_schema = pa.schema([pa.field("id", pa.int64()), pa.field("alignments_json", pa.json_())])
    assert lancekit_ol.facet_fields(arrow_schema) == schema.facet_fields(arrow_schema)
    assert lancekit_ol.facet_fields(arrow_schema)[1]["type"] == "json"


def test_build_run_event_carries_schema_facet_on_the_output() -> None:
    fields = [{"name": "payload", "type": "blob"}, {"name": "embedding", "type": "array<float>"}]
    event = build_run_event(
        operation="embed",
        author="data_eng",
        job_namespace="medallion",
        inputs=[("bronze", "events")],
        output_namespace="silver",
        output_name="features",
        version=1,
        schema_fields=fields,
        token="t1",
    )
    output = event["outputs"][0]
    assert output["facets"]["schema"]["fields"] == fields
    assert output["facets"]["schema"]["_schemaURL"].endswith("SchemaDatasetFacet")
    assert "schema" not in event["inputs"][0].get("facets", {})  # inputs carry no schema facet


def test_schema_facet_caps_metadata_bloat(caplog) -> None:
    # ASSERTS (§9 P2, 2026-07-11): >512 fields → the facet carries EXACTLY the first 512 + a
    # schema_facet_truncated warning; the _schemaURL stays the shared spec pin (a shorter fields
    # list is still a valid SchemaDatasetFacet — spec-true truncation, full schema stays readable
    # from storage where the manifest IS the schema).
    import logging

    from service_kit import openlineage as ol

    wide = [{"name": f"c{i}", "type": "int64"} for i in range(ol.FACET_MAX_FIELDS + 88)]
    with caplog.at_level(logging.WARNING):
        facet = ol.schema_facet("producer", wide)
    fields = cast("list[dict[str, str]]", facet["fields"])
    assert len(fields) == ol.FACET_MAX_FIELDS
    assert fields[0]["name"] == "c0" and fields[-1]["name"] == "c511"
    assert facet["_schemaURL"] == ol.SCHEMA_FACET_SCHEMA_URL
    assert any(r.message == "schema_facet_truncated" for r in caplog.records)
