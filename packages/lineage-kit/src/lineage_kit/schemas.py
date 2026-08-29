"""Pydantic OpenLineage schemas — THE standard emission shape for all Ray compute (R21).

Every plane that runs on Ray (the medallion movers, the ingest→bronze producer, the HTR
pipeline, ``ray_stage``/lance jobs, and online Ray Serve deployments) authors its lineage
through these models instead of hand-building wire dicts. The models convert to the
official ``openlineage-python`` ``event_v2`` classes (:meth:`RunEvent.to_openlineage`),
and the wire form (:meth:`RunEvent.to_wire`) is produced BY the official serializer —
spec fidelity is by construction, not by mirroring.

The facet ``_schemaURL`` constants below are asserted EQUAL to the installed client's
``facet_v2`` classes by this package's tests (the same gate pattern as
``tests/unit/test_openlineage_spec_conformance.py``), so bumping ``openlineage-python``
reddens CI the moment a published facet version moves.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from openlineage.client import event_v2
from openlineage.client.facet_v2 import (
    dataset_version_dataset,
    datasource_dataset,
    error_message_run,
    output_statistics_output_dataset,
    parent_run,
    schema_dataset,
)
from openlineage.client.serde import Serde
from pydantic import BaseModel, ConfigDict, Field


#: This library, as every emitted event's ``producer`` / facet ``_producer``.
PRODUCER = "https://github.com/Borg93/rask/tree/main/packages/lineage-kit"

#: The top-level ``schemaURL`` every OpenLineage ``RunEvent`` must carry.
RUN_EVENT_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"

#: ``BaseFacet`` — the spec-legal ``_schemaURL`` for custom facets with no published schema.
BASE_FACET_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/BaseFacet"

#: Standard facet schema URLs — each asserted against the official client by the tests.
PARENT_RUN_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-2-0/ParentRunFacet.json#/$defs/ParentRunFacet"
ERROR_MESSAGE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet"
SCHEMA_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
OUTPUT_STATISTICS_FACET_SCHEMA_URL = (
    "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json#/$defs/OutputStatisticsOutputDatasetFacet"
)
DATASOURCE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/DatasourceDatasetFacet.json#/$defs/DatasourceDatasetFacet"
DATASET_VERSION_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json#/$defs/DatasetVersionDatasetFacet"


class RunState(StrEnum):
    """The OpenLineage run states. ``COMPLETE``/``FAIL``/``ABORT`` are terminal."""

    START = "START"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    ABORT = "ABORT"
    FAIL = "FAIL"
    OTHER = "OTHER"


#: States after which a run must not emit again (enforced by ``LineageRun``).
TERMINAL_STATES = frozenset({RunState.COMPLETE, RunState.ABORT, RunState.FAIL})


class WireModel(BaseModel):
    """Base: alias-tolerant (wire keys OR pythonic names), ignores unknown wire keys.

    PUBLIC, because it is inherited across a module boundary: :mod:`lineage_kit.consume`'s exported
    models are built on it, and an adopter authoring its own consume-side shape needs the same wire
    tolerance. It was ``WireModel``, whose underscore promised a privacy the package did not keep.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Facet(WireModel):
    """Base facet — every facet, standard or custom, carries ``_producer`` + ``_schemaURL``."""

    producer: str = Field(default=PRODUCER, alias="_producer")
    schema_url: str = Field(default=BASE_FACET_SCHEMA_URL, alias="_schemaURL")


def custom_facet(**fields: Any) -> dict[str, Any]:  # noqa: ANN401 — facet payloads are arbitrary JSON by design
    """A spec-legal custom facet payload (points at ``BaseFacet``, stamps ``_producer``)."""
    return {"_producer": PRODUCER, "_schemaURL": BASE_FACET_SCHEMA_URL, **fields}


class RunRef(WireModel):
    """A reference to a run by id (the parent facet's ``run`` / ``root.run``)."""

    run_id: str = Field(alias="runId")


class JobRef(WireModel):
    """A reference to a job by namespace + name."""

    namespace: str
    name: str


class RootRef(WireModel):
    """The topmost run of a hierarchy (``ParentRunFacet.root``)."""

    run: RunRef
    job: JobRef


class ParentRunFacet(Facet):
    """``parent`` run facet — what links stage runs to their job run and actor runs to their stage."""

    schema_url: str = Field(default=PARENT_RUN_FACET_SCHEMA_URL, alias="_schemaURL")
    run: RunRef
    job: JobRef
    root: RootRef | None = None

    def to_openlineage(self) -> parent_run.ParentRunFacet:
        root = (
            parent_run.Root(
                run=parent_run.RootRun(runId=self.root.run.run_id),
                job=parent_run.RootJob(namespace=self.root.job.namespace, name=self.root.job.name),
            )
            if self.root
            else None
        )
        return parent_run.ParentRunFacet(
            run=parent_run.Run(runId=self.run.run_id),
            job=parent_run.Job(namespace=self.job.namespace, name=self.job.name),
            root=root,
            producer=self.producer,  # ty: ignore[unknown-argument] — attrs init-alias for `_producer`, real at runtime
        )


class ErrorMessageRunFacet(Facet):
    """``errorMessage`` run facet — the FAIL/ABORT emitters' error payload."""

    schema_url: str = Field(default=ERROR_MESSAGE_FACET_SCHEMA_URL, alias="_schemaURL")
    message: str
    programming_language: str = Field(default="PYTHON", alias="programmingLanguage")
    stack_trace: str | None = Field(default=None, alias="stackTrace")

    def to_openlineage(self) -> error_message_run.ErrorMessageRunFacet:
        return error_message_run.ErrorMessageRunFacet(
            message=self.message,
            programmingLanguage=self.programming_language,
            stackTrace=self.stack_trace,
            producer=self.producer,  # ty: ignore[unknown-argument] — attrs init-alias for `_producer`, real at runtime
        )


class SchemaField(WireModel):
    """One column of a ``SchemaDatasetFacet`` (name + concise type label)."""

    name: str
    type_: str = Field(alias="type")


class SchemaDatasetFacet(Facet):
    """``schema`` dataset facet — the produced dataset's columns."""

    schema_url: str = Field(default=SCHEMA_FACET_SCHEMA_URL, alias="_schemaURL")
    fields: list[SchemaField] = Field(default_factory=list)

    def to_openlineage(self) -> schema_dataset.SchemaDatasetFacet:
        return schema_dataset.SchemaDatasetFacet(
            fields=[schema_dataset.SchemaDatasetFacetFields(name=f.name, type=f.type_) for f in self.fields],
            producer=self.producer,  # ty: ignore[unknown-argument] — attrs init-alias for `_producer`, real at runtime
        )


class OutputStatisticsFacet(Facet):
    """``outputStatistics`` output facet — measured row count + on-disk size of a write."""

    schema_url: str = Field(default=OUTPUT_STATISTICS_FACET_SCHEMA_URL, alias="_schemaURL")
    row_count: int = Field(alias="rowCount")
    size: int | None = None

    def to_openlineage(self) -> output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet:
        return output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(
            rowCount=self.row_count,
            size=self.size,
            producer=self.producer,  # ty: ignore[unknown-argument] — attrs init-alias for `_producer`, real at runtime
        )


class DatasourceFacet(Facet):
    """``dataSource`` dataset facet — the physical storage URI of the dataset."""

    schema_url: str = Field(default=DATASOURCE_FACET_SCHEMA_URL, alias="_schemaURL")
    name: str
    uri: str

    def to_openlineage(self) -> datasource_dataset.DatasourceDatasetFacet:
        return datasource_dataset.DatasourceDatasetFacet(name=self.name, uri=self.uri, producer=self.producer)  # ty: ignore[unknown-argument]


class DatasetVersionFacet(Facet):
    """``version`` dataset facet — the Lance dataset version stamped on inputs/outputs."""

    schema_url: str = Field(default=DATASET_VERSION_FACET_SCHEMA_URL, alias="_schemaURL")
    dataset_version: str = Field(alias="datasetVersion")

    def to_openlineage(self) -> dataset_version_dataset.DatasetVersionDatasetFacet:
        return dataset_version_dataset.DatasetVersionDatasetFacet(datasetVersion=self.dataset_version, producer=self.producer)  # ty: ignore[unknown-argument]


class RunFacets(WireModel):
    """The run facet bag. Unknown keys are kept and passed through to the wire verbatim
    (author them with :func:`custom_facet` so they stay spec-legal)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    parent: ParentRunFacet | None = None
    error_message: ErrorMessageRunFacet | None = Field(default=None, alias="errorMessage")

    def to_openlineage(self) -> dict[str, Any]:
        facets: dict[str, Any] = {}
        if self.parent is not None:
            facets["parent"] = self.parent.to_openlineage()
        if self.error_message is not None:
            facets["errorMessage"] = self.error_message.to_openlineage()
        facets.update(self.model_extra or {})
        return facets


class DatasetFacets(WireModel):
    """The dataset facet bag (inputs and outputs); unknown keys pass through."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    schema_: SchemaDatasetFacet | None = Field(default=None, alias="schema")
    data_source: DatasourceFacet | None = Field(default=None, alias="dataSource")
    version: DatasetVersionFacet | None = None

    def to_openlineage(self) -> dict[str, Any]:
        facets: dict[str, Any] = {}
        if self.schema_ is not None:
            facets["schema"] = self.schema_.to_openlineage()
        if self.data_source is not None:
            facets["dataSource"] = self.data_source.to_openlineage()
        if self.version is not None:
            facets["version"] = self.version.to_openlineage()
        facets.update(self.model_extra or {})
        return facets


class OutputDatasetFacets(WireModel):
    """The output-only facet bag (``outputFacets``); unknown keys pass through."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    output_statistics: OutputStatisticsFacet | None = Field(default=None, alias="outputStatistics")

    def to_openlineage(self) -> dict[str, Any]:
        facets: dict[str, Any] = {}
        if self.output_statistics is not None:
            facets["outputStatistics"] = self.output_statistics.to_openlineage()
        facets.update(self.model_extra or {})
        return facets


class Dataset(WireModel):
    """An input dataset (``{namespace, name, facets}``)."""

    namespace: str
    name: str
    facets: DatasetFacets = Field(default_factory=DatasetFacets)

    def to_openlineage_input(self) -> event_v2.InputDataset:
        return event_v2.InputDataset(namespace=self.namespace, name=self.name, facets=self.facets.to_openlineage())


class OutputDataset(Dataset):
    """An output dataset — adds the output-only ``outputFacets``."""

    output_facets: OutputDatasetFacets = Field(default_factory=OutputDatasetFacets, alias="outputFacets")

    def to_openlineage_output(self) -> event_v2.OutputDataset:
        return event_v2.OutputDataset(
            namespace=self.namespace,
            name=self.name,
            facets=self.facets.to_openlineage(),
            outputFacets=self.output_facets.to_openlineage(),
        )


class Run(WireModel):
    """The event's run — a spec-valid UUID ``runId`` plus facets."""

    run_id: str = Field(alias="runId")
    facets: RunFacets = Field(default_factory=RunFacets)


class Job(WireModel):
    """The event's job — ``namespace`` + dotted hierarchical ``name`` (``job.stage.actor``).

    ``facets`` is a raw pass-through bag (``sourceCodeLocation``, ``ownership``, …) — the
    lineage consumer reads these today, so the shape must carry them verbatim; author
    entries with :func:`custom_facet` (or a standard facet's own ``_schemaURL``)."""

    namespace: str
    name: str
    facets: dict[str, Any] = Field(default_factory=dict)


class RunEvent(WireModel):
    """The standard emission shape: one OpenLineage ``RunEvent``, authored in pydantic.

    ``to_openlineage()`` yields the official client's ``event_v2.RunEvent`` (what the
    transports consume); ``to_wire()`` is the official serializer's dict of exactly that —
    the wire form can never drift from the client we pin.
    """

    event_type: RunState = Field(alias="eventType")
    event_time: str = Field(alias="eventTime")
    run: Run
    job: Job
    inputs: list[Dataset] = Field(default_factory=list)
    outputs: list[OutputDataset] = Field(default_factory=list)
    producer: str = PRODUCER
    schema_url: str = Field(default=RUN_EVENT_SCHEMA_URL, alias="schemaURL")

    def to_openlineage(self) -> event_v2.RunEvent:
        return event_v2.RunEvent(
            eventTime=self.event_time,
            producer=self.producer,
            run=event_v2.Run(runId=self.run.run_id, facets=self.run.facets.to_openlineage()),
            job=event_v2.Job(namespace=self.job.namespace, name=self.job.name, facets=self.job.facets),
            eventType=event_v2.RunState[self.event_type.name],
            inputs=[d.to_openlineage_input() for d in self.inputs],
            outputs=[d.to_openlineage_output() for d in self.outputs],
        )

    def to_wire(self) -> dict[str, Any]:
        return Serde.to_dict(self.to_openlineage())
