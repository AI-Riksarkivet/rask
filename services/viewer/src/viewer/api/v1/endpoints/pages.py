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

import lance
from fastapi import APIRouter, Query, Response
from pydantic import BaseModel
from service_kit.exceptions import NotFoundError
from service_kit.lakehouse.blobs import read_aligned_table
from service_kit.media.deps import StateDep
from typing import Annotated

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
    dataset: str
    pages: list[Page]


def _open(state: StateDep, dataset: str) -> lance.LanceDataset:
    """Open a bronze dataset by URI, or 404 naming it.

    Addressed by URI rather than through the media registry: a bronze page dataset is a LAKEHOUSE
    artefact the cascade wrote, not a registered media dataset with a descriptor, so there is no id
    to resolve. Credentials still come from the service's own settings — the caller supplies a
    location, never a credential.
    """
    try:
        return lance.dataset(dataset, storage_options=state.settings.storage_options)
    except Exception as exc:  # noqa: BLE001 — surface the cause, never a bare 500
        raise NotFoundError(f"no readable dataset at {dataset!r}: {exc}") from exc


@router.get("/pages", response_model=PageListing, summary="Pages in a bronze page dataset")
def list_pages(
    state: StateDep,
    dataset: Annotated[str, Query(description="Bronze page dataset URI (s3://…).")],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> PageListing:
    """Page metadata, in dataset order.

    Note what is NOT here: the image bytes. A listing that inlined them would move hundreds of
    megabytes to render a contact sheet; the browser fetches each page's bytes from ``/api/page``,
    which the ``<img>`` tag streams on demand.
    """
    ds = _open(state, dataset)
    table = read_aligned_table(ds, columns=[*_PAGE_COLUMNS, "payload"])
    pages: list[Page] = []
    for i in range(min(table.num_rows, limit)):
        payload = table.column("payload")[i].as_py()
        pages.append(
            Page(
                id=table.column("id")[i].as_py(),
                source_uri=table.column("source_uri")[i].as_py() or "",
                stage=table.column("stage")[i].as_py() or "",
                size=len(payload) if payload else 0,
                has_image=payload is not None,
            )
        )
    return PageListing(dataset=dataset, pages=pages)


@router.get("/page", summary="One page's image bytes")
def get_page(
    state: StateDep,
    dataset: Annotated[str, Query(description="Bronze page dataset URI (s3://…).")],
    page_id: Annotated[int, Query(alias="id", ge=0, description="The page's `id` column value.")],
) -> Response:
    """The raw image bytes for one page.

    Selected by the ``id`` COLUMN, never by row position: positional indexing into a blob read is
    the misattribution bug this module exists to avoid, and a caller holding an id from the listing
    must get that page or a 404 — never a different one.
    """
    ds = _open(state, dataset)
    table = read_aligned_table(ds, columns=[*_PAGE_COLUMNS, "payload"])
    ids = table.column("id").to_pylist()
    if page_id not in ids:
        raise NotFoundError(f"no page with id {page_id} in {dataset!r}")
    payload = table.column("payload")[ids.index(page_id)].as_py()
    if payload is None:
        # A registered page whose harvest produced no bytes. 404 with the reason, so the UI can say
        # "this page failed to harvest" instead of rendering a broken image icon.
        raise NotFoundError(f"page {page_id} has no image payload (harvest produced none)")
    return Response(
        content=payload,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=300"},
    )
