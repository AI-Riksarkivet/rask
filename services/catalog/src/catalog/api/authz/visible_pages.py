"""A listing's page filtered by what the caller may see, judged batch by batch on the objects its candidates name."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Final, Literal

from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError

from catalog.api.authz.decisions import Authorization
from catalog.api.authz.objects import listed_object
from catalog.api.authz.rungs import LISTING_BATCH
from catalog.api.dependencies import Resolver
from catalog.core.pagination import paginate
from catalog.services import table_claims
from lakehouse.governed import fga_ids
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.records.location_claims import ClaimStore


__all__ = [
    "LISTING_RELATION",
    "authorized_page",
    "judged_page",
    "named_record_page",
]


#: What a listing asks of each name it would answer: the describer's rung ([[LH-570]]).
LISTING_RELATION: Final = "can_get_metadata"


async def authorized_page(
    authz: Authorization | None,
    resolver: Resolver,
    *,
    kind: Literal["table", "namespace"],
    candidates: Sequence[tuple[str, Sequence[str]]],
    page_token: str | None,
    limit: int | None,
) -> tuple[list[str], str | None]:
    """The page of ``candidates`` a listing answers its caller, and the page token after it ([[LH-223]]): with
    authorization on, those the caller holds :data:`LISTING_RELATION` on, each judged on the object its name resolves to
    ([[LH-524]]); with it off (``authz`` ``None``), every one.

    Each candidate is the name the listing answers (sorted) and the segments of the object it names.

    A listing authorizes its own candidates, batch by batch, on the objects their names resolve to, and fills its page
    from them. It never asks OpenFGA which objects the caller reaches across the estate: that answer stops at the
    server's ceiling (1,000 objects), so a caller reaching more than that elsewhere would see a small namespace listed
    short. The cursor is the last name served, as :func:`catalog.core.pagination.paginate` keeps it; a ``None`` token
    means every candidate was judged.

    A candidate that records no uuid holds no grant, so it is answered to no caller, and is left out rather than fail the
    listing for every caller ([[LH-534]]).
    """

    if authz is None:
        return paginate([name for name, _ in candidates], page_token, limit)

    async def resolved(batch: Sequence[tuple[str, Sequence[str]]]) -> list[str | None]:
        return list(await asyncio.gather(*(listed_object(resolver, kind, name, segments=list(segments)) for name, segments in batch)))

    page, token = await judged_page(
        authz,
        relation=LISTING_RELATION,
        candidates=[(name, (name, segments)) for name, segments in candidates],
        objects_of=resolved,
        page_token=page_token,
        limit=limit,
    )
    return [name for name, _ in page], token


async def judged_page[T](
    authz: Authorization | None,
    *,
    relation: str,
    candidates: Sequence[tuple[str, T]],
    objects_of: Callable[[Sequence[T]], Awaitable[Sequence[str | None]]],
    page_token: str | None,
    limit: int | None,
) -> tuple[list[T], str | None]:
    """The page of ``candidates`` the caller holds ``relation`` on, and the page token after it.

    Each candidate is its listing key (sorted, unique) and its item; ``objects_of`` answers the FGA object each item of a
    batch is judged on, ``None`` for one that holds no grant and is left out. The page fills batch by batch, the first
    batch as large as the page still needs and each after it twice the one before, up to ``LISTING_BATCH`` ([[LH-562]]):
    a page judges at most its own size beyond twice the candidates it serves or passes over, so a walk a page at a time
    judges little past each page rather than the rest of a ``LISTING_BATCH`` again on every page. The token
    is the last key served, a ``None`` token meaning every candidate was judged. ``authz`` is ``None`` only with FGA off
    (:func:`authorization`), when every decision allows, so the listing still pages on one path. With FGA on each page
    is audited once (``authz_listing``): the subject, the relation, the candidates judged and the items served.
    """
    remaining = [(key, item) for key, item in candidates if not page_token or key > page_token]
    page: list[T] = []
    token: str | None = None
    start, size, full = 0, min(LISTING_BATCH, limit or LISTING_BATCH), False
    while start < len(remaining) and not full:
        batch = remaining[start : start + size]
        objects = await objects_of([item for _, item in batch])
        present = sorted({obj for obj in objects if obj is not None})
        try:
            allowed = dict.fromkeys(present, True) if authz is None else await authz.batch_check(relation, present) if present else {}
        except ServiceUnavailableError:
            # `authz` is set whenever a check was sent. The page answers nothing, and the trail says why.
            audit(AuditAction.AUTHZ_LISTING, AuditOutcome.FAILURE, subject=authz.subject if authz else None, relation=relation, reason="authz_unavailable")
            raise
        for offset, ((key, item), obj) in enumerate(zip(batch, objects, strict=True)):
            if obj is None or not allowed.get(obj, False):
                continue
            page.append(item)
            if limit is not None and len(page) == limit:
                token = key if start + offset + 1 < len(remaining) else None
                full = True
                break
        start, size = start + len(batch), min(LISTING_BATCH, size * 2)
    if authz is not None:
        # One record a page, never one a candidate: what a listing left out is on the trail by its count, and the trail
        # names none of it, as the answer names none ([[LH-585]]).
        audit(AuditAction.AUTHZ_LISTING, AuditOutcome.SUCCESS, subject=authz.subject, relation=relation, judged=min(start, len(remaining)), served=len(page))
    return page, token


async def named_record_page[R](
    authz: Authorization | None,
    store: ClaimStore,
    *,
    relation: str,
    candidates: Sequence[tuple[str, R]],
    kept_by: Callable[[R], table_claims.TableRecord],
    page_token: str | None,
    limit: int | None,
) -> tuple[list[tuple[str, R]], str | None]:
    """The page of records kept for tables, each with the name its table holds now, that the caller holds ``relation`` on.

    Each candidate is its listing key (sorted, unique) and its record, and ``kept_by`` reads the location and uuid the
    record keeps. A record's table is named (:func:`table_claims.names_now`) and judged on its uuid only as the page
    fills (:func:`judged_page`), so a page costs a claim read and a check for about the records it serves or passes
    over, never one for every record held. A record whose table is gone names no table and is listed to nobody.
    """
    names: dict[str, str] = {}

    async def judged_on(batch: Sequence[tuple[str, R]]) -> list[str | None]:
        kept = [kept_by(record) for _, record in batch]
        now = await run_in_threadpool(table_claims.names_now, store, kept)
        names.update((key, name) for (key, _), name in zip(batch, now, strict=True) if name is not None)
        return [fga_ids.table_object(record.uuid) if name is not None else None for record, name in zip(kept, now, strict=True)]

    page, token = await judged_page(
        authz,
        relation=relation,
        candidates=[(key, (key, record)) for key, record in candidates],
        objects_of=judged_on,
        page_token=page_token,
        limit=limit,
    )
    return [(names[key], record) for key, record in page], token
