"""GET /v1/table pagination is real now — applied to the merged result, never the native call (#141).

The endpoint advertised ``page_token``/``limit`` and silently ignored both: the params rode into the
native root call whose result line 137 then OVERWROTE with the unbounded namespace walk. Worse than
inert — a native-level limit would truncate the ROOT listing before the walk and bound seeds merged
in, dropping tables from every page. The fix paginates the final sorted+deduped list keyset-style.
"""

from __future__ import annotations

from catalog.api.pagination import paginate


NAMES = ["a$one", "b$two", "c$three", "d$four", "e$five"]


def test_pages_walk_the_whole_list_without_overlap_or_loss() -> None:
    collected: list[str] = []
    token: str | None = None
    for _ in range(10):  # bounded so a broken cursor cannot hang the test
        page, token = paginate(list(NAMES), token, 2)
        collected.extend(page)
        if token is None:
            break
    assert collected == NAMES


def test_exact_fit_ends_pagination() -> None:
    """limit == remaining ⇒ complete, so the cursor must be None — a token here loops forever."""
    page, token = paginate(list(NAMES), None, 5)
    assert page == NAMES
    assert token is None


def test_stale_cursor_past_the_end_is_empty_and_done() -> None:
    assert paginate(list(NAMES), "zzz", 2) == ([], None)
