"""Producer-side OpenLineage emitter for the mock medallion flow.

This is **compute-layer instrumentation, not part of the lineage service**. It uses the
official ``openlineage-python`` client to build spec-correct ``RunEvent``s for the
bronze → silver → gold flow and either writes them to ``sample_events.json`` or POSTs
them to a running lineage service at the OpenLineage default path ``/api/v1/lineage``.

The service never imports this module — producers and consumers stay decoupled, exactly
as a real job (lance-ray / an ingest job) would emit to us over HTTP. ``openlineage-python``
is a dev/demo dependency for that reason.

Run::

    uv run python -m lineage.seed --write lineage/sample_events.json
    uv run python -m lineage.seed --emit http://localhost:2334
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import attr
from openlineage.client import OpenLineageClient
from openlineage.client.event_v2 import InputDataset, Job, OutputDataset, Run, RunEvent, RunState
from openlineage.client.facet_v2 import (
    RunFacet,
    column_lineage_dataset,
    dataset_version_dataset,
    datasource_dataset,
    error_message_run,
    job_type_job,
    ownership_job,
    schema_dataset,
    tags_dataset,
)
from openlineage.client.serde import Serde
from openlineage.client.transport.http import HttpConfig, HttpTransport


_PRODUCER = "https://github.com/Borg93/lance-ns/tree/main/lineage"
# Ray is the compute engine that runs these jobs (the OpenLineage ``Job``); Lance is the data
# they read/write (the ``Dataset``). The ``jobType`` facet's integration records that.
_JOB_NS = "ray-jobs"
_INTEGRATION = "RAY"

# Where each Lance table physically lives (S3-compatible) — the standard ``dataSource`` facet.
_URIS: dict[str, str] = {
    "raw_events": "s3://landing/raw/events",
    "bronze$events": "s3://lakehouse/bronze/events",
    "silver$features": "s3://lakehouse/silver/features",
    "gold$catalog": "s3://lakehouse/gold/catalog",
}
# Governance labels per dataset — the standard ``tags`` facet.
_TAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "raw_events": (("layer", "raw"),),
    "bronze$events": (("layer", "bronze"),),
    "silver$features": (("layer", "silver"), ("pii", "false")),
    "gold$catalog": (("layer", "gold"),),
}


@attr.define
class AuthorRunFacet(RunFacet):
    """Custom run facet carrying the OIDC identity that ran the job (the who-by).

    OpenLineage has no standard "run author" facet, so we define one; the lineage
    service reads ``run.facets.author.{name,sub}`` from it.
    """

    name: str = attr.field(default="")
    sub: str | None = attr.field(default=None)


def _schema(fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    if not fields:
        return {}
    facet = schema_dataset.SchemaDatasetFacet(fields=[schema_dataset.SchemaDatasetFacetFields(name=n, type=t) for n, t in fields])
    return {"schema": facet}


# out_field -> [(input_namespace, input_dataset, input_field, transformation_type, transformation_subtype)].
# The standard ``columnLineage`` facet (#24): which input column(s) each output column was derived from,
# with the transformation kind (DIRECT/IDENTITY for pass-through, DIRECT/TRANSFORMATION for a real change).
ColumnMap = dict[str, tuple[tuple[str, str, str, str, str], ...]]


def _column_lineage(mapping: ColumnMap) -> dict[str, Any]:
    if not mapping:
        return {}
    facet = column_lineage_dataset.ColumnLineageDatasetFacet(
        fields={
            out_field: column_lineage_dataset.Fields(
                inputFields=[
                    column_lineage_dataset.InputField(
                        namespace=ns,
                        name=name,
                        field=field,
                        transformations=[column_lineage_dataset.Transformation(type=ttype, subtype=subtype)],
                    )
                    for ns, name, field, ttype, subtype in inputs
                ]
            )
            for out_field, inputs in mapping.items()
        }
    )
    return {"columnLineage": facet}


def _ds_facets(name: str, fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """Standard dataset facets for ``name``: ``schema`` + ``dataSource`` (where it lives) + ``tags``."""
    facets: dict[str, Any] = dict(_schema(fields))
    uri = _URIS.get(name)
    if uri:
        facets["dataSource"] = datasource_dataset.DatasourceDatasetFacet(name=uri, uri=uri)
    tags = _TAGS.get(name)
    if tags:
        facets["tags"] = tags_dataset.TagsDatasetFacet(tags=[tags_dataset.TagsDatasetFacetFields(key=k, value=v) for k, v in tags])
    return facets


def _in(namespace: str, name: str, *fields: tuple[str, str]) -> InputDataset:
    return InputDataset(namespace=namespace, name=name, facets=_ds_facets(name, fields))


def _out(
    namespace: str,
    name: str,
    version: int | None,
    *fields: tuple[str, str],
    columns: ColumnMap | None = None,
) -> OutputDataset:
    """An output dataset with its standard facets + the Lance ``version`` this run produced.

    ``version=None`` (a failed run produced no version) omits the ``version`` facet. ``columns`` adds the
    standard ``columnLineage`` facet (which input column each output column came from). (#24)
    """
    facets = _ds_facets(name, fields)
    if version is not None:
        facets["version"] = dataset_version_dataset.DatasetVersionDatasetFacet(datasetVersion=str(version))
    facets.update(_column_lineage(columns or {}))
    return OutputDataset(namespace=namespace, name=name, facets=facets)


def _job(name: str, author: str, kind: str) -> Job:
    """The compute job (Ray) with standard ``ownership`` + ``jobType`` facets.

    ``kind`` is the OpenLineage ``jobType``: ``ETL`` (land raw into bronze) or
    ``TRANSFORMATION`` (between medallion layers).
    """
    return Job(
        namespace=_JOB_NS,
        name=name,
        facets={
            "ownership": ownership_job.OwnershipJobFacet(owners=[ownership_job.Owner(name=author, type="user")]),
            "jobType": job_type_job.JobTypeJobFacet(processingType="BATCH", integration=_INTEGRATION, jobType=kind),
        },
    )


def _event(
    *,
    run_id: str,
    event_time: str,
    job_name: str,
    author: str,
    kind: str,
    inputs: list[InputDataset],
    outputs: list[OutputDataset],
    state: RunState = RunState.COMPLETE,
    error: str | None = None,
) -> RunEvent:
    run_facets: dict[str, Any] = {"author": AuthorRunFacet(name=author, sub=author)}
    if error is not None:
        run_facets["errorMessage"] = error_message_run.ErrorMessageRunFacet(message=error, programmingLanguage="PYTHON")
    return RunEvent(
        eventTime=event_time,
        producer=_PRODUCER,
        eventType=state,
        run=Run(runId=run_id, facets=run_facets),
        job=_job(job_name, author, kind),
        inputs=inputs,
        outputs=outputs,
    )


def build_events() -> list[RunEvent]:
    """The realistic medallion run history as spec-correct OpenLineage events.

    The flow (Ray runs each compute ``Job``; each output carries its produced Lance ``version``;
    authors are OIDC subs; ``jobType`` = ETL into bronze, TRANSFORMATION between layers):

    1. **alice** runs an **ETL** job: ``raw_events`` → ``bronze$events`` (v1). ``payload`` is a
       Lance **blob** column (multimodal bytes — image/video/audio).
    2. **data_eng**'s embed job **FAILS** (CUDA OOM) — recorded with its ``errorMessage``, but it
       produced no data (no version, no derivation).
    3. **data_eng** retries: embeds ``bronze$events`` → ``silver$features`` (v1, adds ``embedding``
       — a Lance data-evolution add-column).
    4. **data_eng** refines ``silver$features`` in place → v2 (adds ``caption``) — the
       "run against silver again" pass; a version bump, not a self-derivation.
    5. **analyst** aggregates ``silver$features`` → ``gold$catalog`` (v1) and embeds the upstream
       provenance as a JSONB ``lineage`` column inside the Lance file (Lance ``pa.json_()``), so the
       lineage travels with the data and is queryable in place (``json_extract``/``json_get``).

    Run ids are fixed (idempotent re-ingest under AGE MERGE). Terminal-state events only (a real
    producer also emits START/RUNNING; omitted here for a compact history).
    """
    # ``payload`` is the raw multimodal blob; ``payload_src`` is which camera/sensor it came from
    # (the dataset's *source system* is the separate ``raw_events`` node upstream of bronze).
    bronze_cols = (("id", "int"), ("payload", "blob"), ("payload_src", "string"))
    # silver drops the raw blob — the embed job keeps id + payload_src and adds the features.
    silver_v1_cols = (("id", "int"), ("payload_src", "string"), ("embedding", "array<float>"))
    silver_v2_cols = (*silver_v1_cols, ("caption", "string"))
    # Gold carries the keys forward (id, payload_src) + the features, plus its own provenance JSONB.
    gold_cols = (
        ("id", "int"),
        ("payload_src", "string"),
        ("embedding", "array<float>"),
        ("caption", "string"),
        ("lineage", "json"),
    )
    # Column-level provenance (#24): which input column each output column came from. The embed turns the
    # raw blob into a vector (a real TRANSFORMATION); keys pass through (IDENTITY); the caption model reads
    # the embedding (a same-dataset column edge — in-place refinement adds a column from another).
    silver_from_bronze: ColumnMap = {
        "id": (("bronze", "bronze$events", "id", "DIRECT", "IDENTITY"),),
        "payload_src": (("bronze", "bronze$events", "payload_src", "DIRECT", "IDENTITY"),),
        "embedding": (("bronze", "bronze$events", "payload", "DIRECT", "TRANSFORMATION"),),
    }
    caption_from_silver: ColumnMap = {
        "caption": (("silver", "silver$features", "embedding", "DIRECT", "TRANSFORMATION"),),
    }
    gold_from_silver: ColumnMap = {
        "id": (("silver", "silver$features", "id", "DIRECT", "IDENTITY"),),
        "payload_src": (("silver", "silver$features", "payload_src", "DIRECT", "IDENTITY"),),
        "embedding": (("silver", "silver$features", "embedding", "DIRECT", "IDENTITY"),),
        "caption": (("silver", "silver$features", "caption", "DIRECT", "IDENTITY"),),
    }
    return [
        _event(
            run_id="11111111-1111-1111-1111-111111111111",
            event_time="2026-06-20T09:00:00Z",
            job_name="ingest_events",
            author="alice",
            kind="ETL",
            inputs=[_in("source", "raw_events")],
            outputs=[_out("bronze", "bronze$events", 1, *bronze_cols)],
        ),
        _event(
            run_id="22222222-2222-2222-2222-222222222220",
            event_time="2026-06-21T08:00:00Z",
            job_name="embed_features",
            author="data_eng",
            kind="TRANSFORMATION",
            inputs=[_in("bronze", "bronze$events", *bronze_cols)],
            outputs=[_out("silver", "silver$features", None, *silver_v1_cols, columns=silver_from_bronze)],
            state=RunState.FAIL,
            error="CUDA OOM while embedding batch 7/12",
        ),
        _event(
            run_id="22222222-2222-2222-2222-222222222222",
            event_time="2026-06-21T09:00:00Z",
            job_name="embed_features",
            author="data_eng",
            kind="TRANSFORMATION",
            inputs=[_in("bronze", "bronze$events", *bronze_cols)],
            outputs=[_out("silver", "silver$features", 1, *silver_v1_cols, columns=silver_from_bronze)],
        ),
        _event(
            run_id="33333333-3333-3333-3333-333333333333",
            event_time="2026-06-21T11:00:00Z",
            job_name="caption_features",
            author="data_eng",
            kind="TRANSFORMATION",
            inputs=[_in("silver", "silver$features", *silver_v1_cols)],
            outputs=[_out("silver", "silver$features", 2, *silver_v2_cols, columns=caption_from_silver)],
        ),
        _event(
            run_id="44444444-4444-4444-4444-444444444444",
            event_time="2026-06-21T12:00:00Z",
            job_name="aggregate_gold",
            author="analyst",
            kind="TRANSFORMATION",
            inputs=[_in("silver", "silver$features", *silver_v2_cols)],
            outputs=[_out("gold", "gold$catalog", 1, *gold_cols, columns=gold_from_silver)],
        ),
    ]


def events_as_dicts() -> list[dict[str, Any]]:
    """The medallion events as OpenLineage JSON dicts (via the client's serializer)."""
    return [json.loads(Serde.to_json(event)) for event in build_events()]


def write_sample(path: Path) -> None:
    """Write the medallion events to ``path`` as pretty OpenLineage JSON."""
    path.write_text(json.dumps(events_as_dicts(), indent=2) + "\n")


def emit(url: str) -> None:
    """POST every medallion event to a running lineage service (OpenLineage HTTP transport)."""
    client = OpenLineageClient(transport=HttpTransport(HttpConfig(url=url)))
    for event in build_events():
        client.emit(event)


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit the mock medallion OpenLineage events.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", type=Path, metavar="PATH", help="write events to a JSON file")
    group.add_argument("--emit", metavar="URL", help="POST events to a lineage service base URL")
    args = parser.parse_args()
    if args.write:
        write_sample(args.write)
    else:
        emit(args.emit)


if __name__ == "__main__":
    main()
