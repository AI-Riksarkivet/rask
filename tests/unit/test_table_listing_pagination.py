"""GET /v1/table pagination is real now — applied to the merged result, never the native call (#141).

The endpoint advertised ``page_token``/``limit`` and silently ignored both: the params rode into the
native root call whose result line 137 then OVERWROTE with the unbounded namespace walk. Worse than
inert — a native-level limit would truncate the ROOT listing before the walk and bound seeds merged
in, dropping tables from every page. The fix paginates the final sorted+deduped list keyset-style.
"""

from __future__ import annotations

import inspect

from catalog.api.pagination import paginate
from catalog.api.v1.endpoints.tables import list_all_tables


NAMES = ["a$one", "b$two", "c$three", "d$four", "e$five"]


def test_no_limit_returns_everything_and_no_cursor() -> None:
    assert paginate(list(NAMES), None, None) == (NAMES, None)


def test_pages_walk_the_whole_list_without_overlap_or_loss() -> None:
    collected: list[str] = []
    token: str | None = None
    for _ in range(10):  # bounded so a broken cursor cannot hang the test
        page, token = paginate(list(NAMES), token, 2)
        collected.extend(page)
        if token is None:
            break
    assert collected == NAMES


def test_final_page_has_no_cursor() -> None:
    page, token = paginate(list(NAMES), "d$four", 2)
    assert page == ["e$five"]
    assert token is None


def test_exact_fit_ends_pagination() -> None:
    """limit == remaining ⇒ complete, so the cursor must be None — a token here loops forever."""
    page, token = paginate(list(NAMES), None, 5)
    assert page == NAMES
    assert token is None


def test_stale_cursor_past_the_end_is_empty_and_done() -> None:
    assert paginate(list(NAMES), "zzz", 2) == ([], None)


def test_native_call_is_unpaginated_and_pagination_runs_after_fga() -> None:
    """Source-order proof: the native request must not carry the caller's limit (it would truncate
    only the root ingredient), and paginate must run AFTER the FGA filter so pages count only
    tables the caller can see."""
    src = inspect.getsource(list_all_tables)
    assert "ListTablesRequest(id=[], include_declared=include_declared)" in src
    assert src.index("can_read_data") < src.index("paginate(")
