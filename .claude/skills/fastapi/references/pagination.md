# Pagination

Strategies for paginating list endpoints, with reusable dependencies and the indexes you need at the DB layer. All examples use **Pydantic models** (never `@dataclass`), **SQLModel + SQLAlchemy 2.0 async**, and **PEP 695** generics. See [`database.md`](database.md) for the engine / session setup.

## Contents

- Pick the right strategy
- `PaginationParams` — reusable dep
- Offset pagination — simple, with metadata
- Cursor pagination — opaque cursor for feeds
- Keyset pagination — fastest at depth
- Combined filter + sort + pagination dep
- Indexes you need
- Approximate counts + MAX_OFFSET guard
- Anti-patterns

## Pick the right strategy

| Strategy   | When                                                                  | Random access | Performance at page 1000 | Handles concurrent writes |
| ---------- | --------------------------------------------------------------------- | :-----------: | :----------------------: | :-----------------------: |
| **Offset** | Admin tables, small / bounded datasets, need page numbers.            | ✅            | 🟡 slow                  | 🟡 page may "shift"       |
| **Cursor** | Infinite scroll, social feeds, real-time data. No "jump to page 7".   | ❌            | ✅                       | ✅                        |
| **Keyset** | Largest datasets, consistent latency at any depth. Internal services. | ❌            | ✅ fastest               | ✅                        |

> **Default to offset.** Switch to cursor when the front-end is an infinite scroll. Switch to keyset when offset's deep-page latency shows up on a flame graph — not before.

## `PaginationParams` — reusable dep

One `BaseModel` + one factory dep, used across every list endpoint.

```python
# api/pagination.py
from typing import Annotated
from fastapi import Depends, Query
from pydantic import BaseModel, Field


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def get_pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, alias="limit")] = 20,
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


PaginationDep = Annotated[PaginationParams, Depends(get_pagination)]
```

`alias="limit"` lets callers use either `?page_size=20` or `?limit=20` — pick the one your front-end already sends, don't expose both publicly.

## Paginated response envelope (generic, PEP 695)

```python
# api/responses.py
from pydantic import BaseModel


class Page[Item](BaseModel):
    items: list[Item]
    total: int
    page: int
    page_size: int
    pages: int
    has_next: bool
    has_prev: bool


def build_page[Item](items: list[Item], total: int, p: PaginationParams) -> Page[Item]:
    pages = -(-total // p.page_size)  # ceil division
    return Page[Item](
        items=items, total=total,
        page=p.page, page_size=p.page_size, pages=pages,
        has_next=p.page < pages, has_prev=p.page > 1,
    )
```

## Offset pagination

```python
# api/articles.py
from sqlalchemy import func, select
from fastapi import APIRouter

from app.api.deps import SessionDep
from app.api.pagination import PaginationDep
from app.api.responses import Page, build_page
from app.models import Article, ArticlePublic

router = APIRouter(prefix="/articles", tags=["articles"])


@router.get("")
async def list_articles(
    session: SessionDep, p: PaginationDep,
) -> Page[ArticlePublic]:
    total = (await session.scalar(select(func.count()).select_from(Article))) or 0
    rows = (await session.scalars(
        select(Article)
        .order_by(Article.created_at.desc(), Article.id.desc())
        .offset(p.offset).limit(p.page_size)
    )).all()
    return build_page([ArticlePublic.model_validate(r) for r in rows], total, p)
```

**Always add a secondary sort key (`id`)** — two rows with the same `created_at` would otherwise reorder between pages.

## Cursor pagination

The cursor is an opaque base64 of the last item's `(created_at, id)`. Clients pass it back verbatim; they never inspect it.

```python
# api/cursor.py
import base64
import json
from datetime import datetime
from typing import Annotated
from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel


class CursorParams(BaseModel):
    limit: int = 20
    after_created_at: datetime | None = None
    after_id: int | None = None


def _decode(cursor: str) -> dict[str, object]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor))
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor") from e


def encode_cursor(*, created_at: datetime, id: int) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"ts": created_at.isoformat(), "id": id}).encode()
    ).decode()


def get_cursor(
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CursorParams:
    if cursor is None:
        return CursorParams(limit=limit)
    decoded = _decode(cursor)
    return CursorParams(
        limit=limit,
        after_created_at=datetime.fromisoformat(str(decoded["ts"])),
        after_id=int(decoded["id"]),       # type: ignore[arg-type]
    )


CursorDep = Annotated[CursorParams, Depends(get_cursor)]


class CursorPage[Item](BaseModel):
    items: list[Item]
    next_cursor: str | None
```

```python
from sqlalchemy import and_, or_, select


@router.get("/feed")
async def feed(session: SessionDep, c: CursorDep) -> CursorPage[ArticlePublic]:
    stmt = select(Article).order_by(Article.created_at.desc(), Article.id.desc())

    if c.after_created_at is not None and c.after_id is not None:
        stmt = stmt.where(
            or_(
                Article.created_at < c.after_created_at,
                and_(Article.created_at == c.after_created_at, Article.id < c.after_id),
            )
        )

    # Fetch limit+1 to detect "has more" without a second query.
    rows = (await session.scalars(stmt.limit(c.limit + 1))).all()
    has_more = len(rows) > c.limit
    rows = rows[: c.limit]

    next_cursor = encode_cursor(created_at=rows[-1].created_at, id=rows[-1].id) if has_more and rows else None
    return CursorPage[ArticlePublic](
        items=[ArticlePublic.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )
```

**Why opaque?** Clients can't infer "page 7" semantics or skip ahead. The cursor changes shape (add fields, change order keys) without breaking them — they treat it as a token.

## Keyset pagination

Same query shape as cursor, but the keyset lives in **public query params** — useful for internal services where you control both sides and the readability matters more than hiding the cursor format.

```python
class KeysetParams(BaseModel):
    limit: int = 20
    after_created_at: datetime | None = None
    after_id: int | None = None


def get_keyset(
    after_created_at: Annotated[datetime | None, Query()] = None,
    after_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KeysetParams:
    return KeysetParams(
        limit=limit, after_created_at=after_created_at, after_id=after_id,
    )
```

The handler is identical to the cursor version. The only difference is the wire format: clients pass `?after_created_at=…&after_id=…` directly instead of `?cursor=…`.

## Combined filter + sort + pagination dep

When the same endpoint needs filtering AND sorting AND pagination, compose three small deps — don't write one mega-dep.

```python
from enum import StrEnum
from typing import Annotated
from fastapi import Depends, Query
from pydantic import BaseModel


class SortField(StrEnum):
    created_at = "created_at"
    title = "title"
    views = "views"


class SortOrder(StrEnum):
    asc = "asc"
    desc = "desc"


class SortParams(BaseModel):
    field: SortField = SortField.created_at
    order: SortOrder = SortOrder.desc


def get_sort(
    sort_by: SortField = SortField.created_at,
    order: SortOrder = SortOrder.desc,
) -> SortParams:
    return SortParams(field=sort_by, order=order)


class ArticleFilters(BaseModel):
    author: str | None = None
    search: str | None = None
    min_views: int | None = None


def get_filters(
    author: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    min_views: Annotated[int | None, Query(ge=0)] = None,
) -> ArticleFilters:
    return ArticleFilters(author=author, search=search, min_views=min_views)


SortDep = Annotated[SortParams, Depends(get_sort)]
FilterDep = Annotated[ArticleFilters, Depends(get_filters)]


@router.get("/search")
async def search(
    session: SessionDep, f: FilterDep, s: SortDep, p: PaginationDep,
) -> Page[ArticlePublic]:
    stmt = select(Article)
    if f.author:    stmt = stmt.where(Article.author == f.author)
    if f.search:    stmt = stmt.where(Article.title.ilike(f"%{f.search}%"))
    if f.min_views is not None:
        stmt = stmt.where(Article.views >= f.min_views)

    total = (await session.scalar(select(func.count()).select_from(stmt.subquery()))) or 0

    col = {SortField.created_at: Article.created_at,
           SortField.title:      Article.title,
           SortField.views:      Article.views}[s.field]
    stmt = stmt.order_by(col.desc() if s.order is SortOrder.desc else col.asc(),
                         Article.id.desc())

    rows = (await session.scalars(stmt.offset(p.offset).limit(p.page_size))).all()
    return build_page([ArticlePublic.model_validate(r) for r in rows], total, p)
```

## Indexes you need

Pagination performance is 90% indexes. Without them, every strategy degrades to a full scan.

```sql
-- Offset / keyset / cursor — covers the secondary-sort tiebreaker too.
CREATE INDEX idx_articles_created_at_id ON articles (created_at DESC, id DESC);

-- If sorting on alternate columns, one index per (sort_col, id) pair you actually serve.
CREATE INDEX idx_articles_views_id ON articles (views DESC, id DESC);

-- Filter columns used in WHERE before pagination.
CREATE INDEX idx_articles_author ON articles (author);
```

**One index per sort dimension you actually serve.** Don't pre-create indexes for sort fields you don't expose — they cost write throughput.

## Approximate counts + MAX_OFFSET guard

For tables over ~1M rows, `SELECT COUNT(*)` becomes the slowest part of an offset response. Two cheap mitigations:

```python
# 1. Approximate count from Postgres catalog — milliseconds instead of seconds.
from sqlalchemy import text


async def approximate_count(session: AsyncSession, table: str) -> int:
    row = await session.scalar(
        text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t").bindparams(t=table)
    )
    return int(row or 0)
```

Use the approximate count in the response when you only need rough totals (UI says "about 2.3M results"). Fall back to a real `COUNT(*)` only on filters that drop most rows.

```python
# 2. Forbid deep pages so adversarial queries can't DoS the DB.
MAX_OFFSET = 10_000


def get_pagination_guarded(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PaginationParams:
    if (page - 1) * page_size > MAX_OFFSET:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Page too deep — use cursor pagination beyond offset {MAX_OFFSET}.",
        )
    return PaginationParams(page=page, page_size=page_size)
```

## Anti-patterns

| Mistake                                                                | Why                                                                                      | Fix                                                                                          |
| ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Ordering only by `created_at` for cursor / keyset                      | Two rows at the same timestamp can reorder between pages.                                | Add `id DESC` as a deterministic tiebreaker on every paginated query.                        |
| Returning the cursor as a JSON object (`{"id": 42, "ts": …}`)          | Clients start parsing it and your cursor schema is now public API.                       | Opaque base64. Treat the string as a token.                                                  |
| `SELECT COUNT(*)` on every list call against a 10M-row table           | The count alone takes seconds; pagination still completes but the response is slow.      | Use `pg_class.reltuples` for "about" counts; do exact counts only when the filter narrows it.|
| Offsetting into the millions                                           | Postgres reads + discards every row up to the offset. Latency grows linearly.            | Cap with `MAX_OFFSET`; surface a "use cursor" message in the 400.                            |
| Page-size with no upper bound (`page_size: int = 20`)                  | `?page_size=999999` exhausts memory and bandwidth.                                       | `Field(le=100)` (or whatever your real maximum is).                                          |
| `total_pages` calculated client-side from incomplete data              | Drift between client and server math, off-by-one bugs.                                   | Return `pages`, `has_next`, `has_prev` in the envelope; clients don't compute them.          |
| Sync `db.query(Article).offset(...).limit(...).all()` in async routes  | Blocks the event loop.                                                                   | SQLAlchemy 2.0 `await session.scalars(select(...).offset(...).limit(...))`.                  |
| `@dataclass` for filter / sort / pagination params                     | No validation, no JSON schema, no FastAPI / Pydantic integration. Project-wide ban.       | `class PaginationParams(BaseModel): ...` — see SKILL.md house style.                          |
