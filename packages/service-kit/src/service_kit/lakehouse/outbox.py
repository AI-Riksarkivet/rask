"""Durable object-store outbox for lineage events (#4) — closes the commit→publish loss window.

Today a producer commits its Lance write and then does a FIRE-AND-FORGET Dapr publish (the sidecar accepts
the RPC but there is no broker/consumer ack). A crash between the commit and a durable delivery loses the
lineage event: the data landed but the graph never learns of it. The DLQ only catches events that were
published *then* failed delivery; the reconcile back-fill only recovers version+schema, not the full event.

The outbox closes it: the producer stages the FULL ``RunEvent`` JSON as an object under
``<outbox_uri>/<run_id>.json`` BEFORE the publish, and deletes it once the publish returns. Because the
stage happens AFTER the commit, every surviving object corresponds to a real committed write (no phantoms).
If the process crashes before the delete — or the publish never lands — the object survives and the lineage
service's reconcile relay re-ingests it (idempotent on ``run_id``) and deletes it. Strictly richer than the
version+schema back-fill: inputs, author, and columnLineage are all preserved.

Storage-agnostic: an ``s3://`` outbox uses an ``S3FileSystem`` built from the SAME ``storage_options`` the
producer/relay already use; a local/``file://`` path uses the local filesystem (dev + unit tests).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import suppress

import pyarrow.fs as pafs

from service_kit.lakehouse import outbox_metrics
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)


def stage_event(outbox_uri: str, storage_options: StorageOptions, run_id: str, event_json: str) -> None:
    """Persist the event JSON at ``<outbox_uri>/<run_id>.json`` (overwrite — a redelivery re-stages the
    same run_id). Blocking object-store IO; callers run it in a threadpool."""
    fs, base = fs_and_base(outbox_uri, storage_options)
    fs.create_dir(base, recursive=True)  # local FS needs the parent dir; an S3 prefix marker is harmless
    with fs.open_output_stream(f"{base}/{run_id}.json") as stream:
        stream.write(event_json.encode("utf-8"))


def drop_event(outbox_uri: str, storage_options: StorageOptions, run_id: str) -> None:
    """Delete the staged event (called after a publish returns / after the relay re-ingests it). An
    already-absent object is fine (idempotent). Blocking IO; callers threadpool it."""
    fs, base = fs_and_base(outbox_uri, storage_options)
    with suppress(FileNotFoundError):
        fs.delete_file(f"{base}/{run_id}.json")


def read_event(outbox_uri: str, storage_options: StorageOptions, run_id: str) -> str | None:
    """The staged event JSON for one ``run_id``, or ``None`` if it isn't staged (already drained / never
    staged). The targeted read behind an admin DLQ replay (#83) — cheaper + race-safer than scanning the
    whole prefix. Blocking IO; callers threadpool it."""
    fs, base = fs_and_base(outbox_uri, storage_options)
    try:
        with fs.open_input_stream(f"{base}/{run_id}.json") as stream:
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
    """``(depth, oldest_age_seconds)`` — the saturation snapshot (#4 observability, docs/DECISIONS.md P1.1
    (outbox observability, the four signals)).

    A metadata-only LIST: no object bodies are read, so this is cheap enough to run on every reconcile tick
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
    """Yield ``(run_id, event_json)`` for staged events under the outbox prefix (the relay's input), OLDEST
    FIRST, at most ``limit`` of them.

    BOUNDED (audit finding, docs/DECISIONS.md P1.2 (bounded drain)): the drain previously materialised the
    ENTIRE prefix into memory inside the single-flight lock, so a backlog (exactly the situation the outbox
    exists for) could OOM or stall the reconcile tick — the relay would fail hardest precisely when it was
    needed most. The cap
    makes each tick's work bounded; the remainder drains on the next tick, oldest-first, so nothing starves.
    An absent prefix yields nothing. Blocking IO; the caller threadpools the whole drain.
    """
    fs, _ = fs_and_base(outbox_uri, storage_options)
    infos = _staged_infos(outbox_uri, storage_options)
    if limit is not None:
        infos = infos[:limit]
    for info in infos:
        run_id = info.path.rsplit("/", 1)[-1].removesuffix(".json")
        # TOCTOU: the medallion mover stages-then-drops on this SAME prefix continuously, so an object
        # listed above can vanish before we open it. A concurrently-dropped event was already published
        # (that's why it's being dropped) — skip it, don't let the race 500 the whole reconcile tick.
        try:
            stream = fs.open_input_stream(info.path)
        except FileNotFoundError:
            continue
        with stream:
            yield run_id, stream.readall().decode("utf-8")


async def publish_lineage_with_outbox(
    publisher: object,
    *,
    outbox_uri: str,
    storage_options: StorageOptions,
    run_id: str,
    event_json: str,
    pubsub_name: str,
    topic_name: str,
    timeout_seconds: float,
) -> None:
    """Durably publish a lineage event: stage → publish → drop-on-ack (#4).

    With no ``outbox_uri`` configured this degrades to a plain publish (pre-#4 behavior), so the outbox is
    opt-in. A publish failure PROPAGATES (the producer's existing RETRY / redelivery handles it) and leaves
    the staged object in place, so even a hard process crash between the stage and a durable delivery is
    recoverable by the relay. The delete is best-effort: a failed delete just leaves a redundant object the
    relay re-ingests idempotently and drops.
    """
    from fastapi.concurrency import run_in_threadpool

    from service_kit import dapr_publish

    staged = bool(outbox_uri)
    if staged:
        await run_in_threadpool(stage_event, outbox_uri, storage_options, run_id, event_json)
        outbox_metrics.record_staged()
    try:
        await dapr_publish.publish_event(
            publisher,
            timeout_seconds=timeout_seconds,
            pubsub_name=pubsub_name,
            topic_name=topic_name,
            data=event_json,
            data_content_type="application/json",
        )
    except Exception:
        # The event REMAINS staged — that is the crash window working, not data loss. Count it so a
        # sustained publish outage is visible (rising failures + rising depth) instead of silent, then
        # re-raise unchanged: the producer's retry/redelivery contract is unaltered by the metric.
        if staged:
            outbox_metrics.record_publish_failed()
        raise
    outbox_metrics.record_published()
    if staged:
        with suppress(Exception):
            await run_in_threadpool(drop_event, outbox_uri, storage_options, run_id)
