"""lineage-kit — the one compute-lineage layer (ruling R21).

A light wrapper over ``openlineage-python`` giving every Ray plane (Ray Data pipeline
stages, Ray actors, offline ``ray job submit`` / lance-ray drivers, and online Ray Serve
deployments) ONE emission shape and one job→stage→actor parent/child run seam.

Offline shape::

    with job_run("ingest", inputs=[("s3", "source-bucket")]) as run:
        layout(batch)                      # @stage("layout") — child run, auto-linked
        Actor.remote(lineage_context=run.context.model_dump_json())  # or runtime_env env_vars=run.context.as_env()

Online shape (Ray Serve)::

    class Transcriber(LineageActorMixin):
        def __init__(self) -> None:
            self.init_lineage("transcribe")            # root run — replica lifetime
        async def __call__(self, req) -> ...:
            with self.lineage_task("request"):         # child run per request
                ...

Transport is env-driven (``RASK_LINEAGE_ENDPOINT`` → HTTP; unset → logged no-op that
never crashes compute). See ``config.LineageSettings``.
"""

from lineage_kit import metrics as metrics
from lineage_kit.actor import LineageActorMixin
from lineage_kit.config import LineageSettings
from lineage_kit.consume import (
    AUTHOR_RUN_FACET,
    LANCE_RUN_FACET,
    LINEAGE_DOC_SCHEMA,
    DatasetRef,
    LineageDoc,
    LineageEdge,
    as_json_rows,
    parse_doc,
)
from lineage_kit.context import CONTEXT_ENV_VAR, LineageContext, coerce_context, current_context, resolve_context, use_context
from lineage_kit.emitter import (
    ClientEmitter,
    Emitter,
    NoopEmitter,
    RecordingEmitter,
    ambient_emitter,
    build_emitter,
    default_emitter,
    set_default_emitter,
    use_emitter,
)
from lineage_kit.runs import LineageRun, job_run, run_id_for
from lineage_kit.schemas import (
    BASE_FACET_SCHEMA_URL,
    DATASET_VERSION_FACET_SCHEMA_URL,
    DATASOURCE_FACET_SCHEMA_URL,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    OUTPUT_STATISTICS_FACET_SCHEMA_URL,
    PARENT_RUN_FACET_SCHEMA_URL,
    PRODUCER,
    RUN_EVENT_SCHEMA_URL,
    SCHEMA_FACET_SCHEMA_URL,
    TERMINAL_STATES,
    Dataset,
    DatasetVersionFacet,
    DatasourceFacet,
    ErrorMessageRunFacet,
    Job,
    OutputDataset,
    OutputStatisticsFacet,
    ParentRunFacet,
    Run,
    RunEvent,
    RunState,
    SchemaDatasetFacet,
    SchemaField,
    WireModel,
    custom_facet,
)
from lineage_kit.stage import stage


__all__ = [
    "AUTHOR_RUN_FACET",
    "BASE_FACET_SCHEMA_URL",
    "CONTEXT_ENV_VAR",
    "DATASET_VERSION_FACET_SCHEMA_URL",
    "DATASOURCE_FACET_SCHEMA_URL",
    "ERROR_MESSAGE_FACET_SCHEMA_URL",
    "LANCE_RUN_FACET",
    "LINEAGE_DOC_SCHEMA",
    "OUTPUT_STATISTICS_FACET_SCHEMA_URL",
    "PARENT_RUN_FACET_SCHEMA_URL",
    "PRODUCER",
    "RUN_EVENT_SCHEMA_URL",
    "SCHEMA_FACET_SCHEMA_URL",
    "TERMINAL_STATES",
    "ClientEmitter",
    "Dataset",
    "DatasetRef",
    "DatasetVersionFacet",
    "DatasourceFacet",
    "Emitter",
    "ErrorMessageRunFacet",
    "Job",
    "LineageActorMixin",
    "LineageContext",
    "LineageDoc",
    "LineageEdge",
    "LineageRun",
    "LineageSettings",
    "NoopEmitter",
    "OutputDataset",
    "OutputStatisticsFacet",
    "ParentRunFacet",
    "RecordingEmitter",
    "Run",
    "RunEvent",
    "RunState",
    "SchemaDatasetFacet",
    "SchemaField",
    "WireModel",
    "ambient_emitter",
    "as_json_rows",
    "build_emitter",
    "coerce_context",
    "current_context",
    "custom_facet",
    "default_emitter",
    "job_run",
    "parse_doc",
    "resolve_context",
    "run_id_for",
    "set_default_emitter",
    "stage",
    "use_context",
    "use_emitter",
]
