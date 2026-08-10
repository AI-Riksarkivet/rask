"""`GET /v1/namespace/{id}/table/list` must not enumerate tables the caller cannot read.

THE REGRESSION. `14a84022` (C1, upward visibility) redefined `can_get_metadata` on a container as
``reader or can_get_metadata from child``. That is right for what it was for — without it, a grantee
holding one table could not resolve the breadcrumb to their own data, and every list above it 403'd —
but `authorize` resolves this route's `list` action to exactly that relation. So the route began
OPENING for anyone holding `reader` on any single table beneath the namespace, while its body was an
unfiltered `native.call(ns, "list_tables", …)`. One grant bought the whole sibling listing.

Measured against both compiled models before this suite was written: with the pre-commit model
(`can_get_metadata: reader`) the check was **false**; with the shipped one, **true**. So this is a
regression introduced by that commit, not a pre-existing hole — and `tables.py`'s `list_all_tables`
had filtered on `can_read_data` all along, which is what makes the gap an inconsistency rather than a
policy.

WHAT IS PINNED HERE, and the split matters: the ROUTE still opens (the breadcrumb must resolve — that
is C1's whole point), and the CONTENTS are checked per object. A test that asserted a 403 would be
asserting C1 away.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from catalog.api.v1.endpoints import namespaces as ns_ep
from catalog.core.config import Settings
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ListTablesResponse


class _Settings:
    """Only what the route reads. A real `Settings` drags the whole config surface in for four fields."""

    delimiter = "$"

    def __init__(self, *, fga_enabled: bool) -> None:
        self.fga_enabled = fga_enabled


async def _list(
    *,
    backend_tables: list[str],
    allowed: list[str] | None,
    fga_enabled: bool = True,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> ListTablesResponse:
    """Drive the route function with the native backend and OpenFGA both faked.

    The native call is faked at `native.call` rather than by handing in a fake namespace object,
    because the point of the test is what the ROUTE does with the backend's answer — a backend that
    returned a pre-filtered list would let an unfiltered route pass.
    """
    monkeypatch.setattr(ns_ep.native, "call", lambda _ns, _op, _req: ListTablesResponse(tables=list(backend_tables)))

    async def _fake_list_objects(_client: object, *, user: str, relation: str, object_type: str) -> list[str]:
        assert relation == "can_read_data", f"the listing must be filtered on data access, not on {relation!r}"
        assert object_type == "table"
        assert user == "ivan"
        return list(allowed or [])

    monkeypatch.setattr(ns_ep.fga, "list_objects", _fake_list_objects)

    token = None if not fga_enabled else MagicMock(sub="ivan")
    return await ns_ep.list_tables(
        id="acme-gold",
        ns=MagicMock(),
        # `cast` and not a `type: ignore`: the fake still has to satisfy the signature at the
        # call site, so a route that starts reading a fifth setting fails here loudly.
        settings=cast(Settings, _Settings(fga_enabled=fga_enabled)),
        token=token,
        client=MagicMock(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_a_single_table_grantee_cannot_enumerate_its_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE REGRESSION, in the shape C1 made reachable: ivan holds `reader` on one table, which opens
    the route; the sibling's NAME must not come back with it."""
    response = await _list(
        backend_tables=["catalog", "sealed_wills"],
        allowed=["table:acme-gold$catalog"],
        monkeypatch=monkeypatch,
    )

    assert response.tables == ["catalog"], "a sibling table the caller cannot read was named in the listing"


@pytest.mark.asyncio
async def test_the_route_still_answers_rather_than_refusing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half C1 exists for. A grantee whose only table is here must still get a 200 with their own
    table — narrowing the route to a 403 would put back the broken breadcrumb C1 fixed."""
    response = await _list(backend_tables=["catalog"], allowed=["table:acme-gold$catalog"], monkeypatch=monkeypatch)

    assert response.tables == ["catalog"]


@pytest.mark.asyncio
async def test_a_caller_who_can_read_nothing_here_gets_an_empty_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty, not "everything". A grant on some OTHER namespace's table also opens this route under
    C1 only if it is beneath this container — but `list_objects` is estate-wide, so the filter must
    match on the FULL path, never on the bare name."""
    response = await _list(
        backend_tables=["catalog", "sealed_wills"],
        allowed=["table:other-ns$catalog"],  # same bare name, different namespace
        monkeypatch=monkeypatch,
    )

    assert response.tables == []


@pytest.mark.asyncio
async def test_with_fga_off_nothing_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """An auth-off stack has no tuple store to ask; refusing or emptying the listing there would break
    every local run. Order still normalizes, because pagination depends on it."""
    response = await _list(backend_tables=["sealed_wills", "catalog"], allowed=None, fga_enabled=False, monkeypatch=monkeypatch)

    assert response.tables == ["catalog", "sealed_wills"]


@pytest.mark.asyncio
async def test_a_page_counts_only_tables_the_caller_can_see(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason pagination moved off the native call. Filtering AFTER a backend `limit` would return
    a page of 1 for `limit=2` and then hand out a cursor that skips what the filter removed — pages
    that silently drop tables the caller CAN read."""
    response = await _list(
        backend_tables=["a", "b", "c", "d"],
        allowed=["table:acme-gold$a", "table:acme-gold$c", "table:acme-gold$d"],
        monkeypatch=monkeypatch,
        limit=2,
    )

    assert response.tables == ["a", "c"], "the page was cut before the filter"
    assert response.page_token == "c", "the cursor must be the last VISIBLE name, so the next page resumes correctly"

    nxt = await _list(
        backend_tables=["a", "b", "c", "d"],
        allowed=["table:acme-gold$a", "table:acme-gold$c", "table:acme-gold$d"],
        monkeypatch=monkeypatch,
        limit=2,
        page_token="c",
    )

    assert nxt.tables == ["d"]
    assert nxt.page_token is None


@pytest.mark.asyncio
async def test_the_backend_is_never_asked_to_paginate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned on the REQUEST, because this is the half a result-shape assertion cannot see: if a
    native `limit` ever comes back, the filter starts operating on a pre-truncated list again and every
    assertion above still passes for the pages that happen to be full."""
    seen: dict[str, Any] = {}

    def _capture(_ns: object, _op: str, req: Any) -> ListTablesResponse:
        seen["page_token"], seen["limit"] = req.page_token, req.limit
        return ListTablesResponse(tables=["catalog"])

    monkeypatch.setattr(ns_ep.native, "call", _capture)

    async def _all(_client: object, **_kw: Any) -> list[str]:
        return ["table:acme-gold$catalog"]

    monkeypatch.setattr(ns_ep.fga, "list_objects", _all)

    await ns_ep.list_tables(
        id="acme-gold",
        ns=MagicMock(),
        settings=cast(Settings, _Settings(fga_enabled=True)),
        token=MagicMock(sub="ivan"),
        client=MagicMock(),
        page_token="catalog",
        limit=1,
    )

    assert seen == {"page_token": None, "limit": None}


@pytest.mark.asyncio
async def test_the_native_call_is_not_made_on_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`native.call` is blocking IO. The route is async now, so it has to hand that to a threadpool or
    it stalls the whole worker for the length of a backend listing."""
    threads: list[bool] = []
    real = run_in_threadpool

    async def _watched(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        threads.append(True)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(ns_ep, "run_in_threadpool", _watched)

    await _list(backend_tables=["catalog"], allowed=["table:acme-gold$catalog"], monkeypatch=monkeypatch)

    assert threads, "the blocking native listing ran directly on the event loop"
