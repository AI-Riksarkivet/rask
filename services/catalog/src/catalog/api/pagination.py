"""Keyset pagination shared by the catalog's own listings.

Lifted out of `endpoints/tables.py` when the model registry needed the identical cursor. It stays a
CATALOG module rather than moving to `service-kit`: the cursor is "the last name of the previous
page", which is only stateless and stable because these listings are `sorted(set(...))` name lists.
That is a property of this service's listings, not of pagination in general — `service_kit.pagination`
owns the OFFSET strategy, which has no such precondition.
"""

from __future__ import annotations


def _paginate(names: list[str], page_token: str | None, limit: int | None) -> tuple[list[str], str | None]:
    """Keyset pagination over an already-sorted, deduped name list.

    The cursor is the last name of the previous page — stateless, and stable across calls because
    the merged listing is ``sorted(set(...))``. A ``None`` next-token means the listing is complete;
    the native call is always made unpaginated, so no upstream cursor can ride through by accident.
    """
    if page_token:
        names = [name for name in names if name > page_token]
    if limit is None or limit < 0 or limit >= len(names):
        return names, None
    page = names[:limit]
    return page, (page[-1] if page else None)
