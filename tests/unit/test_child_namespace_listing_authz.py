"""`GET /v1/namespace/{id}/list` must not enumerate CHILD NAMESPACES the caller cannot see.

THE SAME DEFECT AS ITS SIBLING, ONE ROUTE OVER. `test_namespace_listing_authz.py` closed this for the
TABLE listing under a namespace; the CHILD-NAMESPACE listing in the same module kept the unfiltered
`native.call(...)` and was never revisited.

It is reachable for exactly the reason that one was. C1 redefined `can_get_metadata` on a container as
``reader or can_get_metadata from child``, and `authorize` resolves this route's `list` action to that
relation — so holding `reader` on ONE deep leaf table opens the route on every ancestor. Proven against
the live OpenFGA store 2026-08-31, with bob holding `reader` on a single leaf table:

    can_read_data     table:zz_top$mine$t     -> True
    can_get_metadata  namespace:zz_top        -> True    <- opens GET /v1/namespace/zz_top/list
    can_get_metadata  namespace:zz_top$secret -> False   <- and this NAME came back anyway

A namespace name is not a harmless header: it is the estate's own stated position that "a corpus LIST
is itself sensitive: it names data someone may not know exists," and a namespace name leaks the
existence and the naming of a tenant's data organisation.

WHAT IS PINNED, and the split is the same one: the ROUTE still opens — narrowing it to 403 would put
back the broken breadcrumb C1 exists to fix — and each ITEM is checked on the object itself.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from lance_namespace import ListNamespacesResponse

from catalog.api.v1.endpoints import namespaces as ns_ep
from catalog.core.config import Settings


class _Settings:
    delimiter = "$"

    def __init__(self, *, fga_enabled: bool) -> None:
        self.fga_enabled = fga_enabled


async def _list(
    *,
    backend: list[str],
    allowed: list[str] | None,
    fga_enabled: bool = True,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> ListNamespacesResponse:
    """Drive the route with the backend and OpenFGA both faked.

    Faked at `native.call` rather than by handing in a pre-filtered namespace object, for the reason
    the sibling suite gives: a backend that filtered for the route would let an unfiltered route pass.
    """
    monkeypatch.setattr(ns_ep.native, "call", lambda _ns, _op, _req: ListNamespacesResponse(namespaces=list(backend)))

    async def _fake_list_objects(_client: object, *, user: str, relation: str, object_type: str) -> ns_ep.fga.ObjectListing:
        assert relation == "can_get_metadata", f"a namespace listing is filtered on metadata visibility, not {relation!r}"
        assert object_type == "namespace"
        assert user == "bob"
        return ns_ep.fga.ObjectListing(objects=list(allowed or []), truncated=False)

    monkeypatch.setattr(ns_ep.fga, "list_objects", _fake_list_objects)

    return await ns_ep.list_namespaces(
        id="zz_top",
        ns=MagicMock(),
        settings=cast(Settings, _Settings(fga_enabled=fga_enabled)),
        token=(MagicMock(sub="bob") if fga_enabled else None),
        client=MagicMock(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_a_single_leaf_grantee_cannot_enumerate_sibling_NAMESPACES(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live-proven disclosure: one deep grant opened the route and named every sibling."""
    response = await _list(
        backend=["mine", "secret"],
        allowed=["namespace:zz_top", "namespace:zz_top$mine"],
        monkeypatch=monkeypatch,
    )

    assert response.namespaces == ["mine"], "a sibling namespace the caller cannot see was named in the listing"


@pytest.mark.asyncio
async def test_the_route_still_answers_rather_than_refusing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half C1 exists for — the breadcrumb to the caller's own data must still resolve."""
    response = await _list(backend=["mine"], allowed=["namespace:zz_top$mine"], monkeypatch=monkeypatch)

    assert response.namespaces == ["mine"]


@pytest.mark.asyncio
async def test_a_caller_who_can_see_nothing_here_gets_an_EMPTY_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty, not 403: the route's gate and its contents answer different questions."""
    response = await _list(backend=["mine", "secret"], allowed=[], monkeypatch=monkeypatch)

    assert response.namespaces == []


@pytest.mark.asyncio
async def test_with_fga_off_nothing_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev and single-tenant deployments run the checker permissive by construction."""
    response = await _list(backend=["mine", "secret"], allowed=None, fga_enabled=False, monkeypatch=monkeypatch)

    assert response.namespaces == ["mine", "secret"]
