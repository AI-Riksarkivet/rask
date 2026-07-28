"""The storage registry — every object store the estate knows, and its ROLE (R28).

The object browser used to address buckets through a hardcoded two-value union
(``Literal["images-batch", "images-batch-alto"]``) mirrored BY HAND into the lakehouse zone's
TypeScript. Two copies of one fact, in two languages, kept in step by discipline alone — and
neither said what either bucket was FOR, so no view could group stores by medallion tier without a
third hand-written table.

The list itself lives in ``service_kit.schemas.storage`` because the VIEWER validates bucket names
against the same registry this endpoint serves to the UI; a service importing another service's
endpoint module to share a fact would be a layering violation.
"""

from __future__ import annotations

from fastapi import APIRouter

from service_kit.schemas.storage import (
    GOVERNED_TIERS,
    Store,
    StoreRegistry,
    registered_stores,
)


router = APIRouter(prefix="/v1", tags=["stores"])


@router.get("/stores", response_model=StoreRegistry, summary="Every registered object store")
def list_stores() -> StoreRegistry:
    """The whole registry in one response — it is estate config, and the tier view needs all of it."""
    return StoreRegistry(stores=registered_stores())


@router.get(
    "/stores/tiers",
    response_model=dict[str, list[Store]],
    summary="Stores grouped by medallion tier",
)
def stores_by_tier() -> dict[str, list[Store]]:
    """The tier -> store view, DERIVED from the registry rather than transcribed.

    Every governed tier appears even when empty: a bronze row with nothing in it says "no store
    backs bronze here", which is a fact worth showing. Roles outside the medallion (raw, derived,
    observability) are grouped under their own names — raw is deliberately not a tier (R23).
    """
    stores = registered_stores()
    grouped: dict[str, list[Store]] = {tier.value: [] for tier in GOVERNED_TIERS}
    for store in stores:
        grouped.setdefault(store.role.value, []).append(store)
    return grouped
