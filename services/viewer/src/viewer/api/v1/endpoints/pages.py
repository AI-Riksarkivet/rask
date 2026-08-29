"""The document viewer's read path — page images out of a BRONZE blob-v2 dataset.

Bronze page datasets store the image bytes in a blob-v2 column (``payload``) alongside the tabular
columns that say which page each one is. Serving them needs two things to be right, and each of them
has already been got wrong once.

READS MUST PRESERVE CARDINALITY. ``read_blobs`` / ``take_blobs`` silently DROP null rows through
pylance 9 (three selected rows with one null payload return two payloads) and from 10.0.0 return
``None`` in that slot instead — so pairing their output POSITIONALLY against a second scan of the
tabular columns misattributes every page after the first gap, with no exception raised. A failed
harvest or a skipped page is exactly that case. Neither route here pairs anything by position: the
listing reads only the blob DESCRIPTORS, which arrive in the same scan as the tabular columns, and
the byte route resolves ``id`` -> stable ``_rowid`` -> ``take_blobs(ids=[rowid])``, where ``None`` in
the slot IS the null signal. ``blobs.py`` says the same in its own words — "the take-path remains
correct for single-row serving (``ids=[rowid]``, where an empty result IS the null signal)". A plain
``large_binary`` payload has no take-path at all; both routes decide that shape from ``ds.schema``
and serve it from the request-bounded filtered scan instead (see ``_page_rows`` / ``_take_page``).

READS MUST BE BOUNDED BY THE REQUEST, NOT BY THE DATASET (VS-05). Both routes used to run one
unfiltered, unbounded ``read_aligned_table`` at ``blob_handling="all_binary"``, which materialises
EVERY row's bytes: serving one page cost the whole corpus, and the listing's ``limit`` was applied
only after the scan, so a ``can_get_metadata`` route's memory was bounded by the volume. A blob
column scanned at DEFAULT ``blob_handling`` yields ``struct<kind, position, size, blob_id,
blob_uri>`` and reads no object bytes at all, so ``size`` and ``has_payload`` are answerable for
free; the bytes are fetched for one row, by row id, only on the route that serves them.

See ``docs/architecture/lance-blob-v2-findings.md`` for the measurements behind both rules, and
``docs/architecture/document-viewer.md`` for how this endpoint came to exist.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Annotated

import httpx
import lance
import pyarrow as pa
from fastapi import APIRouter, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from lance.blob import BlobFile
from pydantic import BaseModel, computed_field

from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError, UnauthorizedError
from service_kit.governed.audit import FAILURE, audit
from service_kit.governed.deps import RawBearerToken
from service_kit.lakehouse.blobs import is_blob_field
from service_kit.lancekit.predicate import eq
from service_kit.media.authz import table_object
from service_kit.media.deps import StateDep
from viewer.api.security import READ_DATA, READ_METADATA, CheckerDep, CurrentSubject
from viewer.api.v1.endpoints.media import MAX_BUFFERED_BLOB_BYTES, _handle_size, _stream_handle


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api", tags=["pages"])

#: The tabular columns a page row carries beside its blob. Kept explicit so a dataset that grows a
#: column does not silently start shipping it to the browser.
_PAGE_COLUMNS = ["id", "source_uri", "stage"]


class Page(BaseModel):
    """One page in a bronze dataset — metadata only; the bytes come from ``GET /api/page``."""

    id: int
    source_uri: str
    stage: str
    #: The payload's length in bytes, read from the blob DESCRIPTOR rather than from the bytes — the
    #: listing must never dereference a payload (VS-05).
    #:
    #: ONE CASE WHERE THAT IS NOT A BYTE COUNT, stated rather than smuggled: for an EXTERNAL
    #: descriptor (``kind == EXTERNAL_KIND``) ``size == 0`` means THE WHOLE OBJECT, whose length the
    #: dataset does not carry — see ``carry_external_descriptor`` in ``service_kit.lakehouse.blobs``.
    #: Such a row reports 0 here. Resolving it would mean a per-row HEAD against the object store,
    #: which is exactly the IO this listing exists to avoid; no consumer branches on ``size`` (the
    #: lakehouse zone renders it as a caption), so an honest 0 beats a scan.
    size: int
    #: False when this row's payload is null. Surfaced rather than hidden: a row that failed to
    #: acquire is a real state of the dataset, and a viewer that silently skips it reports a corpus as
    #: complete when it is not.
    #:
    #: MODALITY-FREE. This was `has_image`, and the route it describes serves an ARBITRARY governed
    #: table — audio, video and PDF corpora included. CLAUDE.md's test for a shared seam is "would this
    #: be right for audio?", and `has_image` is not.
    has_payload: bool

    @computed_field
    @property
    def has_image(self) -> bool:
        """Deprecated alias for `has_payload`. REMOVE after one release.

        A rename is a wire change, and the web zones are their own Deployments — during a rolling
        upgrade an old `rask-web-lakehouse` pod talks to a new viewer, and `storage.ts` reads
        `has_image`. Dropping it in the same release would break exactly that window. `computed_field`
        rather than a plain property so it is actually SERIALISED; it mirrors rather than defaults, so
        the two can never disagree.
        """
        return self.has_payload


class PageListing(BaseModel):
    #: The catalog table id these pages came from (e.g. bronze$pages).
    dataset: str
    pages: list[Page]


#: Magic-byte prefixes for the formats a governed corpus actually carries. Evidence, not assumption:
#: a JPEG really does start `FF D8 FF`.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF", "application/pdf"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
)


def sniff_media_type(payload: bytes) -> str:
    """What these bytes are, or an honest admission that we cannot tell.

    The governed tier schema is `{id, payload, stage, lineage, source_rowid}` with `payload` OPAQUE,
    and `table` is a caller-supplied catalog id — so there is no MIME to read anywhere. This route used
    to answer `image/jpeg` regardless, which is false for the audio, video and PDF corpora the same
    route serves.

    `application/octet-stream` for anything unrecognised, because a guess is worse than an admission:
    "bytes I cannot describe" is true, while `image/jpeg` over a WAV is not.
    """
    for prefix, media_type in _MAGIC:
        if payload.startswith(prefix):
            return media_type
    # RIFF is a container: the format is the four bytes at offset 8, not the header.
    if payload.startswith(b"RIFF") and len(payload) >= 12:
        return {b"WAVE": "audio/wav", b"AVI ": "video/x-msvideo", b"WEBP": "image/webp"}.get(payload[8:12], "application/octet-stream")
    return "application/octet-stream"


def _resolve(state: StateDep, table: str, token: str | None) -> str:
    """Resolve a catalog table id (``bronze$pages``) to its dataset location.

    THROUGH THE CATALOG, never a caller-supplied URI. Taking an ``s3://`` parameter would let any
    caller point this endpoint at any bucket the viewer's credentials can reach and have it stream
    the bytes back — the viewer would become a read primitive for the whole object store. It also
    made the viewer disagree with the catalog by construction: it could render a dataset the catalog
    had never heard of, which is exactly the ungoverned state this gate exists to close.

    The catalog is the one that knows where a registered table lives, so it is the one asked.
    """
    base = state.settings.catalog_uri
    if not base:
        raise NotFoundError("the viewer has no catalog configured (MEDIA_CATALOG_URI), so it cannot resolve a table")
    # The CALLER's bearer, not a service credential (#90). `raw_bearer`'s own docstring says why:
    # the catalog checks one relation on one `table:` object and injects no row predicate, so a
    # service token answers 200 for a caller with no grant at all — the two identities diverge, not
    # the rows. This call used to carry no Authorization header whatsoever, which meant it 401'd on
    # any auth-enabled catalog and, when it did succeed, succeeded as nobody in particular.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # THE PROCESS-WIDE POOL, not a fresh client per call (VS-12). A new `httpx.Client(base_url=...)`
    # here paid a TLS handshake and a throwaway connection pool on every `/api/page(s)`; the pooled
    # client built once in the lifespan is on `state.http` and `system.py` already reuses it the same
    # way. It carries no `base_url`, so the resolve passes the catalog URI as an absolute url. The
    # module fallback only fires for bare `AppState` constructions in unit tests.
    http = state.http if state.http is not None else httpx
    try:
        r = http.post(f"{base}/v1/table/{table}/describe", json={}, headers=headers, timeout=30.0)
    except httpx.RequestError as exc:
        # AN OUTAGE, NOT AN ABSENCE — and the old message said so ("catalog unreachable") while the
        # class it raised said the opposite. `httpx.RequestError` is the base of ConnectError,
        # ConnectTimeout, ReadTimeout and DNS failure: all transient, all clearing on their own. A 404
        # is TERMINAL, so the zone rendered "page not found", the reader stopped, and nothing retried.
        # This is the same laundering the 401/403 branch below refuses to do, for the same reason.
        #
        # The exception goes to the LOG, never the detail: `_problem` puts `str(exc)` verbatim into the
        # client body with no redaction at any status, so interpolating it here put the internal host
        # and port on the wire. `ns_errors` redacts every 5xx detail for exactly this reason and never
        # applied, because the class chosen was a 4xx — mislabelling the outage is what made the leak
        # reachable.
        logger.exception("catalog unreachable while resolving %r", table)
        raise ServiceUnavailableError(f"the catalog is unreachable, so {table!r} cannot be resolved right now") from exc
    # 401/403 are NOT "unknown table". Reporting them as "catalog does not know table X" sends the
    # next reader hunting for a missing registration that is not the problem; the annotator lost real
    # debugging time to exactly this laundering. Say which failure it is — and now that the caller's
    # bearer IS forwarded, say whether one was sent, because "no credential" and "credential refused"
    # are different problems with different fixes.
    if r.status_code in (401, 403):
        detail = "no bearer was sent (the caller is anonymous)" if not token else "the caller's bearer was refused"
        raise UnauthorizedError(f"the catalog rejected the credential resolving {table!r} (HTTP {r.status_code}) — {detail}")
    if r.status_code >= 500:
        # A 5xx RESPONSE is the same outage as a refused connection — the catalog (or a proxy in
        # front of it) answered "I am broken", not "that table does not exist". This branch was
        # `>= 400` and laundered it into a terminal 404; found by the adversarial re-audit, because
        # the connection-level tests above raise before a response exists and never walk this path.
        logger.error("catalog answered %s resolving %r", r.status_code, table)
        raise ServiceUnavailableError(f"the catalog is failing (HTTP {r.status_code}), so {table!r} cannot be resolved right now")
    if r.status_code >= 400:
        raise NotFoundError(f"catalog does not know table {table!r} (HTTP {r.status_code})")
    location = r.json().get("location")
    if not location:
        raise NotFoundError(f"catalog returned no location for {table!r}")
    return str(location)


def _open(state: StateDep, table: str, token: str | None) -> lance.LanceDataset:
    """Open the dataset a catalog table points at.

    404 only when the table is genuinely absent (that decision belongs to `_resolve`); a driver
    failure opening it is a 503, because it is an outage and a 404 would stop the reader for good.
    """
    location = _resolve(state, table, token)
    try:
        return lance.dataset(location, storage_options=state.settings.storage_options())
    except Exception as exc:  # noqa: BLE001 — any driver failure here is an outage, not an absence
        # A RustFS outage, expired vended credentials and a corrupt manifest all land here, and every
        # one of them was reported as a missing page. The location is deliberately absent from the
        # detail as well as the exception text: it is an `s3://` path the caller never supplied and
        # has no business learning from a failure.
        logger.exception("table %r resolves to %r, which is unreadable", table, location)
        raise ServiceUnavailableError(f"the storage backing {table!r} is unreadable right now") from exc


async def _authorized_dataset(
    state: StateDep,
    table: str,
    token: str | None,
    *,
    checker: object,
    subject: str,
    relation: str,
    action: str,
) -> lance.LanceDataset:
    """Check the caller against ``table`` and return the open dataset, or raise.

    The check runs BEFORE the dataset is opened, so a denied caller never causes a read of the bytes
    they were denied — and, less obviously, cannot use the 404-vs-403 timing of a catalog resolve to
    probe which tables exist.

    Blocking Lance/S3 IO goes to the threadpool. These routes were sync `def` (FastAPI ran them
    there automatically); awaiting the checker makes them coroutines, and a coroutine that calls
    `lance.dataset()` inline blocks the event loop for every other request in the process.
    """
    obj = table_object(table)
    if not await checker(user=subject, relation=relation, obj=obj):  # ty: ignore[call-non-callable]
        audit(action, FAILURE, subject=subject, resource=table, relation=relation)
        raise ForbiddenError(f"{subject} lacks {relation} on {obj}")
    return await run_in_threadpool(_open, state, table, token)


def _page_rows(ds: lance.LanceDataset, limit: int) -> tuple[pa.Table, bool]:
    """The listing's scan, bounded by the REQUEST, plus whether ``payload`` came back as descriptors.

    ``limit`` is pushed into the scan rather than applied to the result. That is the OOM half of
    VS-05: capped at 500 by the query model but never handed to Lance, the read was bounded by the
    volume, so ~10k pages at ~1 MB was ~10 GB materialised to return 100 metadata rows.

    DEFAULT ``blob_handling``, so a blob-v2 ``payload`` arrives as ``struct<kind, position, size,
    blob_id, blob_uri>`` and no object bytes are read at all.

    The flag exists because ``table`` is a free caller-supplied catalog id: a ``payload`` that is a
    plain ``large_binary`` column rather than blob-v2 is a reachable shape, and there the same scan
    returns real bytes that must be measured with ``len``. Asked of ``ds.schema``, not of the result:
    the scan strips the Arrow extension marker from the returned field (measured on pylance 10.0.0),
    so ``is_blob_field`` answers False for a blob column read back out of a table.
    """
    rows = ds.to_table(columns=[*_PAGE_COLUMNS, "payload"], limit=limit)
    return rows, is_blob_field(ds.schema.field("payload"))


def _take_page(ds: lance.LanceDataset, table: str, page_id: int) -> tuple[BlobFile | BytesIO, int, str]:
    """Resolve one page's ``id`` to an open payload handle, size and sniffed media type. All blocking IO.

    TWO SHAPES, decided by ``ds.schema`` exactly as `_page_rows` decides its listing: ``table`` is a
    free caller-supplied catalog id, so a ``payload`` that is a plain ``large_binary`` column rather
    than blob-v2 is a reachable shape. ``take_blobs`` refuses a non-blob column outright, so an
    unconditional take made the same table answer 200 on the listing and 500 here.

    Blob-v2: ``id`` -> stable ``_rowid`` -> ``take_blobs(ids=[...])``. ``ids`` survive deletes and
    compaction where positional ``indices`` do not, and no step pairs a blob read against a second
    scan, so the null-row landmine in this module's docstring cannot apply. The whole corpus is no
    longer read to find one row (VS-05).

    Plain ``large_binary``: there is no take-path, so the filtered scan itself carries the bytes —
    still bounded by the request (one matched row, ``limit=1``), and the cell's own validity is the
    null signal. The bytes are wrapped in a ``BytesIO`` so both shapes hand the caller the same
    seekable handle contract.

    First match wins on both shapes, preserving what ``ids.index(page_id)`` did for a non-unique
    ``id`` column.

    Only 12 bytes are read to sniff: ``_MAGIC``'s longest prefix is 8 and the RIFF branch needs the
    four bytes at offset 8, so 12 is exact. The handle is rewound afterwards because the caller reads
    from the same one.

    Both 404s are decided HERE, before any response object exists — the rule
    ``services/viewer/tests/test_media_null_payload.py`` exists to enforce: once a streaming response
    has sent its headers the status is already chosen and an exception can no longer become one.
    """
    if not is_blob_field(ds.schema.field("payload")):
        rows = ds.to_table(columns=["payload"], filter=eq("id", page_id), limit=1)
        if rows.num_rows == 0:
            raise NotFoundError(f"no page with id {page_id} in {table!r}")
        cell = rows.column("payload")[0]
        if not cell.is_valid:
            raise NotFoundError(f"row {page_id} in {table!r} has no payload")
        payload = bytes(cell.as_py())
        return BytesIO(payload), len(payload), sniff_media_type(payload[:12])
    ids = ds.to_table(columns=["id"], filter=eq("id", page_id), with_row_id=True)
    if ids.num_rows == 0:
        raise NotFoundError(f"no page with id {page_id} in {table!r}")
    rowid = int(ids.column("_rowid")[0].as_py())
    blob = ds.take_blobs("payload", ids=[rowid])[0]
    if blob is None:
        # A registered page whose harvest produced no bytes. 404 with the reason, so the UI can say
        # "this page failed to harvest" instead of rendering a broken image icon. `None` in the slot
        # is how pylance 10.0.0 reports it; through 9.0.0 the row was omitted from the list entirely.
        raise NotFoundError(f"row {page_id} in {table!r} has no payload")
    size = _handle_size(blob)
    head = blob.read(min(12, size))
    blob.seek(0)
    return blob, size, sniff_media_type(head)


def _read_all(blob: BlobFile | BytesIO, size: int) -> bytes:
    """The whole payload, for a blob small enough to buffer."""
    with blob as f:
        return f.read(size)


@router.get("/pages", summary="Pages in a bronze page dataset")
async def list_pages(
    state: StateDep,
    checker: CheckerDep,
    subject: CurrentSubject,
    token: RawBearerToken,
    table: Annotated[str, Query(description="Catalog table id, e.g. bronze$pages.")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> PageListing:
    """Page metadata, in dataset order.

    Note what is NOT here: the image bytes. A listing that inlined them would move hundreds of
    megabytes to render a contact sheet; the browser fetches each page's bytes from ``/api/page``,
    which the ``<img>`` tag streams on demand.
    """
    ds = await _authorized_dataset(state, table, token, checker=checker, subject=subject, relation=READ_METADATA, action="viewer.pages.list")
    rows, descriptors = await run_in_threadpool(_page_rows, ds, limit)
    pages: list[Page] = []
    for i in range(rows.num_rows):
        cell = rows.column("payload")[i]
        if descriptors:
            # Cell validity is the null signal for a descriptor column (correct from pylance 9;
            # lance-blob-v2-findings.md records that it is wrong only at 8.0.0, and every manifest
            # pins >= 10.0.0). No payload is dereferenced to answer either field.
            has_payload = bool(cell.is_valid)
            size = int(cell.as_py()["size"]) if has_payload else 0
        else:
            payload = cell.as_py()
            has_payload = payload is not None
            size = len(payload) if payload else 0
        pages.append(
            Page(
                id=rows.column("id")[i].as_py(),
                source_uri=rows.column("source_uri")[i].as_py() or "",
                stage=rows.column("stage")[i].as_py() or "",
                size=size,
                has_payload=has_payload,
            )
        )
    return PageListing(dataset=table, pages=pages)


@router.get("/page", summary="One row's payload bytes")
async def get_page(
    state: StateDep,
    checker: CheckerDep,
    subject: CurrentSubject,
    token: RawBearerToken,
    table: Annotated[str, Query(description="Catalog table id, e.g. bronze$pages.")],
    page_id: Annotated[int, Query(alias="id", ge=0, description="The page's `id` column value.")],
) -> Response:
    """The raw image bytes for one page.

    Selected by the ``id`` COLUMN, never by row position: positional indexing into a blob read is
    the misattribution bug this module exists to avoid, and a caller holding an id from the listing
    must get that page or a 404 — never a different one. The filter pushes that selection into the
    scan, so the cost is one page rather than the corpus (VS-05).

    Buffered while small, streamed above the threshold. ``payload`` is opaque and ``table`` is a free
    query parameter, so the response size is whatever the DATA names — the same reason `/api/media`
    draws this line, and the threshold is shared with it rather than re-derived. An EMPTY payload is
    deliberately a 200 with a zero-length body here, not the 404 `blob_response` gives a derivative:
    a governed row whose payload is genuinely zero bytes is present, and this route's absence signal
    is the null above.
    """
    ds = await _authorized_dataset(state, table, token, checker=checker, subject=subject, relation=READ_DATA, action="viewer.page.read")
    blob, size, media_type = await run_in_threadpool(_take_page, ds, table, page_id)
    headers = {"Cache-Control": "public, max-age=300"}
    if size > MAX_BUFFERED_BLOB_BYTES:
        return StreamingResponse(
            _stream_handle(blob, size),
            media_type=media_type,
            # Known exactly from the probe, and safe to declare because the generator owns this
            # handle: the bytes it yields and the size measured here come from the same open blob.
            headers={**headers, "Content-Length": str(size)},
        )
    return Response(
        content=await run_in_threadpool(_read_all, blob, size),
        media_type=media_type,
        headers=headers,
    )
