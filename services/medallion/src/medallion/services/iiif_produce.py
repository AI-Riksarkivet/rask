"""The lance-ray producer's IIIF ingest business logic — the P7a page-image head of the cascade.

:func:`ingest_iiif` harvests one volume's pages from the Riksarkivet IIIF Image API (through the
``storage.iiif`` reading client — the surviving library role of the retired ``IIIFCachedSource`` cache)
and lands them DIRECTLY as the BRONZE page-image blob-v2 Lance dataset (R23: raw is the external world,
not a governed tier — the retired raw→bronze mover's stage stamp merged into the bronze ingest;
``source_rowid`` roots at bronze), then emits the ONE ``iiif://… -> bronze`` OpenLineage bronze-write
event. It **never publishes ``medallion.bronze`` itself**: the producer's own ``/bronze-arrival``
subscription reacts to the COMPLETE bronze write and fires the cascade — publishing both would
double-fire it (pinned by ``tests/unit/test_medallion.py``). The bronze dataset *is* the page cache now:
versioned, governed; HTR then runs as cascade compute (bronze→silver layout/lines, silver→gold
transcription — the P7b movers).

Two execution modes, same as the media head: in-process harvest for small volumes/dev, and — with
``MEDALLION_RAY_ENABLED`` — the Ray branch, submitting the self-contained ``scripts/ray_iiif_ingest_job.py``
through the same Jobs-REST seam the stage movers use (idempotent reattach, TRACEPARENT injection).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx
import lance
from dapr.aio.clients import DaprClient
from fastapi.concurrency import run_in_threadpool
from opentelemetry import trace

from medallion.core.config import MedallionSettings, project_namespace
from medallion.schemas.events import build_run_event
from medallion.services.ingest import IngestResult, ingest_to_bronze
from medallion.services.ray_submit import submit_iiif_ingest_job
from service_kit.lakehouse import outbox
from service_kit.lakehouse import schema as lakehouse_schema
from service_kit.lakehouse.sources import SourceObject
from service_kit.lakehouse.warehouse_registry import UnresolvableProjectError, project_root
from storage.iiif import build_image_url, fetch_image, get_image_ids


log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

#: The producer's OpenLineage operation — the job name + the run-id seed prefix
#: (``run_id_for(f"[{project}-]iiif-ingest-{token}")``, deterministic so redelivery MERGEs).
IIIF_OPERATION = "iiif-ingest"


def iiif_input(base_url: str, volume_id: str) -> tuple[str, str]:
    """The EXTERNAL-source input dataset on the bronze-write event (R23): ``(iiif://<host>, <volume id>)``.

    The scheme://authority namespace + in-source name follows the OpenLineage external-dataset naming
    convention — the graph records which manifest a bronze page dataset was harvested from without
    pretending IIIF is a Lance dataset (raw is the external world, never a catalog tier)."""
    host = urlsplit(base_url).netloc or base_url.rstrip("/")
    return (f"iiif://{host}", volume_id)


def iiif_head_enabled(settings: MedallionSettings) -> bool:
    """Whether the IIIF ingest head is configured — compute + a bronze target URI.

    Unlike the media head no source bucket is needed (the source is the IIIF API itself), and a local-path
    ``iiif_bronze_uri`` is a legitimate dev configuration — so only compute + the target gate it."""
    return bool(settings.compute_enabled and settings.iiif_bronze_uri)


class IIIFVolumeSource:
    """A ``SourceAdapter`` over one volume's IIIF manifest — each page's bytes + its Image-API URL.

    Pages yield in sorted image-id order (``get_image_ids`` sorts), so the positional ``id`` column is
    reproducible: same manifest -> same ids -> same rows every harvest.
    """

    def __init__(
        self,
        volume_id: str,
        *,
        base_url: str,
        query_params: str,
        timeout: float,
        max_pages: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._volume_id = volume_id
        self._base_url = base_url.rstrip("/")
        self._query_params = query_params
        self._timeout = timeout
        self._max_pages = max_pages
        self._client = client

    def iter_objects(self) -> Iterator[SourceObject]:
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            image_ids = get_image_ids(self._volume_id, base_url=self._base_url, timeout=self._timeout, client=client)
            if self._max_pages is not None:
                image_ids = image_ids[: self._max_pages]
            for image_id in image_ids:
                url = build_image_url(image_id, base_url=self._base_url, query_params=self._query_params)
                yield SourceObject(uri=url, data=fetch_image(url, client=client))
        finally:
            if self._client is None:  # only close a client this source itself opened
                client.close()


def _page_key(obj: SourceObject) -> str:
    """The page identity within the volume (the ALTO filename stem) — parsed from the Image-API URL this
    module itself built (``.../arkis!<image_id>/<query>``), so the shape is ours to rely on."""
    return obj.uri.split("arkis!", 1)[-1].split("/", 1)[0]


def _harvest_and_ingest(settings: MedallionSettings, bronze_uri: str, volume_id: str, max_pages: int | None) -> IngestResult:
    """Blocking half (IIIF HTTP + Lance IO — callers threadpool it): harvest every page of the volume and
    land the bronze blob-v2 page dataset in one atomic overwrite commit."""
    source = IIIFVolumeSource(
        volume_id,
        base_url=settings.iiif_base_url,
        query_params=settings.iiif_query_params,
        timeout=settings.iiif_timeout_seconds,
        max_pages=max_pages,
    )
    return ingest_to_bronze(
        source,
        bronze_uri,
        settings.storage_options(),
        max_objects=settings.ingest_max_objects,
        max_total_bytes=settings.ingest_max_total_bytes,
        chunk_objects=settings.ingest_chunk_objects,
        chunk_bytes=settings.ingest_chunk_bytes,
        # The page-lane superset columns (design §2.2): provenance the HTR movers + exporter key on.
        extra_columns={"volume_id": lambda _obj: volume_id, "page_key": _page_key},
    )


def _measure_bronze(bronze_uri: str, storage_options: dict[str, str]) -> IngestResult:
    """Measure a bronze dataset the RAY job wrote (blocking — threadpool it): the head still owns the ONE
    bronze-write emit, so it reads version/rows/schema off the committed dataset, exactly like the movers'
    measure-after-Ray path. ``source_uris`` stays empty — the IIIF input is the external manifest id."""
    dataset = lance.dataset(bronze_uri, storage_options=storage_options or None)
    return IngestResult(
        version=int(dataset.version),
        row_count=dataset.count_rows(),
        source_uris=[],
        fields=lakehouse_schema.facet_fields(dataset.schema),
    )


async def ingest_iiif(
    dapr: DaprClient,
    settings: MedallionSettings,
    volume_id: str,
    *,
    token: str | None = None,
    max_pages: int | None = None,
    project: str = "",
) -> dict[str, str]:
    """Harvest one IIIF volume into the bronze page-image dataset and emit its ONE bronze-write event.

    Returns ``{"status": "iiif_disabled"}`` when the head isn't configured (the route maps it to 409),
    ``{"status": "publish_failed"}`` when the lineage emit fails (→ 503, retryable: the harvest is an
    idempotent overwrite and the token-derived run id MERGEs), else ``{"status": "ingested"}``. The
    cascade trigger is NOT published here — ``/bronze-arrival`` fires it off this very event.

    ``project`` (#84 per-tenant routing) routes the volume to
    ``<project-root>/medallion/<bronze_namespace>-pages`` (its own dataset directory beside the events
    lane's bronze) and qualifies the run-id seed + lineage identities, so volumes/tenants never collide;
    fail closed on an unresolvable project (never the shared ``iiif_bronze_uri``).
    """
    if not iiif_head_enabled(settings):
        return {"status": "iiif_disabled"}
    token = token or uuid.uuid4().hex[:12]
    bronze_uri = settings.iiif_bronze_uri
    if project:
        if not settings.control_root:
            raise UnresolvableProjectError(f"project {project!r} iiif ingest refused: routing is disabled (MEDALLION_CONTROL_ROOT unset)")
        root = await run_in_threadpool(project_root, settings.control_root, settings.storage_options(), project)
        if root is None:
            raise UnresolvableProjectError(f"project {project!r} has no active warehouse")
        bronze_uri = f"{root}/medallion/{settings.bronze_namespace}-pages"
    with tracer.start_as_current_span("medallion.ingest_iiif") as span:
        span.set_attribute("iiif.volume_id", volume_id)
        if settings.ray_enabled:
            # The Ray branch: the self-contained harvest job runs on the unified cluster (Jobs-REST,
            # deterministic submission id → redelivery/retry re-attaches); the head then measures the
            # committed dataset so the emit below is identical to the in-process path.
            span.set_attribute("lance.medallion.compute", "ray")
            await submit_iiif_ingest_job(settings, bronze_uri=bronze_uri, volume_id=volume_id, max_pages=max_pages, token=token)
            result = await run_in_threadpool(_measure_bronze, bronze_uri, settings.storage_options())
        else:
            span.set_attribute("lance.medallion.compute", "in_process")
            result = await run_in_threadpool(_harvest_and_ingest, settings, bronze_uri, volume_id, max_pages)
        span.set_attribute("lance.version", result.version)
        span.set_attribute("lance.row_count", result.row_count)
    event = build_run_event(
        operation=IIIF_OPERATION,
        author=settings.producer_author,
        job_namespace=settings.job_namespace,
        # ONE external-source input — the harvested manifest as ``(iiif://<host>, <volume id>)`` (R23).
        # Provenance per page lives IN the data (source_uri, volume_id, page_key columns), so the graph
        # stays one node per volume, not one per page.
        inputs=[iiif_input(settings.iiif_base_url, volume_id)],
        output_namespace=project_namespace(project, settings.bronze_namespace),
        output_name=project_namespace(project, settings.iiif_bronze_dataset),
        version=result.version,
        row_count=result.row_count,
        source_uri=bronze_uri,
        schema_fields=result.fields,  # blob-aware: payload:blob at the page head
        token=token,
        project=project or None,
    )
    try:
        # Stage-then-publish-then-drop through the outbox (#4), like every other lineage emit. This event
        # IS the cascade head — /bronze-arrival fires the medallion.bronze trigger off it — so a failed
        # emit surfaces (503, retryable: overwrite + deterministic run id make the retry converge).
        await outbox.publish_lineage_with_outbox(
            dapr,
            outbox_uri=settings.lineage_outbox_uri,
            storage_options=settings.storage_options(),
            run_id=event["run"]["runId"],
            event_json=json.dumps(event),
            pubsub_name=settings.pubsub,
            topic_name=settings.lineage_topic,
            timeout_seconds=settings.publish_timeout_seconds,
        )
    except Exception as exc:
        log.warning("medallion_iiif_publish_failed", extra={"token": token, "volume_id": volume_id, "error": str(exc)})
        return {"status": "publish_failed", "token": token}
    dataset = project_namespace(project, settings.iiif_bronze_dataset)
    log.info("medallion_iiif_ingested", extra={"token": token, "volume_id": volume_id, "dataset": dataset, "rows": result.row_count})
    return {"status": "ingested", "token": token, "dataset": dataset, "pages": str(result.row_count)}
