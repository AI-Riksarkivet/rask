"""Version history + time-travel — the compare-versions read-side.

The audit trail of the write plane: who = ``reviewer``, when = the Lance version's
timestamp, what = lineage; this module serves the WHEN (per-unit history + historical
snapshots) that powers the annotator's compare-versions panel.

Two version number-spaces exist pre-merge, and every response names its own via
``X-Annotations-Version-Source``: in full catalog mode (``rest_catalog_mode``) the
history comes from the catalog's governed version surface (``/version/list`` +
version-pinned ``count_rows`` — writes commit THERE, so the local replica is absent
or stale); every other configuration reads the local table's own ``ds.versions()``
lineage.

Both branches answer one row per version, and each row costs a read: a manifest open
plus a filtered scan locally, an HTTP round-trip in catalog mode. They are issued
through a bounded, process-wide pool rather than in a ``for`` loop, and counted by
pushdown rather than by materializing the matching ids — see :data:`VERSION_FANOUT`.
"""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Annotated, Any, Final

import lance
from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from annotator.annotations.schema import ANNOTATIONS_TABLE
from annotator.api.security import RawBearerToken
from service_kit.exceptions import NotFoundError
from service_kit.lancekit.keys import chunk_key_filter, validate_doc_key
from service_kit.lancekit.reader import CatalogTableReader, open_catalog_reader
from service_kit.lancekit.registry import table_dataset
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle


router = APIRouter(tags=["annotate"])

#: Names which version NUMBER-SPACE a version number belongs to (``catalog`` vs the
#: local ``direct``/``local`` lineage) — pre-merge the two are different counters,
#: so compare-versions tooling must never silently mix them.
VERSION_SOURCE_HEADER = "X-Annotations-Version-Source"

#: How many versions this module reads at once, PROCESS-wide.
#:
#: A history answers one row per version, and every row costs a manifest open plus a filtered scan
#: (locally) or an HTTP round-trip (in catalog mode). Walking them in a `for` loop made the response
#: `limit` × that latency — up to 200 of them in series, against S3, while holding a threadpool
#: worker. The fan-out below is what removes the series; the bound is what stops the cure being
#: worse, and it is PROCESS-wide rather than per request precisely so N concurrent histories cannot
#: multiply into N × fan-out threads.
VERSION_FANOUT: Final = 8

#: Lazily-threaded (a `ThreadPoolExecutor` starts no worker until something is submitted), so
#: importing this module — which every annotator process does — costs nothing.
_VERSION_POOL: Final = ThreadPoolExecutor(max_workers=VERSION_FANOUT, thread_name_prefix="annotation-versions")


def _in_order[T, R](items: list[T], work: Callable[[T], R]) -> list[tuple[T, R]]:
    """Run `work` over `items` with the bounded pool, dropping the ones that are GONE.

    Results come back in INPUT order, which the newest-first contract depends on, and a
    `NotFoundError` skips its own entry rather than failing the whole listing — the retention race
    the sequential loop already handled, kept handled.
    """
    pending: list[tuple[T, Future[R]]] = [(item, _VERSION_POOL.submit(work, item)) for item in items]
    done: list[tuple[T, R]] = []
    for item, future in pending:
        try:
            done.append((item, future.result()))
        except NotFoundError:
            continue
    return done


class AnnotationVersion(BaseModel):
    """One point in a unit's edit history — a Lance version + when it was committed +
    how many annotations this unit had at it (the audit/compare-versions trail)."""

    version: int
    timestamp: str
    count: int


@router.get("/annotations/{doc_id}/{speech_id}/{chunk_id}/versions")
def annotation_versions(
    state: StateDep,
    response: Response,
    doc_id: str,
    speech_id: int,
    chunk_id: int,
    caller_token: RawBearerToken = None,
    dataset: DatasetParam = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[AnnotationVersion]:
    """The unit's edit history (most-recent first, capped): each Lance version + its
    timestamp + the count of THIS unit's annotations at it. Powers the compare-versions
    panel — the read-side of the write-plane provenance story (who=reviewer, when=version,
    what=lineage). The ``X-Annotations-Version-Source`` header names the number-space
    the versions belong to: the catalog's in full catalog mode (where writes commit),
    else the local table's."""
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    settings = state.settings
    if settings.rest_catalog_mode:
        # Full catalog mode: the catalog owns the version number-space (saves commit
        # there), so history comes from ITS version surface — never the local replica,
        # which is absent or stale here and used to silently return [].
        response.headers[VERSION_SOURCE_HEADER] = "catalog"
        reader = open_catalog_reader(
            table_id=settings.catalog_table_id(handle.id, ANNOTATIONS_TABLE),
            settings=settings,
            caller_token=caller_token,
        )
        where = chunk_key_filter(declared, doc_id, (speech_id, chunk_id))
        return catalog_annotation_versions(reader, where, limit=limit)
    response.headers[VERSION_SOURCE_HEADER] = "local"
    try:
        ds = table_dataset(handle, ANNOTATIONS_TABLE)
    except NotFoundError:
        return []
    where = chunk_key_filter(declared, doc_id, (speech_id, chunk_id))
    return local_annotation_versions(ds, where, limit=limit)


def local_annotation_versions(ds: Any, where: str, *, limit: int) -> list[AnnotationVersion]:
    """The unit's history off the LOCAL table's own version lineage, newest first and capped.

    Counted by PUSHDOWN — `count_rows(filter=...)` — not by materializing an Arrow table of matching
    ids and reading `.num_rows` off it, which is what this did: a table built only to be measured,
    once per version. And read through the bounded pool (:data:`VERSION_FANOUT`), because the
    manifest opens are independent of each other and the latency is the whole cost.
    """
    listed = list(reversed(ds.versions()))[:limit]
    counted = _in_order(listed, lambda v: int(ds.checkout_version(int(v["version"])).count_rows(filter=where)))
    return [AnnotationVersion(version=int(v["version"]), timestamp=iso_timestamp(v.get("timestamp")), count=count) for v, count in counted]


def catalog_annotation_versions(reader: CatalogTableReader, where: str, *, limit: int) -> list[AnnotationVersion]:
    """The unit's history off the catalog's version surface: ONE governed
    ``version/list`` (server-side descending + capped) + a per-version ``count_rows``
    pinned at that version. A table the catalog doesn't know yet degrades to ``[]``
    (no annotations is not an error — same as a missing local table); a version
    reclaimed between the list and its count (maintenance retention race) drops that
    entry instead of failing the whole listing.

    The counts are issued through the same bounded pool as the local branch: each is an independent
    HTTP round-trip to the catalog, and in series `limit` of them are the whole response time."""
    try:
        versions = reader.versions(limit=limit)
    except NotFoundError:
        return []
    counted = _in_order(list(versions), lambda v: reader.count_rows(where, version=v.version))
    return [AnnotationVersion(version=v.version, timestamp=millis_iso(v.timestamp_millis), count=count) for v, count in counted]


def millis_iso(timestamp_millis: int | None) -> str:
    """A catalog version timestamp (epoch millis) → ISO-8601 UTC string ('' when absent)."""
    if timestamp_millis is None:
        return ""
    return datetime.fromtimestamp(timestamp_millis / 1000, tz=UTC).isoformat()


def iso_timestamp(ts: object) -> str:
    """A Lance version timestamp → ISO string (datetime or already-string)."""
    isofmt = getattr(ts, "isoformat", None)
    return isofmt() if callable(isofmt) else str(ts or "")


def checkout(ds: lance.LanceDataset, version: int) -> lance.LanceDataset:
    """Time-travel to a version, translating Lance's raw not-found (an out-of-range or
    reclaimed version) into a clean NotFoundError — mirroring ``table_dataset`` so a bad
    ``?version`` is a 404, not an opaque 500. Only the NOT-FOUND family maps to 404;
    operational failures (permissions, connectivity — this table can live on S3)
    propagate honestly instead of masquerading as a missing version."""
    try:
        return ds.checkout_version(version)
    except (ValueError, FileNotFoundError) as e:
        raise NotFoundError(f"annotations version {version} not found") from e
    except OSError as e:
        if "not found" in str(e).lower():
            raise NotFoundError(f"annotations version {version} not found") from e
        raise
