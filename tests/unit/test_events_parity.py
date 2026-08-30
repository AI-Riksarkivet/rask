"""Byte-parity pin for the lineage-kit adoption (gate 5, R21): the wire must not change.

``medallion.schemas.events.build_run_event`` became a thin construction over
``lineage_kit.schemas.RunEvent``. This suite freezes the RETIRED hand-built builder verbatim
(``_legacy_build_run_event`` below) and asserts the new path's output is byte-identical across the full
argument matrix — every lane (bronze produce, media ingest, IIIF ingest, stage transform with
stats/schema/columns/assertions, FAIL, per-project, token-less). ``eventTime`` is the one
non-deterministic field: it is compared for FORMAT and popped before the dict equality.

Also pins migration note #1: ``lineage_kit.run_id_for`` equals ``service_kit.openlineage.run_id_for``
for every seed (the uuid5 namespace was aligned to the lance-ns URL), so redelivery keeps MERGEing onto
the runs already in the graph.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

import lineage_kit
from medallion.schemas.events import build_run_event
from service_kit import openlineage as service_kit_ol
from service_kit.openlineage import (
    DATASOURCE_FACET_SCHEMA_URL,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    RUN_EVENT_SCHEMA_URL,
    VERSION_FACET_SCHEMA_URL,
    column_lineage_facet,
    custom_facet,
    run_id_for,
    schema_facet,
)


_PRODUCER = "https://github.com/Borg93/lance-ns/tree/main/medallion"
_REPO_URL = "https://github.com/Borg93/lance-ns"
_SOURCE_LOCATION_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-1-0/SourceCodeLocationJobFacet.json#/$defs/SourceCodeLocationJobFacet"
_OUTPUT_STATS_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json#/$defs/OutputStatisticsOutputDatasetFacet"
_DATA_QUALITY_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-1-0/DataQualityAssertionsDatasetFacet.json#/$defs/DataQualityAssertionsDatasetFacet"


def _legacy_dataset(
    namespace: str,
    name: str,
    version: int | None = None,
    row_count: int | None = None,
    size_bytes: int | None = None,
    assertions: list[dict[str, Any]] | None = None,
    source_uri: str | None = None,
    schema_fields: Any = None,
    column_edges: list[tuple[str, str, str, str, str, str, bool]] | None = None,
) -> dict[str, Any]:
    ds: dict[str, Any] = {"namespace": namespace, "name": name}
    facets: dict[str, Any] = {}
    if schema_fields:
        facets["schema"] = schema_facet(_PRODUCER, schema_fields)
    if column_edges:
        column_facet = column_lineage_facet(_PRODUCER, column_edges)
        if column_facet:
            facets["columnLineage"] = column_facet
    if version is not None:
        facets["version"] = {"_producer": _PRODUCER, "_schemaURL": VERSION_FACET_SCHEMA_URL, "datasetVersion": str(version)}
    if source_uri:
        facets["dataSource"] = {"_producer": _PRODUCER, "_schemaURL": DATASOURCE_FACET_SCHEMA_URL, "name": source_uri, "uri": source_uri}
    if row_count is not None or size_bytes is not None:
        facets["outputStatistics"] = {"_producer": _PRODUCER, "_schemaURL": _OUTPUT_STATS_FACET_SCHEMA, "rowCount": row_count, "size": size_bytes}
    if assertions:
        facets["dataQualityAssertions"] = {"_producer": _PRODUCER, "_schemaURL": _DATA_QUALITY_FACET_SCHEMA, "assertions": assertions}
    if facets:
        ds["facets"] = facets
    return ds


def _legacy_build_run_event(**kwargs: Any) -> dict[str, Any]:
    """The RETIRED hand-built builder, frozen verbatim (modulo local helper names) — the parity oracle."""
    operation: str = kwargs["operation"]
    author: str | None = kwargs["author"]
    job_namespace: str = kwargs["job_namespace"]
    inputs: list[tuple[str, str]] = kwargs["inputs"]
    output_namespace: str = kwargs["output_namespace"]
    output_name: str = kwargs["output_name"]
    version: int = kwargs.get("version", 1)
    row_count: int | None = kwargs.get("row_count")
    size_bytes: int | None = kwargs.get("size_bytes")
    assertions: list[dict[str, Any]] | None = kwargs.get("assertions")
    source_uri: str | None = kwargs.get("source_uri")
    schema_fields: Any = kwargs.get("schema_fields")
    column_map: list[tuple[str, str, str]] | None = kwargs.get("column_map")
    token: str | None = kwargs.get("token")
    project: str | None = kwargs.get("project")
    event_type: str = kwargs.get("event_type", "COMPLETE")
    error_message: str | None = kwargs.get("error_message")

    lance_fields: dict[str, Any] = {"operation": operation, "version": version}
    if token:
        lance_fields["token"] = token
    if project:
        lance_fields["project"] = project
    run_facets: dict[str, Any] = {"lance": custom_facet(_PRODUCER, **lance_fields)}
    if author:
        run_facets["author"] = custom_facet(_PRODUCER, name=author, sub=author)
    if error_message:
        run_facets["errorMessage"] = {
            "_producer": _PRODUCER,
            "_schemaURL": ERROR_MESSAGE_FACET_SCHEMA_URL,
            "message": error_message,
            "programmingLanguage": "PYTHON",
        }
    if token:
        # The ONE line of this frozen copy that is deliberately re-derived rather than frozen. The
        # freeze pins the WIRE, and the project-qualified runId's seed changed on purpose (`-` → NUL,
        # because a `-` join between two `-`-bearing fields is forgeable — see
        # `medallion.schemas.events.build_run_event`). Left at the old form this copy would assert the
        # collision back into existence, which is the opposite of what a parity gate is for. The
        # project-LESS branch below it stays byte-frozen, and that is the half the graph depends on.
        run_id = run_id_for("\x00".join((project, operation, token)) if project else f"{operation}-{token}")
    else:
        run_id = str(uuid.uuid4())
    failed = event_type.upper() in {"FAIL", "ABORT"}
    column_edges: list[tuple[str, str, str, str, str, str, bool]] | None = None
    if column_map and len(inputs) == 1:
        in_ns, in_name = inputs[0]
        column_edges = [(out_field, in_ns, in_name, in_field, "DIRECT", subtype, False) for out_field, in_field, subtype in column_map]
    outputs = [
        _legacy_dataset(output_namespace, output_name)
        if failed
        else _legacy_dataset(
            output_namespace,
            output_name,
            version=version,
            row_count=row_count,
            size_bytes=size_bytes,
            assertions=assertions,
            source_uri=source_uri,
            schema_fields=schema_fields,
            column_edges=column_edges,
        )
    ]
    return {
        "eventType": event_type,
        "eventTime": datetime.now(UTC).isoformat(),
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {
            "namespace": job_namespace,
            "name": operation,
            "facets": {
                "sourceCodeLocation": {
                    "_producer": _PRODUCER,
                    "_schemaURL": _SOURCE_LOCATION_FACET_SCHEMA,
                    "type": "git",
                    "url": _REPO_URL,
                    "path": "services/medallion",
                    "branch": "main",
                }
            },
        },
        "inputs": [_legacy_dataset(ns, name) for ns, name in inputs],
        "outputs": outputs,
    }


_MATRIX: list[dict[str, Any]] = [
    # The bronze produce head (no inputs, dummy version-1 emit — R23: bronze is the first tier).
    {
        "operation": "lance_ray_ingest",
        "author": "ray",
        "job_namespace": "lance-medallion",
        "inputs": [],
        "output_namespace": "bronze",
        "output_name": "bronze$events",
        "token": "tok1",
    },
    # The media head: one input per source object, blob schema facet, measured stats without size.
    {
        "operation": "ingest_media",
        "author": "ray",
        "job_namespace": "lance-medallion",
        "inputs": [("source", "s3://lake/a.png"), ("source", "s3://lake/b.png")],
        "output_namespace": "bronze-media",
        "output_name": "bronze-media$objects",
        "version": 3,
        "row_count": 2,
        "source_uri": "s3://lake/medallion/bronze-media",
        "schema_fields": [{"name": "payload", "type": "blob"}],
        "token": "tok2",
    },
    # The IIIF head (P7a, re-tiered by R23): one external iiif:// input, bronze blob page dataset out.
    {
        "operation": "iiif-ingest",
        "author": "ray",
        "job_namespace": "lance-medallion",
        "inputs": [("iiif://iiifintern-ai.ra.se", "A0068688")],
        "output_namespace": "bronze",
        "output_name": "bronze$pages",
        "version": 2,
        "row_count": 12,
        "source_uri": "s3://lake/medallion/bronze-pages",
        "schema_fields": [{"name": "payload", "type": "blob"}, {"name": "page_key", "type": "string"}],
        "token": "tok3",
    },
    # A full stage transform: stats + schema + column lineage + quality assertions, per-project.
    {
        "operation": "embed_features",
        "author": "data_eng",
        "job_namespace": "lance-medallion",
        "inputs": [("bronze", "bronze$events")],
        "output_namespace": "silver",
        "output_name": "silver$features",
        "version": 4,
        "row_count": 5,
        "size_bytes": 1234,
        "source_uri": "s3://lake/medallion/silver",
        "schema_fields": [{"name": "id", "type": "int64"}],
        "column_map": [("id", "id", "IDENTITY")],
        "assertions": [{"assertion": "row_count_gt_0", "success": True}],
        "token": "tok4",
        "project": "acme",
    },
    # A FAIL run: bare output, errorMessage facet.
    {
        "operation": "embed_features",
        "author": "data_eng",
        "job_namespace": "lance-medallion",
        "inputs": [("bronze", "bronze$events")],
        "output_namespace": "silver",
        "output_name": "silver$features",
        "token": "tok5",
        "event_type": "FAIL",
        "error_message": "boom",
    },
    # Author-less, token-less defensive fallback (random run id — compared modulo runId).
    {"operation": "op", "author": None, "job_namespace": "ns", "inputs": [], "output_namespace": "bronze", "output_name": "bronze$events"},
]


@pytest.mark.parametrize("kwargs", _MATRIX, ids=lambda kw: f"{kw['operation']}-{kw.get('event_type', 'COMPLETE')}")
def test_wire_is_byte_identical_to_the_legacy_builder(kwargs: dict[str, Any]) -> None:
    new = build_run_event(**kwargs)
    legacy = _legacy_build_run_event(**kwargs)
    # eventTime is now(): assert the format contract, then compare everything else exactly.
    assert datetime.fromisoformat(new.pop("eventTime")).tzinfo is not None
    legacy.pop("eventTime")
    if not kwargs.get("token"):  # token-less → a fresh random UUID on each side, equal modulo value
        assert uuid.UUID(new["run"].pop("runId")) != uuid.UUID(legacy["run"].pop("runId"))
    assert new == legacy


def test_lineage_kit_run_id_namespace_is_aligned() -> None:
    """Migration note #1: lineage-kit's uuid5 namespace equals the lance-ns one, so every deterministic
    run id already in the graph keeps MERGEing after emitters swap onto lineage-kit."""
    for seed in ("lance_ray_ingest-tok1", "acme-embed_features-tok4", "iiif-ingest-x"):
        assert lineage_kit.run_id_for(seed) == service_kit_ol.run_id_for(seed)


def test_the_model_facet_carries_the_runs_own_provenance() -> None:
    """#88 step 6: models + build sha on the RUN, from the artefact — and only when present."""
    from medallion.schemas.events import build_run_event

    event = build_run_event(
        operation="transcribe_pages",
        author="htr",
        job_namespace="medallion",
        inputs=[("bronze", "bronze$pages")],
        output_namespace="gold-htr",
        output_name="gold$htr",
        token="t1",
        models=["org/m@v1", "org/n@v2"],
        commit_sha="cafe01",
    )

    facet = event["run"]["facets"]["model"]
    assert facet["models"] == ["org/m@v1", "org/n@v2"]
    assert facet["commit"] == "cafe01"
    assert facet["_producer"]  # a spec-legal custom facet, not a bare dict


def test_no_models_renders_no_model_facet() -> None:
    """The byte-parity guarantee: every existing lane passes nothing and must emit EXACTLY what it
    emitted before this kwarg existed — and an absent sha is OMITTED, never nulled."""
    from medallion.schemas.events import build_run_event

    kwargs: dict = {
        "operation": "embed_features",
        "author": "data_eng",
        "job_namespace": "medallion",
        "inputs": [("bronze", "bronze$events")],
        "output_namespace": "silver",
        "output_name": "silver$features",
        "token": "t1",
        "event_time": "2026-08-04T20:00:00+00:00",  # pinned — two now() calls would differ by microseconds
    }
    before = build_run_event(**kwargs)
    with_kwargs = build_run_event(**kwargs, models=None, commit_sha=None)

    assert "model" not in before["run"]["facets"]
    assert before == with_kwargs

    sha_only = build_run_event(**kwargs, models=["org/m@v1"], commit_sha=None)
    assert "commit" not in sha_only["run"]["facets"]["model"]
