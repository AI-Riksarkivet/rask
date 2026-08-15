"""The OpenLineage spec-fidelity gate every emitter has to pass.

Our services build ``RunEvent`` wire dicts by hand — ``openlineage-python`` is a **dev-only**
dependency, deliberately kept out of the service images, so only ``lineage.seed`` (the demo producer)
constructs events through the official ``event_v2`` / ``facet_v2`` classes. That trade is only safe with
a gate, and until 2026-07-26 there was none: validating the LIVE ``/events`` feed against
``https://openlineage.io/spec/2-0-2/OpenLineage.json`` failed 15 of 200 events, and three facet
``_schemaURL``s had silently drifted a version behind the client.

So this module is the two halves of that gate:

* **Structure** — every builder's output is validated against the vendored official core schema
  (``tests/data/openlineage-2-0-2.json``, a byte copy of the published 2-0-2 document our
  ``RUN_EVENT_SCHEMA_URL`` names). Vendored so the gate is offline and pinned: it tests the version we
  claim to emit, not whatever openlineage.io serves today.
* **Facet versions** — every ``_schemaURL`` constant is asserted EQUAL to the corresponding official
  ``facet_v2`` class's ``_get_schema()``. Bumping ``openlineage-python`` now reddens CI the moment a
  facet's published version moves, which is the only thing that made the drift invisible before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from catalog.core import lineage_emit as catalog_emit
from jsonschema import Draft202012Validator, FormatChecker
from lineage.models import Dataset, RunEvent
from maintenance.core import lineage_emit as compaction_emit
from medallion.schemas import events as medallion_events
from openlineage.client import facet_v2

from service_kit import openlineage as ol
from service_kit.lancekit import openlineage as lancekit_ol


_SPEC_PATH = Path(__file__).resolve().parents[1] / "data" / "openlineage-2-0-2.json"
_SPEC: dict[str, Any] = json.loads(_SPEC_PATH.read_text())

#: A UUID-shaped run id — the spec's ``Run.runId`` carries ``format: uuid`` and Marquez rejects anything
#: else, so every builder that takes an injected id must be fed (and asserted on) a real UUID.
_RUN_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_EVENT_TIME = "2026-07-26T09:00:00+00:00"


def _errors_against(definition: str, instance: object) -> list[str]:
    """Validation errors for ``instance`` against ``#/$defs/<definition>`` of the vendored core spec.

    ``format_checker`` is what makes ``runId``'s ``format: uuid`` and ``producer``'s ``format: uri``
    actually assert — jsonschema treats formats as annotations otherwise, which is precisely how a
    ``runId`` of ``"promote-1260245"`` passed unnoticed in the live feed.
    """
    root = {"$ref": f"#/$defs/{definition}", "$defs": _SPEC["$defs"], "$id": _SPEC["$id"]}
    validator = Draft202012Validator(root, format_checker=FormatChecker())
    return [f"{e.json_path}: {e.message}" for e in sorted(validator.iter_errors(instance), key=lambda e: e.json_path)]


def _assert_conforms(event: dict[str, Any]) -> None:
    errors = _errors_against("RunEvent", event)
    assert not errors, "\n".join(errors)


def test_vendored_spec_is_the_version_we_claim_to_emit() -> None:
    # ASSERTS the gate can't quietly drift from the constant: the vendored document's $id must be the
    # base of RUN_EVENT_SCHEMA_URL, so bumping one without the other fails here rather than shipping
    # events that name a spec version we never validated against.
    assert f"{_SPEC['$id']}#/$defs/RunEvent" == ol.RUN_EVENT_SCHEMA_URL


@pytest.mark.parametrize(
    ("constant", "official"),
    [
        (ol.SCHEMA_FACET_SCHEMA_URL, facet_v2.schema_dataset.SchemaDatasetFacet),
        (ol.VERSION_FACET_SCHEMA_URL, facet_v2.dataset_version_dataset.DatasetVersionDatasetFacet),
        (ol.DATASOURCE_FACET_SCHEMA_URL, facet_v2.datasource_dataset.DatasourceDatasetFacet),
        (ol.ERROR_MESSAGE_FACET_SCHEMA_URL, facet_v2.error_message_run.ErrorMessageRunFacet),
        (ol.COLUMN_LINEAGE_FACET_SCHEMA_URL, facet_v2.column_lineage_dataset.ColumnLineageDatasetFacet),
        (ol.BASE_FACET_SCHEMA_URL, facet_v2.BaseFacet),
    ],
)
def test_facet_schema_url_matches_the_official_client(constant: str, official: Any) -> None:
    # ASSERTS the version-drift guard. Each constant must equal what openlineage-python's own class
    # stamps; when the client is bumped and a facet's published version moves, this reddens instead of
    # letting us emit a stale _schemaURL. It caught SchemaDatasetFacet at 1-1-1 (client: 1-2-0) and
    # DatasourceDatasetFacet + ErrorMessageRunFacet at 1-0-0 (client: 1-0-1) — the 1-0-0 documents
    # $ref the retired 1-0-2 core spec while our envelope declares 2-0-2.
    assert constant == official._get_schema()


@pytest.mark.parametrize(
    ("constant", "official"),
    [
        (lancekit_ol.SCHEMA_URL, ol.RUN_EVENT_SCHEMA_URL),
        (lancekit_ol._SCHEMA_FACET_URL, ol.SCHEMA_FACET_SCHEMA_URL),
        (lancekit_ol._DATASOURCE_FACET_URL, ol.DATASOURCE_FACET_SCHEMA_URL),
        (lancekit_ol._COLUMN_LINEAGE_FACET_URL, ol.COLUMN_LINEAGE_FACET_SCHEMA_URL),
        (lancekit_ol._ERROR_MESSAGE_FACET_URL, ol.ERROR_MESSAGE_FACET_SCHEMA_URL),
    ],
)
def test_lancekit_mirror_shares_the_shared_constants(constant: str, official: str) -> None:
    # ASSERTS the lance-media (ratch/annotation) emit path emits the SAME spec versions as the lance-ns
    # emitters. It used to hand-copy them and had already drifted in both directions.
    assert constant == official


def test_catalog_write_event_conforms() -> None:
    event = catalog_emit.build_write_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        author="alice",
        version=3,
        operation=catalog_emit.CREATE_TABLE,
        run_id=_RUN_ID,
        event_time=_EVENT_TIME,
        job_namespace="lance-catalog",
        source_uri="s3://lakehouse/bronze/images",
        schema_fields=[{"name": "id", "type": "int64"}, {"name": "payload", "type": "blob"}],
        inputs=[catalog_emit.InputRef("alpha$raw", "alpha$raw$drop", 7)],
        extra_run_facets=catalog_emit.shape_run_facets({"params": {"lr": 0.01}}),
    )
    _assert_conforms(event)


@pytest.mark.parametrize("operation", [catalog_emit.DROP_TABLE, catalog_emit.DEREGISTER_TABLE])
def test_catalog_versionless_write_event_conforms(operation: str) -> None:
    # ASSERTS the versionless branch (drop/deregister omit the version facet) is still a valid RunEvent —
    # an omitted optional facet must not take the required envelope with it.
    event = catalog_emit.build_write_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        author=None,
        version=None,
        operation=operation,
        run_id=_RUN_ID,
        event_time=_EVENT_TIME,
        job_namespace="lance-catalog",
    )
    _assert_conforms(event)


def test_medallion_run_event_conforms() -> None:
    event = medallion_events.build_run_event(
        operation="promote_bronze_to_silver",
        author="alice",
        job_namespace="medallion-producer",
        inputs=[("alpha$bronze", "alpha$bronze$images")],
        output_namespace="alpha$silver",
        output_name="alpha$silver$features",
        version=2,
        row_count=64,
        size_bytes=4096,
        assertions=[{"assertion": "row_count", "success": True}],
        source_uri="s3://lakehouse/silver/features",
        schema_fields=[{"name": "id", "type": "int64"}, {"name": "embedding", "type": "array<float>"}],
        column_map=[("embedding", "payload", "TRANSFORMATION"), ("id", "id", "IDENTITY")],
        token="tok1",
        project="acme",
    )
    _assert_conforms(event)


def test_medallion_fail_event_conforms() -> None:
    event = medallion_events.build_run_event(
        operation="promote_bronze_to_silver",
        author="alice",
        job_namespace="medallion-producer",
        inputs=[("alpha$bronze", "alpha$bronze$images")],
        output_namespace="alpha$silver",
        output_name="alpha$silver$features",
        token="tok1",
        event_type="FAIL",
        error_message="CUDA OOM while embedding batch 7/12",
    )
    _assert_conforms(event)
    assert event["eventType"] in _SPEC["$defs"]["RunEvent"]["allOf"][1]["properties"]["eventType"]["enum"]


def test_compaction_maintenance_events_conform() -> None:
    ok = compaction_emit.build_maintenance_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        job_namespace="lance-compaction",
        run_id=_RUN_ID,
        event_time=_EVENT_TIME,
    )
    _assert_conforms(ok)
    failed = compaction_emit.build_maintenance_fail_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        job_namespace="lance-compaction",
        run_id=_RUN_ID,
        event_time=_EVENT_TIME,
        error="commit conflict, retry limit exceeded",
    )
    _assert_conforms(failed)


def test_lancekit_annotation_event_conforms() -> None:
    result = lancekit_ol.WriteResult(
        version=4,
        row_count=12,
        size_bytes=0,
        fields=[{"name": "id", "type": "int64"}, {"name": "labels", "type": "json"}],
        column_map=[("id", "id", "IDENTITY"), ("labels", "labels", "IDENTITY")],
    )
    event = lancekit_ol.build_run_event(
        operation="MERGE_INSERT",
        job_namespace="media",
        job_name="annotate.merge_insert",
        inputs=[("media", "unit-7")],
        output_namespace="media",
        output_name="annotations",
        event_time=_EVENT_TIME,
        result=result,
        source_uri="s3://media/annotations",
        seed="annotate-annotations-unit-7-v4",
    )
    _assert_conforms(event)


def test_lancekit_column_lineage_uses_the_modern_transformations_array() -> None:
    # ASSERTS the ingest can actually READ the mirror's column edges. The first cut put
    # transformationType/transformationSubtype on the InputField — valid JSON (InputField allows
    # additional properties) but a slot no consumer reads, so every edge arrived with an empty type
    # and subtype. Drive the real parser, don't eyeball the dict.
    result = lancekit_ol.WriteResult(
        version=1,
        row_count=1,
        size_bytes=0,
        fields=[{"name": "labels", "type": "json"}],
        column_map=[("labels", "raw_labels", "TRANSFORMATION")],
    )
    event = lancekit_ol.build_run_event(
        operation="MERGE_INSERT",
        job_namespace="media",
        job_name="annotate.merge_insert",
        inputs=[("media", "unit-7")],
        output_namespace="media",
        output_name="annotations",
        event_time=_EVENT_TIME,
        result=result,
    )
    edges = Dataset.model_validate(event["outputs"][0]).column_edges
    assert edges == [
        {
            "out_field": "labels",
            "namespace": "media",
            "name": "unit-7",
            "field": "raw_labels",
            "type": "DIRECT",
            "subtype": "TRANSFORMATION",
            "description": "",
            "masking": False,
        }
    ]


def test_lancekit_run_id_matches_the_shared_derivation() -> None:
    # ASSERTS the two sides mint the SAME uuid5 for one seed — the whole point of sharing the namespace
    # is that a redelivery MERGEs onto one (:Run) instead of forking the graph.
    assert lancekit_ol.run_id_for("promote-tok1") == ol.run_id_for("promote-tok1")


def test_custom_facets_carry_the_base_facet_contract() -> None:
    # ASSERTS BaseFacet's two required fields on our CUSTOM facets. Every standard facet gets these from
    # its own builder; the custom ones (lance, author, progress, params) only have custom_facet, and a
    # facet built as a bare dict — as the reconcile back-fill's did — is a spec violation a consumer
    # rejects.
    facet = ol.custom_facet("https://example.com/producer", operation="reconcile", version=4)
    assert facet["_producer"] == "https://example.com/producer"
    assert facet["_schemaURL"] == ol.BASE_FACET_SCHEMA_URL
    assert not _errors_against("RunFacet", facet)


def test_reconcile_backfill_event_conforms() -> None:
    # ASSERTS the synthetic back-fill event the lineage service writes into its OWN /events feed is a
    # real RunEvent. It was the single largest source of live invalidity (14 of 200 events): eventType
    # "RECONCILED" is not in the spec enum, and its `lance` facet carried no _producer/_schemaURL. The
    # builder is inlined in repository.backfill_write (it needs the transaction), so mirror its shape.
    from lineage.services.repository import _RECONCILE_PRODUCER

    event = {
        "eventType": "OTHER",
        "eventTime": _EVENT_TIME,
        "producer": _RECONCILE_PRODUCER,
        "schemaURL": ol.RUN_EVENT_SCHEMA_URL,
        "run": {
            "runId": ol.run_id_for("reconcile-alpha$bronze$images-v4"),
            "facets": {"lance": ol.custom_facet(_RECONCILE_PRODUCER, operation="reconcile", version=4)},
        },
        "job": {"namespace": "lance-reconcile", "name": "reconcile.alpha$bronze$images"},
        "inputs": [],
        "outputs": [{"namespace": "", "name": "alpha$bronze$images"}],
    }
    _assert_conforms(event)


def test_ingest_reads_facets_from_the_spec_typed_slots() -> None:
    # ASSERTS Postel on the read side. OutputStatisticsOutputDatasetFacet is an OutputDatasetFacet and
    # DataQualityAssertionsDatasetFacet is an InputDatasetFacet, so the official client — and therefore
    # every Spark/Airflow/dbt/Marquez producer, and our own lineage.seed — serialises them into
    # outputFacets/inputFacets, NOT into facets. Reading only `facets` dropped them silently.
    output = Dataset.model_validate(
        {
            "namespace": "silver",
            "name": "features",
            "facets": {},
            "outputFacets": {
                "outputStatistics": {
                    "_producer": "https://example.com/p",
                    "_schemaURL": facet_v2.output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet._get_schema(),
                    "rowCount": 64,
                    "size": 4096,
                }
            },
            "inputFacets": {
                "dataQualityAssertions": {
                    "_producer": "https://example.com/p",
                    "_schemaURL": facet_v2.data_quality_assertions_dataset.DataQualityAssertionsDatasetFacet._get_schema(),
                    "assertions": [{"assertion": "row_count", "success": False}],
                }
            },
        }
    )
    assert output.statistics == (64, 4096)
    assert output.quality_assertions == [{"assertion": "row_count", "success": False}]


def test_typed_slots_survive_the_feed_round_trip() -> None:
    # ASSERTS the durable /events blob (model_dump(by_alias=True)) keeps a producer's typed slots exactly
    # as sent — inputFacets/outputFacets are read off model_extra rather than declared as fields, so we
    # neither drop them nor stamp an empty outputFacets onto an input dataset (a key InputDataset lacks).
    wire = {
        "eventType": "COMPLETE",
        "eventTime": _EVENT_TIME,
        "producer": "https://example.com/p",
        "schemaURL": ol.RUN_EVENT_SCHEMA_URL,
        "run": {"runId": _RUN_ID, "facets": {}},
        "job": {"namespace": "ray-jobs", "name": "embed"},
        "inputs": [{"namespace": "bronze", "name": "events", "facets": {}}],
        "outputs": [
            {
                "namespace": "silver",
                "name": "features",
                "facets": {},
                "outputFacets": {
                    "outputStatistics": {
                        "_producer": "https://example.com/p",
                        "_schemaURL": ol.BASE_FACET_SCHEMA_URL,
                        "rowCount": 9,
                    }
                },
            }
        ],
    }
    dumped = RunEvent.model_validate(wire).model_dump(by_alias=True)
    assert dumped["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 9
    assert "outputFacets" not in dumped["inputs"][0]
    assert dumped["schemaURL"] == ol.RUN_EVENT_SCHEMA_URL
    _assert_conforms(dumped)
