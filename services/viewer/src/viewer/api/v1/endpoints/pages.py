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

See ``docs/architecture/lance-blob-v2-findings.md`` for the measurements behind that.
"""

from __future__ import annotations

from typing import Annotated

import httpx
import lance
from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from service_kit.exceptions import NotFoundError
from service_kit.lakehouse.blobs import read_aligned_table
from service_kit.media.deps import StateDep


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


def _resolve(state: StateDep, table: str) -> str:
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
    try:
        with httpx.Client(base_url=base, timeout=30.0) as http:
            r = http.post(f"/v1/table/{table}/describe", json={})
    except httpx.RequestError as exc:
        raise NotFoundError(f"catalog unreachable while resolving {table!r}: {exc}") from exc
    if r.status_code >= 400:
        raise NotFoundError(f"catalog does not know table {table!r} (HTTP {r.status_code})")
    location = r.json().get("location")
    if not location:
        raise NotFoundError(f"catalog returned no location for {table!r}")
    return str(location)


def _open(state: StateDep, table: str) -> lance.LanceDataset:
    """Open the dataset a catalog table points at, or 404 naming the table."""
    location = _resolve(state, table)
    try:
        return lance.dataset(location, storage_options=state.settings.storage_options)
    except Exception as exc:  # noqa: BLE001 — surface the cause, never a bare 500
        raise NotFoundError(f"table {table!r} resolves to {location!r}, which is unreadable: {exc}") from exc


@router.get("/pages", response_model=PageListing, summary="Pages in a bronze page dataset")
def list_pages(
    state: StateDep,
    table: Annotated[str, Query(description="Catalog table id, e.g. bronze$pages.")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> PageListing:
    """Page metadata, in dataset order.

    Note what is NOT here: the image bytes. A listing that inlined them would move hundreds of
    megabytes to render a contact sheet; the browser fetches each page's bytes from ``/api/page``,
    which the ``<img>`` tag streams on demand.
    """
    ds = _open(state, table)
    rows = read_aligned_table(ds, columns=[*_PAGE_COLUMNS, "payload"])
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
def get_page(
    state: StateDep,
    table: Annotated[str, Query(description="Catalog table id, e.g. bronze$pages.")],
    page_id: Annotated[int, Query(alias="id", ge=0, description="The page's `id` column value.")],
) -> Response:
    """The raw image bytes for one page.

    Selected by the ``id`` COLUMN, never by row position: positional indexing into a blob read is
    the misattribution bug this module exists to avoid, and a caller holding an id from the listing
    must get that page or a 404 — never a different one.
    """
    ds = _open(state, table)
    rows = read_aligned_table(ds, columns=[*_PAGE_COLUMNS, "payload"])
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
