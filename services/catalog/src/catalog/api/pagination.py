"""Keyset pagination shared by the catalog's own listings.

It is a CATALOG module rather than a library one: the cursor is "the last name of the previous
page", which is only stateless and stable because these listings are `sorted(set(...))` name lists.
That is a property of this service's listings, not of pagination in general: an offset strategy has no such
precondition.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Query
from lance_namespace import InvalidInputError
from pydantic import AfterValidator


#: The most rows one page of a spec listing answers. The spec's `PageLimit` names no maximum and a server that cannot
#: return every item in one response hands back a `page_token` (`lance_docs/ns_catalog/spec.yaml` PageToken), so a
#: larger `limit` is answered this many rows and a token: the cap costs a caller a second call, never a refusal.
MAX_LIST_LIMIT: Final = 1000


def page_limit(limit: int | None) -> int | None:
    """A spec listing's `limit`, clamped to `MAX_LIST_LIMIT`; zero and below are refused 400 (code 13).

    `PageLimit` is "an inclusive upper bound of the number of results", so zero asks for an empty page, and the keyset
    cursor names the last row a page served: a page serving none has no token to continue from but the caller's own, and
    on a first request an absent token reads as the end of the listing. A door whose `limit` rides in its spec body
    (`ListTableIndicesRequest`) reads it through this, as `ListLimit` reads the query's.
    """
    if limit is None:
        return None
    if limit < 1:
        raise InvalidInputError(f"limit must be at least 1, got {limit}")
    return min(limit, MAX_LIST_LIMIT)


#: A spec listing's query `limit`, read through `page_limit`; `ge=1` states the refusal in the schema.
ListLimit = Annotated[
    int | None,
    Query(
        ge=1,
        description=(
            f"An inclusive upper bound of the number of results. A limit above {MAX_LIST_LIMIT} is answered at most "
            f"{MAX_LIST_LIMIT} results and a page_token that continues the listing. Zero and below are refused 400."
        ),
    ),
    AfterValidator(page_limit),
]


def paginate(names: list[str], page_token: str | None, limit: int | None) -> tuple[list[str], str | None]:
    """Keyset pagination over an already-sorted, deduped name list.

    The cursor is the last name of the previous page — stateless, and stable across calls because
    the merged listing is ``sorted(set(...))``. A ``None`` next-token means the listing is complete;
    the native call is always made unpaginated, so no upstream cursor can ride through by accident.
    """
    if page_token:
        names = [name for name in names if name > page_token]
    if limit is None or limit >= len(names):
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
    if limit is None or limit >= len(ordered):
        return ordered, None
    page = ordered[:limit]
    return page, (str(page[-1]) if page else None)
