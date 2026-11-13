"""Durable object-store outbox for lineage events (#4) — narrows the loss window to commit→stage.

A producer commits its Lance write and then does a FIRE-AND-FORGET Dapr publish (the sidecar accepts the
RPC but there is no broker/consumer ack). A crash between the commit and a durable delivery loses the
lineage event: the data landed but the feed never learns of it. The DLQ only catches events that were
published *then* failed delivery.

The outbox narrows it: the producer stages the FULL event JSON — a ``RunEvent``, or the ``DatasetEvent``
every catalog DDL change is — under ``<outbox_uri>/<its envelope's id>.json`` BEFORE the publish,
and deletes it once the publish returns. Because the stage happens AFTER the commit, every surviving object
corresponds to a real committed write (no phantoms). If the process crashes before the delete — or the
publish never lands — the object survives and the lineage service's relay tick re-ingests it
idempotently and deletes it, with inputs, author and columnLineage all preserved.

NARROWED HERE, CLOSED BY THE VERSION LOG. A crash between the commit and the stage loses this event, and the
catalog's reconcile cron finds the committed version the feed lacks and announces it from the version log, under the
run id the commit derives, so a late copy of this event lands no second row (``catalog.api.commit_reconcile``,
[[LH-497]]). A stage that fails is counted and the call degrades to a plain publish.

Storage-agnostic: an ``s3://`` outbox uses an ``S3FileSystem`` built from the SAME ``storage_options`` the
producer/relay already use; a local/``file://`` path uses the local filesystem (dev + unit tests).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import suppress

import pyarrow.fs as pafs

from lakehouse import outbox_metrics
from lakehouse.cloud_events import CloudEvent
from lakehouse.objectfs import fs_and_base
from lakehouse.storage import split_s3_uri
from lakehouse.storage_options import StorageOptions, object_client, on_s3


log = logging.getLogger(__name__)


class UnpublishedEventError(RuntimeError):
    """A publish that did not reach the bus, and whether its event stays staged for the relay to deliver.

    ``staged`` is the outbox's own answer, never its configuration's: a stage that failed degrades the call to a plain
    publish, so a configured outbox can hold nothing for the relay. The message names the failure's type beside its
    text, since a bare ``TimeoutError()`` has none, and the failure is chained as the cause.

    Args:
        failure: What the publish raised.
        staged: Whether the outbox holds the event.

    Attributes:
        staged: Whether the outbox holds the event, so that its relay delivers it.
    """

    def __init__(self, failure: Exception, *, staged: bool) -> None:
        super().__init__(f"{type(failure).__name__}: {failure}")
        self.staged = staged


def left_staged(failure: Exception) -> bool:
    """Whether a failed publish left its event staged for the relay to deliver.

    Only the outbox can say so (:class:`UnpublishedEventError`); a failure raised before the publish reached it, as when
    the caller's storage options cannot be built, staged nothing.

    Args:
        failure: What the publish, or the work before it, raised.

    Returns:
        Whether the outbox holds the event.
    """
    return isinstance(failure, UnpublishedEventError) and failure.staged


def stage_event(outbox_uri: str, storage_options: StorageOptions, key: str, event_json: str) -> None:
    """Persist the event JSON at ``<outbox_uri>/<key>.json``. The key is the event's own, its envelope's id
    (`lakehouse.cloud_events`): the event's identity and its state together, so a run's COMPLETE cannot
    truncate its own FAIL, and a redelivery of the SAME event re-stages the same object. Blocking
    object-store IO; callers run it in a threadpool.

    A STAGE MUST NEED ONLY PUT PERMISSION ON ITS OWN KEY, and through pyarrow it did not. This called
    ``fs.create_dir(base, recursive=True)`` under a comment calling an S3 prefix marker "harmless" — but
    ``create_dir`` first tests that the BUCKET exists, and a credential scoped to the prefix has nothing
    on the bucket. Measured from the ingest pod 2026-09-10 with a freshly vended STS credential in hand:

        OSError: When testing for existence of bucket 'lance-catalog':
                 AWS Error ACCESS_DENIED during HeadBucket operation

    So the vend was correct and observed working, and the write still could not happen. Widening the
    session policy to cover the bucket is the wrong direction — it grants a capability the stager has no
    need for, to keep a mechanism whose only product is a marker object nobody reads. The exact twin of
    :func:`drop_event`, whose delete re-created that same marker.

    S3 goes through the estate's CLIENT seam (``lakehouse.storage``), a plain PutObject. Everything else
    keeps the filesystem path: a local outbox is a real directory and genuinely needs its parent made.
    """
    if on_s3(outbox_uri):
        bucket, prefix = split_s3_uri(outbox_uri.rstrip("/"))
        name = f"{key}.json"
        object_client(storage_options).put_object(
            Bucket=bucket,
            Key=f"{prefix.rstrip('/')}/{name}" if prefix else name,
            Body=event_json.encode("utf-8"),
        )
        return
    fs, base = fs_and_base(outbox_uri, storage_options)
    fs.create_dir(base, recursive=True)
    with fs.open_output_stream(f"{base}/{key}.json") as stream:
        stream.write(event_json.encode("utf-8"))


def drop_event(outbox_uri: str, storage_options: StorageOptions, key: str) -> None:
    """Delete the staged event (called after a publish returns / after the relay re-ingests it). An
    already-absent object is fine (idempotent). Blocking IO; callers threadpool it.

    A DELETE MUST NEED ONLY DELETE PERMISSION, and through pyarrow it did not. `S3FileSystem.delete_file`
    re-creates the parent directory marker after removing the object, so this path issued a **PutObject**
    on the prefix — and the relay's identity is granted, deliberately and correctly, only
    `s3:DeleteObject` on its own outbox (`lance.objectStorePolicy.lineage` in `chart/templates/_helpers.tpl`, statement
    `DrainItsOwnOutboxAndNothingElse`; the policy grants lineage no `PutObject` anywhere). So the drain
    had never once succeeded:

        lineage_outbox_event_stranded error="When creating key '_lineage_outbox/' in bucket
        'lance-catalog': AWS Error ACCESS_DENIED during PutObject operation" (measured 2026-09-10)

    The consequence was not a stranded object. The relay tick ingests, re-publishes, THEN drops —
    the first two succeed, so every staged event was redelivered to `lineage.events.v1` on every tick
    for the lifetime of the estate. The drop
    is the only thing that ends that loop.

    S3 goes through the estate's CLIENT seam (`lakehouse.storage`), which issues a plain DeleteObject
    and writes nothing. Everything else keeps the filesystem path: a local outbox is a real directory,
    and `delete_file` there needs no object-store permissions at all.
    """
    if on_s3(outbox_uri):
        bucket, prefix = split_s3_uri(outbox_uri.rstrip("/"))
        # The stage's own client (:func:`~lakehouse.storage_options.object_client`): a delete signed with less than the whole key pair falls back
        # to boto3's default chain, and the lineage pod carries no ambient `AWS_*` (measured 2026-09-10), so that would be
        # a delete with no credential at all; pinned in `test_the_outbox_drain_needs_no_write_permission.py`.
        client = object_client(storage_options)
        # An absent object already satisfies the post-condition, and S3's DeleteObject is idempotent for
        # a missing key (204, no error) — so this suppression is for a NON-S3 double or backend that
        # raises instead, the same tolerance the filesystem path below spells the same way. Catching
        # boto3's `client.exceptions.NoSuchKey` would couple this seam to one client's exception shape
        # for a case that client does not raise.
        with suppress(FileNotFoundError):
            client.delete_object(Bucket=bucket, Key=f"{prefix.rstrip('/')}/{key}.json" if prefix else f"{key}.json")
        return
    fs, base = fs_and_base(outbox_uri, storage_options)
    with suppress(FileNotFoundError):
        fs.delete_file(f"{base}/{key}.json")


def resolve_event(outbox_uri: str, storage_options: StorageOptions, run_id: str) -> tuple[str, str] | None:
    """Find the staged object for one RUN ID, as ``(key, event_json)``.

    The object key is per EVENT (its envelope's id) while the DLQ replay route addresses a RUN: its path
    parameter is a run id, not a key, and an exact-key read alone would answer 404 to every replay by run.

    Exact match first, so a caller that already holds a key (the relay, which reads keys straight off
    the filenames) pays nothing. Otherwise the newest `<run_id>@*.json` wins: a run's later event is the
    one worth replaying, and a COMPLETE staged after a FAIL is the outcome that stands.

    Returns the KEY as well as the payload because the caller must drop exactly what it replayed —
    dropping by run id would miss the object it just re-ingested and leave it for the relay to do again.
    """
    exact = read_event(outbox_uri, storage_options, run_id)
    if exact is not None:
        return run_id, exact
    prefix = f"{run_id}@"
    matches = [i for i in _staged_infos(outbox_uri, storage_options) if i.path.rsplit("/", 1)[-1].startswith(prefix)]
    if not matches:
        return None
    newest = matches[-1]  # `_staged_infos` is oldest-first
    key = newest.path.rsplit("/", 1)[-1].removesuffix(".json")
    payload = read_event(outbox_uri, storage_options, key)
    return None if payload is None else (key, payload)


def read_event(outbox_uri: str, storage_options: StorageOptions, key: str) -> str | None:
    """The staged event JSON for one ``run_id``, or ``None`` if it isn't staged (already drained / never
    staged). The targeted read behind an admin DLQ replay (#83) — cheaper + race-safer than scanning the
    whole prefix. Blocking IO; callers threadpool it."""
    fs, base = fs_and_base(outbox_uri, storage_options)
    try:
        with fs.open_input_stream(f"{base}/{key}.json") as stream:
            return stream.readall().decode("utf-8")
    except FileNotFoundError:
        return None


def _staged_infos(outbox_uri: str, storage_options: StorageOptions) -> list[pafs.FileInfo]:
    """Every staged `.json` object under the prefix, OLDEST FIRST.

    Oldest-first matters for the bounded drain: when a backlog exceeds the per-tick cap, the events that
    have been at risk LONGEST must drain first, and `outbox.oldest_age` must fall monotonically as the relay
    catches up. A newest-first (or arbitrary) order would let the oldest event starve indefinitely behind a
    steady arrival rate — the backlog would "drain" while the thing you are actually alerting on never moves.
    """
    fs, base = fs_and_base(outbox_uri, storage_options)
    infos = [
        i for i in fs.get_file_info(pafs.FileSelector(base, allow_not_found=True, recursive=False)) if i.type == pafs.FileType.File and i.path.endswith(".json")
    ]
    infos.sort(key=lambda i: i.mtime_ns or 0)
    return infos


def backlog(outbox_uri: str, storage_options: StorageOptions) -> tuple[int, float]:
    """``(depth, oldest_age_seconds)`` — the saturation snapshot (#4 observability, docs/adr/0003-p1-1-outbox-observability-the-four-signals.md P1.1
    (outbox observability, the four signals)).

    A metadata-only LIST: no object bodies are read, so this is cheap enough to run on every relay tick
    even when the outbox is healthy and empty (which is exactly when it must still report depth=0, or the
    gauge would go stale at its last non-zero reading and alert forever).
    """
    infos = _staged_infos(outbox_uri, storage_options)
    if not infos:
        return 0, 0.0
    oldest_ns = infos[0].mtime_ns or 0
    age = max(0.0, (time.time_ns() - oldest_ns) / 1e9) if oldest_ns else 0.0
    return len(infos), age


def list_events(outbox_uri: str, storage_options: StorageOptions, *, limit: int | None = None) -> Iterator[tuple[str, str]]:
    """Yield ``(key, event_json)`` for staged events under the outbox prefix (the relay's input), OLDEST
    FIRST, at most ``limit`` of them.

    THE FIRST ELEMENT IS THE DROP KEY, NOT A RUN ID, and the distinction is the whole reason
    :func:`resolve_event` exists. One run stages ``<run_id>@COMPLETE`` and ``<run_id>@FAIL`` as separate
    objects, each under its envelope's id, so the filename is ``<run_id>@<eventType>`` and a caller that
    treats it as a run id is comparing a key to an id. A caller wanting the RUN reads it off the
    parsed payload, the way `dlq._summary` does — and a catalog DDL change (a ``DatasetEvent``) names no
    run at all, so the key is the only name it has.

    BOUNDED (audit finding, docs/adr/0004-p1-2-bounded-oldest-first-outbox-drain.md P1.2 (bounded drain)): the drain previously materialised the
    ENTIRE prefix into memory inside the single-flight lock, so a backlog (exactly the situation the outbox
    exists for) could OOM or stall the relay tick — the relay would fail hardest precisely when it was
    needed most. The cap
    makes each tick's work bounded; the remainder drains on the next tick, oldest-first, so nothing starves.
    An absent prefix yields nothing. Blocking IO; the caller threadpools the whole drain.
    """
    fs, _ = fs_and_base(outbox_uri, storage_options)
    infos = _staged_infos(outbox_uri, storage_options)
    if limit is not None:
        infos = infos[:limit]
    for info in infos:
        key = info.path.rsplit("/", 1)[-1].removesuffix(".json")
        # TOCTOU: a publishing writer stages-then-drops on this SAME prefix continuously, so an object
        # listed above can vanish before we open it. A concurrently-dropped event was already published
        # (that's why it's being dropped) — skip it, don't let the race 500 the whole relay tick.
        try:
            stream = fs.open_input_stream(info.path)
        except FileNotFoundError:
            continue
        with stream:
            yield key, stream.readall().decode("utf-8")


async def publish_with_outbox(
    publisher: object,
    envelope: CloudEvent,
    *,
    outbox_uri: str,
    storage_options: StorageOptions,
    pubsub_name: str,
    topic_name: str,
    timeout_seconds: float,
) -> None:
    """Durably publish ANY bus event: stage → publish → drop-on-ack (#4).

    Lane-agnostic on purpose: what a lane brings is its event's envelope (`lakehouse.cloud_events`). The
    control lane needs the guarantee the lineage lane has — a dropped `table_published` cancels the next
    hop of a pipeline that waits on it — and a SECOND implementation is how two mechanisms doing one job
    drift apart (owner ruling 2026-08-25).

    The staged object is named by the envelope's ``id``, the one identity an event has on the bus, in the
    outbox and in lineage's feed: two events of one run are two objects, and one event staged again is the
    same object.

    A publish failure PROPAGATES and leaves the staged object in place, so even a hard process crash between the stage
    and a durable delivery is recoverable by the relay. The delete is best-effort: a failed delete leaves a redundant
    object the relay re-ingests idempotently and drops, logged with its cause and counted as the stage's and the
    publish's failures are, so a store refusing every delete shows as itself rather than as a backlog nobody explains.

    Args:
        publisher: The sidecar's client.
        envelope: The event in the envelope its lane derives from it (`lakehouse.cloud_events`). What is staged is the
            event alone, ``envelope.data``, as its producer signed it: the relay that delivers a staged copy derives
            the same envelope from it, so the copy goes out under the id the first publish carried.
        outbox_uri: The lane's OWN outbox prefix. A lane-specific relay drains it and re-ingests what it finds (the
            lineage relay records into the feed), so two lanes on one prefix would feed each relay the other's events.
            Empty publishes unstaged: the outbox is opt-in.
        storage_options: What reaches that prefix.
        pubsub_name: The pub/sub component to publish through.
        topic_name: The lane's topic.
        timeout_seconds: How long the publish may take.

    Raises:
        UnpublishedEventError: The publish did not reach the bus; its ``staged`` says whether the relay holds the event.
    """
    from fastapi.concurrency import run_in_threadpool

    from lakehouse import dapr_publish

    event_json = json.dumps(envelope.data)
    staged = bool(outbox_uri)
    if staged:
        try:
            await run_in_threadpool(stage_event, outbox_uri, storage_options, envelope.id, event_json)
        except Exception as exc:
            # A stage failure degrades this call to the pre-#4 plain publish instead of raising past the
            # publish. Both callers catch and continue, so raising here loses the event outright — whereas
            # an unstaged event that reaches the bus is still delivered. Counted because every other outbox
            # signal describes objects that exist and is blind to this path by construction.
            outbox_metrics.record_stage_failed()
            log.warning("outbox_stage_failed", extra={"outbox_uri": outbox_uri, "error": str(exc)})
            staged = False
        else:
            outbox_metrics.record_staged()
    try:
        await dapr_publish.publish_event(publisher, envelope, pubsub_name=pubsub_name, topic_name=topic_name, timeout_seconds=timeout_seconds)  # noqa: TID251
    except Exception as exc:
        # A staged event REMAINS staged — that is the crash window working, not data loss. Counted so a sustained publish
        # outage is visible (rising failures + rising depth) instead of silent. Whether it is staged is this call's to say,
        # since a stage that failed above left the relay nothing although an outbox is configured ([[LH-573]]).
        if staged:
            outbox_metrics.record_publish_failed()
        raise UnpublishedEventError(exc, staged=staged) from exc
    outbox_metrics.record_published()
    if not staged:
        return
    try:
        await run_in_threadpool(drop_event, outbox_uri, storage_options, envelope.id)
    except Exception as exc:  # the event is delivered; what is left is a staged copy the relay re-ingests and drops
        outbox_metrics.record_drop_failed()
        log.warning("outbox_drop_failed", extra={"outbox_uri": outbox_uri, "key": envelope.id, "error": str(exc)})
