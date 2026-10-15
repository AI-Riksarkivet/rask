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
from jsonschema import Draft202012Validator, FormatChecker
from openlineage.client import facet_v2

from catalog.core import lineage_emit as catalog_emit
from catalog.core.run_context import RunContext
from lineage.models import ColumnUpstream, Dataset, RunEvent
from maintenance.core import lineage_emit as compaction_emit
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
    """Validate against the definition this event's SHAPE claims to be.

    A DDL change is a `DatasetEvent` ([[LIN-004]]) and the spec's definition for one carries
    `"not": {"required": ["job", "run"]}` — so checking it against the right `$defs` entry is not a
    softer test, it is a stricter one: the schema itself refuses the members the phantom was made of.
    """
    definition = "DatasetEvent" if "dataset" in event else "RunEvent"
    errors = _errors_against(definition, event)
    assert not errors, f"as {definition}:\n" + "\n".join(errors)


@pytest.mark.parametrize(
    ("constant", "official"),
    [
        (ol.SCHEMA_FACET_SCHEMA_URL, facet_v2.schema_dataset.SchemaDatasetFacet),
    ],
)
def test_facet_schema_url_matches_the_official_client(constant: str, official: Any) -> None:
    # ASSERTS the version-drift guard. Each constant must equal what openlineage-python's own class
    # stamps; when the client is bumped and a facet's published version moves, this reddens instead of
    # letting us emit a stale _schemaURL. It caught SchemaDatasetFacet at 1-1-1 (client: 1-2-0) and
    # DatasourceDatasetFacet + ErrorMessageRunFacet at 1-0-0 (client: 1-0-1) — the 1-0-0 documents
    # $ref the retired 1-0-2 core spec while our envelope declares 2-0-2.
    assert constant == official._get_schema()


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
        inputs=[catalog_emit.InputRef("alpha$raw", "alpha$raw$drop", 7, "review")],
        run=RunContext(run_id="nightly-7", job="ingest"),
    )
    _assert_conforms(event)


@pytest.mark.parametrize("operation", [catalog_emit.DROP_TABLE])
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
    # ASSERTS the slot a graph backend reads the kind from. The first cut put transformationType/
    # transformationSubtype on the InputField — valid JSON (InputField allows additional properties) but a slot
    # no consumer reads, so every forwarded edge arrived with an empty type and subtype. And lineage's own
    # authorizer must find the column's source dataset in the same facet, or it authorizes nothing.
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
    [source] = event["outputs"][0]["facets"]["columnLineage"]["fields"]["labels"]["inputFields"]
    assert [(t["type"], t["subtype"]) for t in source["transformations"]] == [("DIRECT", "TRANSFORMATION")]
    assert Dataset.model_validate(event["outputs"][0]).column_upstreams == [ColumnUpstream(namespace="media", name="unit-7")]


def test_typed_slots_survive_the_feed_round_trip() -> None:
    # ASSERTS the feed's stored payload (model_dump(by_alias=True)) keeps a producer's typed slots exactly
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
