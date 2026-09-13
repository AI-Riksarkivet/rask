"""`version/list` took a `page_token`, forwarded it, and served the same rows regardless.

MEASURED AGAINST THE REAL BACKEND 2026-09-13, driven rather than read. A `dir` namespace over a table
with seven versions:

    no limit    -> [1, 2, 3, 4, 5, 6, 7]   page_token None
    limit=3     -> [1, 2, 3]               page_token None
    limit=2, page_token="3" -> [1, 2]      page_token None

So the backend truncates at `limit` and hands back NO continuation, and the token it is given changes
nothing. The catalog's own surface is spec-correct — `lance_docs/namespace.md` § ListTableVersions puts
`page_token` and `limit` on the query string and `ListTableVersionsResponse` carries `page_token` — and
the handler forwarded both faithfully. The lie was one layer down, and forwarding it made the door
repeat it.

WHAT THAT COSTS A SPEC CLIENT, and it is worse than looping. A caller asking for `limit=3` on a
seven-version table is served three rows and told the listing is COMPLETE, because a `None` token means
"that was everything". It does not page forever; it stops early and silently, having seen three of
seven. Its sibling `GET /v1/model` already calls this out — "a slice that reports neither a total nor a
continuation is truncation wearing pagination's clothes".

THE FIX IS THE HELPER THIS SERVICE ALREADY SHIPS, one layer up from the backend that cannot page.
`catalog.api.pagination` states the pattern in its own docstring: "the native call is always made
unpaginated, so no upstream cursor can ride through by accident". The version list is the same shape
with an integer key and an order that `descending` may flip, so it gets a sibling keyset rather than a
second cursor implementation that can drift from the first.
"""

from __future__ import annotations

import pathlib
import tempfile
from types import SimpleNamespace
from typing import Any, cast

import lance
import lance_namespace as ln
import pyarrow as pa
import pytest

from catalog.api import pagination
from catalog.api.v1.endpoints import versions as versions_ep


def _table_with(version_count: int) -> tuple[Any, str]:
    """A real `dir` namespace over a table with ``version_count`` versions."""
    root = tempfile.mkdtemp()
    uri = str(pathlib.Path(root) / "t.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)
    for value in range(2, version_count + 1):
        lance.write_dataset(pa.table({"id": [value]}), uri, mode="append")
    return ln.connect("dir", {"root": root}), "t"


def _list(ns: Any, table: str, **kwargs: Any) -> Any:
    settings = cast(Any, SimpleNamespace(delimiter="$"))
    return versions_ep.list_table_versions(table, ns, settings, **kwargs)


def test_the_versions_helper_hands_back_its_last_version() -> None:
    """A truncated page must carry the cursor that resumes it, or the caller cannot continue."""
    page, token = pagination.paginate_versions([1, 2, 3, 4, 5], None, 2, descending=False)
    assert page == [1, 2]
    assert token == "2", "the cursor is the last version SERVED — anything else overlaps or skips"


def test_the_versions_cursor_resumes_strictly_after_it() -> None:
    """Strictly after, not at: a cursor that re-serves its own row duplicates a version per page."""
    page, token = pagination.paginate_versions([1, 2, 3, 4, 5], "2", 2, descending=False)
    assert page == [3, 4]
    assert token == "4"


def test_a_complete_versions_page_reports_no_continuation() -> None:
    """`None` must mean "that was everything" — it is what a client stops on."""
    page, token = pagination.paginate_versions([1, 2, 3], None, 10, descending=False)
    assert (page, token) == ([1, 2, 3], None)


def test_descending_pages_downward_and_its_cursor_follows() -> None:
    """`descending` is a spec query param, so the cursor has to mean "after" in the ORDER SERVED."""
    page, token = pagination.paginate_versions([1, 2, 3, 4, 5], None, 2, descending=True)
    assert page == [5, 4]
    assert token == "4"
    nxt, _ = pagination.paginate_versions([1, 2, 3, 4, 5], "4", 2, descending=True)
    assert nxt == [3, 2], "a descending cursor that filtered upward would serve the same rows again"


def test_paging_twice_over_a_REAL_table_gets_different_rows() -> None:
    """THE GATE, and the test this row asked for. Driven against the `dir` backend, not a double."""
    ns, table = _table_with(7)

    first = _list(ns, table, limit=3)
    served = [v.version for v in (first.versions or [])]
    assert served == [1, 2, 3]
    assert first.page_token is not None, "a listing with four more versions behind it reported itself complete"

    second = _list(ns, table, limit=3, page_token=first.page_token)
    assert [v.version for v in (second.versions or [])] == [4, 5, 6], "the second page repeated the first"

    third = _list(ns, table, limit=3, page_token=second.page_token)
    assert [v.version for v in (third.versions or [])] == [7]
    assert third.page_token is None, "the last page must say so, or the caller pages forever"


def test_an_unbounded_listing_is_complete_and_uncursored() -> None:
    """The common case must not grow a cursor it does not need."""
    ns, table = _table_with(4)

    answer = _list(ns, table)

    assert [v.version for v in (answer.versions or [])] == [1, 2, 3, 4]
    assert answer.page_token is None


def test_the_backend_is_asked_UNPAGINATED(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cursor is this layer's, and the upstream one must not ride through underneath it.

    `pagination`'s own docstring states the rule; asserted here because forwarding `limit` would
    reintroduce exactly the silent truncation this file exists to end — the backend would cut the list
    before the helper ever saw the rows it is meant to page.
    """
    seen: list[Any] = []
    ns, table = _table_with(5)
    real = versions_ep.native.call

    def _spy(namespace: Any, op: str, req: Any) -> Any:
        seen.append(req)
        return real(namespace, op, req)

    monkeypatch.setattr(versions_ep.native, "call", _spy)
    _list(ns, table, limit=2, page_token="1")

    assert len(seen) == 1
    assert seen[0].limit is None, "a `limit` sent downstream truncates before this layer can page"
    assert seen[0].page_token is None, "an upstream cursor riding through makes the two cursors disagree"
