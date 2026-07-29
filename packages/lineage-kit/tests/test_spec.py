"""Spec fidelity: our constants and wire shape against ``openlineage-python``'s own models.

The same gate pattern as ``tests/unit/test_openlineage_spec_conformance.py``: every facet
``_schemaURL`` constant is asserted EQUAL to the installed client's ``facet_v2`` class, so
bumping the client reddens this suite the moment a published facet version moves. The wire
form itself is produced BY the official serializer, and the round-trip test proves our
pydantic models re-validate it losslessly.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from lineage_kit import (
    BASE_FACET_SCHEMA_URL,
    DATASET_VERSION_FACET_SCHEMA_URL,
    DATASOURCE_FACET_SCHEMA_URL,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    OUTPUT_STATISTICS_FACET_SCHEMA_URL,
    PARENT_RUN_FACET_SCHEMA_URL,
    PRODUCER,
    RUN_EVENT_SCHEMA_URL,
    SCHEMA_FACET_SCHEMA_URL,
    Dataset,
    DatasetVersionFacet,
    DatasourceFacet,
    ErrorMessageRunFacet,
    Job,
    OutputDataset,
    OutputStatisticsFacet,
    Run,
    RunEvent,
    RunState,
    SchemaDatasetFacet,
    SchemaField,
    custom_facet,
    run_id_for,
)
from lineage_kit.context import LineageContext
from lineage_kit.schemas import DatasetFacets, OutputDatasetFacets, RunFacets
from openlineage.client.facet_v2 import (
    BaseFacet,
    dataset_version_dataset,
    datasource_dataset,
    error_message_run,
    output_statistics_output_dataset,
    parent_run,
    schema_dataset,
)


_JOB_RUN_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_STAGE_RUN_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3302"
_EVENT_TIME = "2026-07-27T09:00:00+00:00"


def _full_event() -> RunEvent:
    """An event exercising every first-class facet plus a custom passthrough facet."""
    parent = LineageContext.root(namespace="rask", job_name="ingest", run_id=_JOB_RUN_ID)
    facets = RunFacets(
        parent=parent.parent_facet(),
        error_message=ErrorMessageRunFacet(message="ValueError: boom"),
        rask=custom_facet(chunk_id="c-17"),  # extra="allow" carries custom facets through
    )
    return RunEvent(
        event_type=RunState.FAIL,
        event_time=_EVENT_TIME,
        run=Run(run_id=_STAGE_RUN_ID, facets=facets),
        job=Job(namespace="rask", name="ingest.layout"),
        inputs=[Dataset(namespace="lance", name="bronze.pages")],
        outputs=[
            OutputDataset(
                namespace="lance",
                name="silver.lines",
                facets=DatasetFacets(
                    schema_=SchemaDatasetFacet(fields=[SchemaField(name="text", type_="string")]),
                    data_source=DatasourceFacet(name="silver.lines", uri="s3://lakehouse/silver/lines.lance"),
                    version=DatasetVersionFacet(dataset_version="7"),
                ),
                output_facets=OutputDatasetFacets(output_statistics=OutputStatisticsFacet(row_count=42, size=1024)),
            )
        ],
    )


@pytest.mark.parametrize(
    ("constant", "official"),
    [
        (PARENT_RUN_FACET_SCHEMA_URL, parent_run.ParentRunFacet),
        (ERROR_MESSAGE_FACET_SCHEMA_URL, error_message_run.ErrorMessageRunFacet),
        (SCHEMA_FACET_SCHEMA_URL, schema_dataset.SchemaDatasetFacet),
        (OUTPUT_STATISTICS_FACET_SCHEMA_URL, output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet),
        (DATASOURCE_FACET_SCHEMA_URL, datasource_dataset.DatasourceDatasetFacet),
        (DATASET_VERSION_FACET_SCHEMA_URL, dataset_version_dataset.DatasetVersionDatasetFacet),
        (BASE_FACET_SCHEMA_URL, BaseFacet),
    ],
)
def test_facet_schema_urls_match_the_installed_client(constant: str, official: Any) -> None:
    assert constant == official._get_schema()


def test_wire_envelope_is_spec_shaped() -> None:
    wire = _full_event().to_wire()
    # The schemaURL is stamped by the OFFICIAL model — asserting it equals our constant
    # proves the envelope claims exactly the spec version we advertise.
    assert wire["schemaURL"] == RUN_EVENT_SCHEMA_URL
    assert wire["producer"] == PRODUCER
    assert wire["eventType"] == "FAIL"
    assert wire["eventTime"] == _EVENT_TIME
    assert uuid.UUID(wire["run"]["runId"])  # spec: runId must be a UUID


def test_wire_facets_carry_producer_and_schema_url() -> None:
    wire = _full_event().to_wire()
    run_facets = wire["run"]["facets"]
    out_facets = wire["outputs"][0]["facets"]
    for facet, url in [
        (run_facets["parent"], PARENT_RUN_FACET_SCHEMA_URL),
        (run_facets["errorMessage"], ERROR_MESSAGE_FACET_SCHEMA_URL),
        (run_facets["rask"], BASE_FACET_SCHEMA_URL),
        (out_facets["schema"], SCHEMA_FACET_SCHEMA_URL),
        (out_facets["dataSource"], DATASOURCE_FACET_SCHEMA_URL),
        (out_facets["version"], DATASET_VERSION_FACET_SCHEMA_URL),
        (wire["outputs"][0]["outputFacets"]["outputStatistics"], OUTPUT_STATISTICS_FACET_SCHEMA_URL),
    ]:
        assert facet["_producer"] == PRODUCER
        assert facet["_schemaURL"] == url


def test_wire_parent_facet_spells_the_official_keys() -> None:
    parent = _full_event().to_wire()["run"]["facets"]["parent"]
    assert parent["run"]["runId"] == _JOB_RUN_ID
    assert parent["job"] == {"namespace": "rask", "name": "ingest", "facets": {}}
    assert parent["root"]["run"]["runId"] == _JOB_RUN_ID
    assert parent["root"]["job"]["name"] == "ingest"


def test_round_trip_through_the_official_serializer() -> None:
    # pydantic → official event_v2 models → official Serde wire dict → pydantic again:
    # every field must survive, so THE standard emission shape and the official client
    # can never silently disagree about a key spelling or a facet placement.
    event = _full_event()
    back = RunEvent.model_validate(event.to_wire())
    assert back == event


def test_output_statistics_values_survive_to_wire() -> None:
    stats = _full_event().to_wire()["outputs"][0]["outputFacets"]["outputStatistics"]
    assert stats["rowCount"] == 42
    assert stats["size"] == 1024


def test_job_facets_survive_to_wire_and_back() -> None:
    # The lineage consumer reads job facets today (sourceCodeLocation, ownership) —
    # the shape must carry them verbatim, never silently drop them.
    source = custom_facet(type="git", url="https://github.com/Borg93/rask", path="services/medallion")
    event = RunEvent(
        event_type=RunState.COMPLETE,
        event_time=_EVENT_TIME,
        run=Run(run_id=_STAGE_RUN_ID),
        job=Job(namespace="rask", name="promote", facets={"sourceCodeLocation": source}),
    )
    wire = event.to_wire()
    assert wire["job"]["facets"]["sourceCodeLocation"] == source
    assert RunEvent.model_validate(wire).job.facets["sourceCodeLocation"] == source


def test_run_id_for_is_a_deterministic_uuid() -> None:
    a, b = run_id_for("ingest-silver.lines-7"), run_id_for("ingest-silver.lines-7")
    assert a == b
    assert uuid.UUID(a)
    assert run_id_for("another-seed") != a
