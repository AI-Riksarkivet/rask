"""Enumerating "which objects does anyone hold anything on" must page, and must never answer short.

THE QUESTION IS NOT "WHAT MAY A SUBJECT REACH". `list_objects` answers that. This one asks whether a
resource is governed AT ALL — because a table carrying no tuple cannot be read, maintained, dropped or
re-created by anyone including its creator, and an auditor that cannot ask it cannot tell governed data
from residue.

WHY IT IS NOT ONE CALL PER OBJECT, which is the shape everyone finds first. OpenFGA refuses a Read
whose ``tuple_key`` carries an empty object id — "the object type field is required and both the object
id and user cannot be empty", reproduced against the live store — so filtering by type alone is not
available and the obvious implementation costs N calls. Omitting ``tuple_key`` ENTIRELY is a different
call that pages the whole store: measured 2026-09-11 against the live estate, 51 pages and 5,027 tuples
in 0.1 s, 1,232 governed tables.

AND A SHORT ANSWER IS WORSE THAN AN ERROR HERE, which is what the last test pins. This set's whole job
is to say what is NOT in it, so a partial read does not degrade the answer — it inverts it, marking
every object it did not reach as ungoverned. Stopping on a non-advancing cursor therefore raises rather
than returning what it has.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from service_kit.governed import fga


class _Store:
    """A Read that pages: each call returns one page and the cursor for the next."""

    def __init__(self, pages: list[list[str]], *, never_ends: bool = False) -> None:
        self._pages = pages
        self._never_ends = never_ends
        self.calls = 0

    async def read(self, _body: object, options: dict[str, Any] | None = None) -> object:
        index = int(str((options or {}).get("continuation_token") or 0))
        self.calls += 1
        page = self._pages[index] if index < len(self._pages) else []
        more = self._never_ends or index + 1 < len(self._pages)
        return SimpleNamespace(
            tuples=[SimpleNamespace(key=SimpleNamespace(object=obj)) for obj in page],
            continuation_token=str(index + 1) if more else "",
        )


@pytest.mark.asyncio
async def test_every_page_is_read_and_the_type_prefix_is_stripped() -> None:
    """THE GATE. A caller holds bare ids (`acme-bronze$events`), so that is what comes back."""
    store = _Store([["table:a$one", "namespace:acme"], ["table:b$two"], ["team:eng", "table:c$three"]])

    governed = await fga.governed_objects(cast(Any, store), object_type="table")

    assert governed == {"a$one", "b$two", "c$three"}, "every page, tables only, no `table:` prefix"
    assert store.calls == 3, "a reader that stopped at the first page would report most of the estate ungoverned"


@pytest.mark.asyncio
async def test_another_type_is_answered_from_the_same_pass() -> None:
    """The filter is the caller's, not the server's — there is no type-only Read to ask for."""
    store = _Store([["table:a$one", "namespace:acme"], ["namespace:beta"]])

    assert await fga.governed_objects(cast(Any, store), object_type="namespace") == {"acme", "beta"}


@pytest.mark.asyncio
async def test_a_cursor_that_never_empties_raises_rather_than_answering_short() -> None:
    """The property that keeps this safe to invert.

    A caller asks this set what is ABSENT from it. Returning a partial read would mark every object the
    pass never reached as ungoverned — turning a server fault into a false verdict about the whole
    estate, silently. The bound exists so that cannot happen quietly.
    """
    store = _Store([["table:a$one"]], never_ends=True)

    with pytest.raises(Exception, match="did not terminate"):
        await fga.governed_objects(cast(Any, store), object_type="table", retry_attempts=1)

    assert store.calls == fga.READ_MAX_PAGES, "it must actually exhaust the bound rather than give up early"
