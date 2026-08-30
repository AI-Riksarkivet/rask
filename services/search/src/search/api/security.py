"""Who is asking, and which corpora they may search.

The search service shipped with no authorization at all (open_python-audit `X6`): the estate's
`RASK_OIDC_*`/`RASK_FGA_*` env reached it and bound to nothing, while its two siblings — viewer and
annotator — both carry the full seam. With the viewer's corpus routes gated, this was the last
unguarded door on the `/api/explorer` edge, and the one that accepts a raw SQL `where` predicate
ANDed into every query (`VS-13`).

**The relation is `can_read_data`.** A search returns row PAYLOAD, so it is the rung `pages.py` uses
for bytes, not the `can_get_metadata` the corpus LISTING uses. Gating a payload read on the metadata
rung would let someone who may only know a corpus exists read what is inside it.

**The object is the row table of the SEARCHABLE TABLE THIS REQUEST NAMED** — `?table=`, resolved
through `Declared.search_named`, which is the same call every handler makes to decide which rows to
read. Reading `declared.search` instead (the first declared entry) let the gate authorize table A
while the handler served table B; a corpus may declare several since `feat(descriptor): a corpus can
declare SEVERAL searchable tables`, so the default is a table like any other, not THE table.

A `?table=` the corpus does not declare names no FGA object and is DENIED, exactly as a corpus that
declares no search block at all is: guessing one — or borrowing the default's — would authorize
against something other than what is about to be read.

Everything mechanical (bearer → verified subject, the three-outcome checker) comes from
`service_kit.governed.deps`, shared with viewer and annotator rather than copied out of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Query, Request

from search.api.dependencies import StateDep
from search.core.config import SearchSettings, get_search_settings
from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import FAILURE, audit
from service_kit.governed.deps import FgaChecker, make_auth_deps
from service_kit.media.authz import corpus_object
from service_kit.media.state import AppState


if TYPE_CHECKING:
    from service_kit.lancekit.registry import DatasetHandle


SettingsDep = Annotated[SearchSettings, Depends(get_search_settings)]

_deps = make_auth_deps(SettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: A search returns row payload — the BYTES rung, not the listing's metadata rung.
READ_DATA = "can_read_data"


def _row_table(handle: DatasetHandle, table: str | None) -> str | None:
    """The row table this request will ACTUALLY read, or None when there is no such table to name.

    `search_named(None)` is the corpus default and `search_named("<undeclared>")` is `None` — never a
    fallback to the default, which is the whole point: a gate that silently checks a different table
    than the handler reads authorizes the wrong thing, and nothing in the response would show it.
    """
    search = handle.descriptor.declared.search_named(table)
    return search.row_table if search is not None else None


async def may_search(
    state: AppState,
    dataset_id: str | None,
    *,
    table: str | None,
    subject: str,
    checker: FgaChecker,
    settings: SearchSettings,
) -> bool:
    """May this caller search this corpus's `table`? `True` when FGA is off (dev is unchanged).

    `table` is the DECLARED searchable-table name the request carries (`?table=`), `None` for the
    corpus default — the same value the handler passes to `resolve_target`, so the object checked
    here and the rows read there cannot diverge.

    Resolution is threadpooled by the caller — opening a cold registry entry is Lance/S3 under a
    lock, and this runs on the event loop.
    """
    if not settings.fga_enabled:
        return True
    from starlette.concurrency import run_in_threadpool

    from service_kit.media.state import dataset_handle

    handle = await run_in_threadpool(dataset_handle, state, dataset_id)
    row_table = _row_table(handle, table)
    if row_table is None:
        return False
    return await checker(user=subject, relation=READ_DATA, obj=corpus_object(settings, handle.id, row_table))


async def require_search(
    state: AppState,
    dataset_id: str | None,
    *,
    table: str | None,
    subject: str,
    checker: FgaChecker,
    settings: SearchSettings,
) -> None:
    """Refuse a search of ONE named corpus table the caller may not read.

    A single-corpus search asks a specific question, so it gets a specific answer — unlike the
    fan-out, which is filtered (`datasets.list_datasets`: "the honest answer to 'what can I search'
    is a shorter list, not a 403").
    """
    if await may_search(state, dataset_id, table=table, subject=subject, checker=checker, settings=settings):
        return
    audit("search.corpus.read", FAILURE, subject=subject, resource=dataset_id or "<default>", relation=READ_DATA)
    raise ForbiddenError(f"{subject} lacks {READ_DATA} on corpus {dataset_id or '<default>'}")


async def authorized_corpora(
    request: Request,
    state: StateDep,
    subject: CurrentSubject,
    checker: CheckerDep,
    settings: SettingsDep,
    corpus: Annotated[list[str] | None, Query(description="Fan out across these corpora, fused by RRF")] = None,
) -> list[str] | None:
    """The corpora this caller may search, or ``None`` for the single-corpus case (already refused).

    A DEPENDENCY, not an inline check, for the reason the viewer's gate is one: `search_get` is a
    sync `def` with a blocking Lance body (correctly threadpooled by FastAPI), and a sync body cannot
    `await` the checker. A dependency runs ON the loop before the handler is dispatched, so the
    handler keeps its shape and the authorization still happens first.

    It returns the FILTERED fan-out list because the two questions have different honest answers.
    Asked to search several corpora, the answer is the subset the caller may read — the estate's
    established rule (`datasets.list_datasets`: "a caller with access to two of five corpora gets
    two, because the honest answer to 'what can I search' is a shorter list, not a 403"). Asked for
    ONE named corpus and not entitled to it, the caller gets 403: that question has no shorter answer.
    """
    # Read off the raw query string rather than declared as a parameter, exactly as `dataset` is:
    # `SearchSpec` binds itself from `request.query_params` (see `_spec_from_query`), and the gate
    # must read the SAME string the handler will, not a second binding that could drift from it.
    table = request.query_params.get("table")
    if corpus:
        allowed: list[str] = []
        for dataset_id in corpus:
            # The fan-out searches the same DECLARED table name in every corpus (`_fused_search`
            # passes one `spec.table` to all of them), so each corpus is checked on ITS row table
            # under that name — and a corpus that does not declare it is dropped, which is the same
            # answer `_fused_search` reaches by skipping it.
            if await may_search(state, dataset_id, table=table, subject=subject, checker=checker, settings=settings):
                allowed.append(dataset_id)
            else:
                audit("search.corpus.read", FAILURE, subject=subject, resource=dataset_id, relation=READ_DATA)
        return allowed
    await require_search(state, request.query_params.get("dataset"), table=table, subject=subject, checker=checker, settings=settings)
    return None


#: The fan-out subset (or ``None`` for a single-corpus search, already authorized).
AuthorizedCorpora = Annotated["list[str] | None", Depends(authorized_corpora)]
