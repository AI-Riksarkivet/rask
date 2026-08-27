"""The medallion-producer producer's MEDIA ingest business logic — the multimodal head of the cascade (§9).

:func:`ingest_media` lands external media objects as a bronze blob-v2 table (through the provider-agnostic
:class:`service_kit.lakehouse.sources.S3Source` seam), emits the ``source URIs -> bronze media`` OpenLineage event with the
blob-aware schema facet, then publishes the media-chain trigger — the deployed bronze→silver media mover
consumes it and derives the inline artifacts (thumbnail + embedding) by CONTENT in the generic compute.

Unlike ``/produce`` (a dummy emitter that works compute-off), media ingest is REAL data by definition, so
it requires compute + the media settings — a disabled head returns an explicit contract the route maps to
409 rather than silently emitting fake provenance. Best-effort on the emit; the TRIGGER publish is the
cascade head, so its failure surfaces (503) exactly like ``/produce``.
"""

from __future__ import annotations

import io
import json
import logging

import pyarrow.fs as pafs
from dapr.aio.clients import DaprClient
from fastapi.concurrency import run_in_threadpool
from opentelemetry import trace
from PIL import Image

from medallion.core.config import MedallionSettings
from medallion.schemas.events import build_run_event
from medallion.services.ingest import IngestResult, ingest_to_bronze
from service_kit import dapr_publish
from service_kit.lakehouse import outbox
from service_kit.lakehouse.objectfs import s3_filesystem
from service_kit.lakehouse.sources import S3Source


log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

#: The deterministic demo samples seeded into the source prefix (media_seed_samples) — the stand-in for an
#: external media drop. Two solid-color PNGs, so the derived embeddings differ per row (real pixel features).
_SAMPLES: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("img-a.png", (200, 40, 40)),
    ("img-b.png", (40, 40, 200)),
)


def media_head_enabled(settings: MedallionSettings) -> bool:
    """Whether the media ingest head is configured — compute + S3 + a bronze target + a source bucket.

    ``s3_endpoint`` is required too: the head seeds/reads the source prefix through an S3 filesystem, so
    local-path compute (a supported mover configuration) cannot host it — 409, not a KeyError 500."""
    return bool(settings.compute_enabled and settings.s3_endpoint and settings.media_bronze_uri and settings.media_source_bucket)


def _filesystem(settings: MedallionSettings) -> pafs.S3FileSystem:
    """A pyarrow S3 filesystem over the same endpoint/creds the compute writes with (path-style, http-ok)."""
    return s3_filesystem(settings.storage_options(), allow_bucket_creation=True)


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _seed_and_ingest(settings: MedallionSettings) -> IngestResult:
    """Blocking half (S3 + Lance IO — callers threadpool it): optionally seed the demo samples into the
    source prefix, then land every source object as a bronze blob-v2 row."""
    fs = _filesystem(settings)
    if settings.media_seed_samples:
        for name, color in _SAMPLES:
            path = f"{settings.media_source_bucket}/{settings.media_source_prefix}/{name}"
            with fs.open_output_stream(path) as stream:
                stream.write(_png(color))
    source = S3Source(fs, settings.media_source_bucket, settings.media_source_prefix)
    return ingest_to_bronze(
        source,
        settings.media_bronze_uri,
        settings.storage_options(),
        max_objects=settings.ingest_max_objects,
        max_total_bytes=settings.ingest_max_total_bytes,
        chunk_objects=settings.ingest_chunk_objects,
        chunk_bytes=settings.ingest_chunk_bytes,
    )


def _split_source_uri(source_uri: str) -> tuple[str, str]:
    """``s3://lake/batch/img.png`` -> (``s3://lake/batch``, ``img.png``).

    The namespace must carry the URI scheme — see the call site. Follows the cascade's source-uri
    convention, kept local rather than shared: a private helper lifted into a shared module for a
    second caller is a coupling neither lane asked for.
    """
    base, _, leaf = source_uri.rpartition("/")
    return (base, leaf) if base else ("", source_uri)


async def ingest_media(dapr: DaprClient, settings: MedallionSettings, token: str, originator: str | None = None) -> dict[str, str]:
    """Land external media as bronze blobs, emit its lineage, and trigger the media chain.

    Returns ``{"status": "media_disabled"}`` when the head isn't configured (the route maps it to 409 —
    real media can't be dummied), ``{"status": "publish_failed"}`` when the emit or the trigger publish
    fails (→ 503, retryable: the ingest is an idempotent overwrite), else ``{"status": "ingested"}``.
    """
    if not media_head_enabled(settings):
        return {"status": "media_disabled"}
    # Idempotency: reuse a caller-supplied key (its 503-retry contract) so a retry MERGEs on the same
    # deterministic run_ids instead of double-firing the media chain (bug hunt 2026-07-13).
    with tracer.start_as_current_span("medallion.ingest_media") as span:
        result = await run_in_threadpool(_seed_and_ingest, settings)
        span.set_attribute("lance.version", result.version)
        span.set_attribute("lance.row_count", result.row_count)
    event = build_run_event(
        operation="ingest_media",
        author=settings.producer_author,
        job_namespace=settings.job_namespace,
        # One input per source object: the graph records source-URI -> bronze provenance in the data path.
        #
        # SPLIT AT THE LAST SEGMENT so the namespace keeps the URI SCHEME. `is_external_source` is
        # literally `"://" in namespace`, and R23 rests on it: raw is the external world, never a
        # governed tier, so these inputs must read as external. Emitted as `("source", uri)` they did
        # not — every media event named an input that looked like a governed `table:source`, which
        # nobody holds a grant on, so `GET /events` hid the whole event from EVERY caller under FGA
        # (the feed shows a row only if the reader can see every dataset it references).
        # The source-uri convention: `iiif://vol/00012.jpg` -> (`iiif://vol`, `00012.jpg`).
        inputs=[_split_source_uri(uri) for uri in result.source_uris],
        output_namespace=settings.media_bronze_namespace,
        output_name=settings.media_bronze_dataset,
        version=result.version,
        row_count=result.row_count,
        source_uri=settings.media_bronze_uri,
        schema_fields=result.fields,  # blob-aware: the graph shows payload:blob at the media head (#24)
        token=token,
        # `author` is the SERVICE that performed the ingest; `originator` is the person who asked for
        # it, resolved at the door. They are different lanes on purpose — the ORIGINATOR lane exists
        # precisely for work a service runs on somebody's behalf. None on a service-to-service call:
        # the shared token names nobody, and an unattributable run must stay unattributed rather than
        # address an inbox actor named after a role.
        originator=originator,
    )
    # Two SEPARATE failure domains (audit): a failed EMIT means no run landed — a retry re-ingests and
    # emits fresh, no duplicate possible. A failed TRIGGER after a landed emit still 503s (the trigger IS
    # the cascade); the retry then overwrites a NEW bronze version and emits for THAT version — every
    # COMPLETE in the graph maps to a real committed write (truthful, not duplicated), and the earlier
    # version's run simply has no derived silver, like any superseded write.
    try:
        # Stage-then-publish-then-drop through the outbox (#4), like every other lineage emit (produce.py,
        # transform.py) — the MEDIA head was the one producer still bypassing it (audit 2026-07-14), leaving
        # the multimodal path the exact commit→publish loss window #4 exists to close. A crash between the
        # blob commit and the publish ack now leaves the FULL event staged for the reconcile relay. The
        # media-chain TRIGGER below stays a bare publish on purpose: the outbox re-ingests lineage, it never
        # re-fires triggers — trigger loss is the documented idempotency-token caller-retry contract.
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
        log.warning("medallion_media_publish_failed", extra={"token": token, "stage": "emit", "error": str(exc)})
        return {"status": "publish_failed", "token": token}
    try:
        # The media-chain trigger (consumed by the media mover's durable consumer). Published AFTER the
        # lineage emit so the graph never shows a derived silver before its bronze head exists.
        await dapr_publish.publish_event(
            dapr,
            timeout_seconds=settings.publish_timeout_seconds,
            pubsub_name=settings.pubsub,
            topic_name=settings.media_topic,
            data=json.dumps(
                {
                    "token": token,
                    "dataset": settings.media_bronze_dataset,
                    "namespace": settings.media_bronze_namespace,
                    # The human the chain is for, threaded past the head. `/produce`'s cascade reads
                    # this back off the bronze event in `_cascade_originator`; the media head fires its
                    # own trigger instead, so without it the sub died at bronze and every derive below
                    # authored as a role literal. Omitted (byte-identical payload) when unset — the
                    # service path names nobody and must not invent one.
                    **({"originator": originator} if originator else {}),
                }
            ),
            data_content_type="application/json",
        )
    except Exception as exc:
        log.warning("medallion_media_publish_failed", extra={"token": token, "stage": "trigger", "error": str(exc)})
        return {"status": "publish_failed", "token": token}
    log.info(
        "medallion_media_ingested",
        extra={"token": token, "dataset": settings.media_bronze_dataset, "rows": result.row_count},
    )
    return {"status": "ingested", "token": token, "dataset": settings.media_bronze_dataset}
