"""The document viewer's read path — page images out of a BRONZE blob-v2 dataset.

Bronze page datasets store the image bytes in a blob-v2 column (``payload``) alongside the tabular
columns that say which page each one is. Serving them needs exactly one thing to be right, and it is
the thing that is easy to get wrong:

READS MUST PRESERVE CARDINALITY. ``read_blobs`` / ``take_blobs`` silently DROP null rows — three
selected rows with one null payload return two payloads — so pairing their output positionally
against a scan of the tabular columns misattributes every page after the first gap, with no
exception raised. A failed harvest or a skipped page is exactly that case. This module therefore
reads through ``service_kit.lakehouse.blobs.read_aligned_table``
(``blob_handling="all_binary"``), which returns the blob column and the tabular columns in ONE scan
with nulls preserved, so alignment holds by construction and there is no mask to keep in step.

See ``docs/architecture/lance-blob-v2-findings.md`` for the measurements behind that, and
``docs/architecture/document-viewer.md`` for how this endpoint came to exist.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
import lance
from fastapi import APIRouter, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError, UnauthorizedError
from service_kit.governed.audit import FAILURE, audit
from service_kit.governed.deps import RawBearerToken
from service_kit.lakehouse.blobs import read_aligned_table
from service_kit.media.authz import table_object
from service_kit.media.deps import StateDep
from viewer.api.security import READ_DATA, READ_METADATA, CheckerDep, CurrentSubject


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
    size: int
    #: False when the harvest left this page's payload null. Surfaced rather than hidden: a page
    #: that failed to fetch is a real state of the dataset, and a viewer that silently skips it
    #: reports a volume as complete when it is not.
    has_image: bool


class PageListing(BaseModel):
    #: The catalog table id these pages came from (e.g. bronze$pages).
    dataset: str
    pages: list[Page]


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
    try:
        with httpx.Client(base_url=base, timeout=30.0) as http:
            r = http.post(f"/v1/table/{table}/describe", json={}, headers=headers)
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
        return lance.dataset(location, storage_options=state.settings.storage_options)
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
    rows = await run_in_threadpool(read_aligned_table, ds, columns=[*_PAGE_COLUMNS, "payload"])
    pages: list[Page] = []
    for i in range(min(rows.num_rows, limit)):
        payload = rows.column("payload")[i].as_py()
        pages.append(
            Page(
                id=rows.column("id")[i].as_py(),
                source_uri=rows.column("source_uri")[i].as_py() or "",
                stage=rows.column("stage")[i].as_py() or "",
                size=len(payload) if payload else 0,
                has_image=payload is not None,
            )
        )
    return PageListing(dataset=table, pages=pages)


@router.get("/page", summary="One page's image bytes")
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
    must get that page or a 404 — never a different one.
    """
    ds = await _authorized_dataset(state, table, token, checker=checker, subject=subject, relation=READ_DATA, action="viewer.page.read")
    rows = await run_in_threadpool(read_aligned_table, ds, columns=[*_PAGE_COLUMNS, "payload"])
    ids = rows.column("id").to_pylist()
    if page_id not in ids:
        raise NotFoundError(f"no page with id {page_id} in {table!r}")
    payload = rows.column("payload")[ids.index(page_id)].as_py()
    if payload is None:
        # A registered page whose harvest produced no bytes. 404 with the reason, so the UI can say
        # "this page failed to harvest" instead of rendering a broken image icon.
        raise NotFoundError(f"page {page_id} has no image payload (harvest produced none)")
    return Response(
        content=payload,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=300"},
    )
