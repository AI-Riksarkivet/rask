"""Keyset pagination shared by the catalog's own listings.

Lifted out of `endpoints/tables.py` when the model registry needed the identical cursor. It stays a
CATALOG module rather than moving to `service-kit`: the cursor is "the last name of the previous
page", which is only stateless and stable because these listings are `sorted(set(...))` name lists.
That is a property of this service's listings, not of pagination in general — `service_kit.pagination`
owns the OFFSET strategy, which has no such precondition.
"""

from __future__ import annotations


def paginate(names: list[str], page_token: str | None, limit: int | None) -> tuple[list[str], str | None]:
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


def paginate_versions(versions: list[int], page_token: str | None, limit: int | None, *, descending: bool) -> tuple[list[int], str | None]:
    """Keyset pagination over a table's version numbers, in the order they are SERVED.

    The sibling above keys on a name; a version list keys on an integer and its order is the caller's
    (`descending` is a spec query parameter on `ListTableVersions`). Sharing one function would have to
    choose a comparison for both, so this is a sibling rather than a generalisation — and the cursor
    still means the same thing: the last row served, resumed strictly after.

    THE COMPARISON FOLLOWS THE ORDER, which is the half a name cursor never has to think about. Serving
    newest-first and then filtering ``version > cursor`` would hand back the rows already served, so the
    predicate flips with ``descending``.

    ``page_token`` is a string on the wire (the spec's field) and a version number in meaning; a token
    that is not one is the caller's error and is refused by the door rather than silently ignored, which
    is the failure this pagination exists to end.
    """
    ordered = sorted(versions, reverse=descending)
    if page_token:
        cursor = int(page_token)
        ordered = [version for version in ordered if (version < cursor if descending else version > cursor)]
    if limit is None or limit < 0 or limit >= len(ordered):
        return ordered, None
    page = ordered[:limit]
    return page, (str(page[-1]) if page else None)
