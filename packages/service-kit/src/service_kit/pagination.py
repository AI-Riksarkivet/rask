"""The estate's ONE offset-pagination vocabulary: params, a depth guard, and a page envelope.

Before this module, `grep -rn 'PaginationParams|Page\\[|build_page|get_pagination'` over `services/`
and `packages/` returned nothing: every paginated route had reinvented the wire shape. The measured
result was eight parameter vocabularies (`limit`+`offset`, `limit`+`after`, `limit`+`cursor`,
`page_size`+`continuation`, `page_token`+`limit`, `page`+`per_page`, `since`, bare `limit`) and seven
ceilings across seven services, so a frontend generating a typed client from the gateway's aggregated
OpenAPI saw four names for one concept.

SCOPED TO OFFSET ON PURPOSE. `pagination.md` says to pick the strategy per endpoint — offset for
bounded admin tables, cursor for feeds, keyset at depth — and this estate genuinely uses all three:
notifications pages an inbox by cursor, and the catalog's `page_token` is the Lance Namespace spec's
own wire contract, which must keep its shape. One model cannot serve those without lying about one of
them. So this ships the offset strategy, which is the one the estate has a route for; a `KeysetParams`
sibling belongs beside it the day a second cursor route wants to share one, not before.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, Field

from service_kit.exceptions import ValidationError


#: How far into a listing a caller may ask to start. `Query(ge=1)` on a page number bounds NOTHING —
#: `?page=1000000` was legal and every store had to seek past ten million rows to answer it. The
#: reference's own figure; a route that genuinely needs to go deeper wants keyset, not a bigger number.
MAX_OFFSET = 10_000


class PaginationParams(BaseModel):
    """Page and size, with the offset DERIVED rather than restated at each call site."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def guard_offset(params: PaginationParams) -> PaginationParams:
    """Refuse a page whose offset exceeds :data:`MAX_OFFSET`, naming the way forward.

    A bare refusal is a dead end: the caller asked a legitimate question and needs to know which door
    answers it, so the message names keyset rather than only stating the limit.
    """
    if params.offset > MAX_OFFSET:
        raise ValidationError(
            f"page {params.page} starts at offset {params.offset}, beyond the {MAX_OFFSET} limit — use a keyset/cursor listing for deeper pages"
        )
    return params


def get_pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, alias="per_page")] = 20,
) -> PaginationParams:
    """The guarded dependency: bounds the size in the SCHEMA and the depth at the door."""
    return guard_offset(PaginationParams(page=page, page_size=page_size))


PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]


class Page[Item](BaseModel):
    """The envelope, with the arithmetic done server-side.

    `pages`, `has_next` and `has_prev` are here because the reference's anti-pattern is a client
    computing them from incomplete data and drifting off-by-one from the server.
    """

    items: list[Item]
    total: int
    page: int
    page_size: int
    pages: int
    has_next: bool
    has_prev: bool


def build_page[Item](items: list[Item], total: int, params: PaginationParams) -> Page[Item]:
    pages = -(-total // params.page_size)  # ceil division
    return Page[Item](
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
        pages=pages,
        has_next=params.page < pages,
        has_prev=params.page > 1,
    )
