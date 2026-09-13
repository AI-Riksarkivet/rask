"""The ROOT namespace listing must name the warehouse-bound namespaces, not just the default root's.

The root id has no top segment to route by, so `dependencies.get_namespace` hands this route the
DEFAULT namespace (`dependencies.py:186-193` — the root parses to no segments and there is no warehouse
above the root). `_drain_namespaces` then asks that one backend, and a warehouse-enabled estate keeps
each tenant's namespaces in that tenant's OWN bucket — so a spec client listing from the root discovers
nothing warehouse-bound.

THE ESTATE ALREADY PROVED THE CONSEQUENCE TWICE. `GET /v1/table` had the identical blind spot and was
measured live at "2 of 9" until it seeded the walk from the bindings registry (`tables.py:185-191`), and
`maintenance/reconcile._top_level_namespaces_across` exists because "the scan read exactly ONE root —
the shared default bucket — while a warehouse-enabled estate puts each tenant's namespaces in that
tenant's OWN bucket". This is the third instance of that defect, on the route a spec client actually
uses to discover the estate.

THE BINDINGS ARE THE ANSWER AT THE ROOT, and that is simpler than draining each warehouse. The root's
children ARE top-level namespaces, and a binding record names exactly one — so the registry already
holds the missing names and no per-warehouse connection is needed. It is also the CORRECT source rather
than merely the cheap one: `get_namespace` routes an id by its binding, so listing what the bindings
name keeps this route's answer and the routes that serve it in step. A namespace sitting physically in
a warehouse bucket with no binding is reachable by no id route, and advertising it would be a listing
the estate cannot serve.

The per-item `can_get_metadata` filter and the keyset cursor are deliberately left where they are: a
merged name is subject to exactly the same visibility rule as one the backend returned.
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
    registry_root = "s3://control/registry"

    def __init__(self, *, warehouses_enabled: bool = True, fga_enabled: bool = False) -> None:
        self.warehouses_enabled = warehouses_enabled
        self.fga_enabled = fga_enabled

    def storage_options(self) -> dict[str, str]:
        return {}


async def _list(
    *,
    id: str,
    backend: list[str],
    bindings: list[dict[str, str]],
    reads: list[str] | None = None,
    warehouses_enabled: bool = True,
    allowed: list[str] | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> ListNamespacesResponse:
    """Drive the route with the backend and the bindings registry both faked.

    Faked at `native.call` rather than by handing in a pre-seeded namespace object, for the reason the
    sibling authz suite gives: a backend that seeded itself would let an unseeded route pass.
    """
    monkeypatch.setattr(ns_ep.native, "call", lambda _ns, _op, _req: ListNamespacesResponse(namespaces=list(backend)))

    def _fake_list_bindings(registry_root: str, _options: dict[str, str]) -> list[dict[str, str]]:
        if reads is not None:
            reads.append(registry_root)
        return list(bindings)

    monkeypatch.setattr(ns_ep.warehouses, "list_bindings", _fake_list_bindings)

    fga_enabled = allowed is not None
    if fga_enabled:

        async def _fake_list_objects(_client: object, *, user: str, relation: str, object_type: str) -> ns_ep.fga.ObjectListing:
            del user, relation, object_type
            return ns_ep.fga.ObjectListing(objects=list(allowed or []), truncated=False)

        monkeypatch.setattr(ns_ep.fga, "list_objects", _fake_list_objects)

    return await ns_ep.list_namespaces(
        id=id,
        ns=MagicMock(),
        settings=cast(Settings, _Settings(warehouses_enabled=warehouses_enabled, fga_enabled=fga_enabled)),
        token=(MagicMock(sub="bob") if fga_enabled else None),
        client=(MagicMock() if fga_enabled else None),
    )


_BOUND: list[dict[str, str]] = [{"top_ns": "acme", "warehouse_id": "acme-wh"}, {"top_ns": "bind86", "warehouse_id": "bind86-wh"}]


@pytest.mark.asyncio
async def test_the_root_listing_names_warehouse_bound_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE. Without the seed a spec client discovers only the shared default root's namespaces."""
    response = await _list(id="$", backend=["shared"], bindings=_BOUND, monkeypatch=monkeypatch)

    assert response.namespaces == ["acme", "bind86", "shared"]


@pytest.mark.asyncio
async def test_a_namespace_reachable_both_ways_is_named_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binding and the default backend can hand the route the same name — never a duplicate row."""
    response = await _list(id="$", backend=["acme", "shared"], bindings=_BOUND, monkeypatch=monkeypatch)

    assert response.namespaces == ["acme", "bind86", "shared"]
    assert response.namespaces.count("acme") == 1


@pytest.mark.asyncio
async def test_a_CHILD_listing_is_not_seeded_with_top_level_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seed belongs to the ROOT alone: a binding names a TOP-level namespace, so merging it into a
    child listing would invent `acme` as a child of some unrelated namespace."""
    reads: list[str] = []
    response = await _list(id="other", backend=["inner"], bindings=_BOUND, reads=reads, monkeypatch=monkeypatch)

    assert response.namespaces == ["inner"]
    assert reads == [], "a child listing must not pay a registry read it cannot use"


@pytest.mark.asyncio
async def test_with_warehouses_off_the_root_listing_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-bucket deployment keeps its existing answer and never reads the registry."""
    reads: list[str] = []
    response = await _list(id="$", backend=["shared"], bindings=_BOUND, reads=reads, warehouses_enabled=False, monkeypatch=monkeypatch)

    assert response.namespaces == ["shared"]
    assert reads == []


@pytest.mark.asyncio
async def test_a_binding_with_no_top_ns_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry record is object-store JSON written by another service — a malformed one must not
    put an empty name into a listing a client will try to resolve."""
    response = await _list(
        id="$",
        backend=["shared"],
        bindings=[{"warehouse_id": "orphan-wh"}, {"top_ns": "", "warehouse_id": "blank-wh"}, {"top_ns": "acme"}],
        monkeypatch=monkeypatch,
    )

    assert response.namespaces == ["acme", "shared"]


@pytest.mark.asyncio
async def test_a_seeded_name_is_still_subject_to_the_visibility_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seed must not become a way around the per-item `can_get_metadata` check — a bound namespace
    the caller cannot see is as disclosive as a sibling one, which is what that filter exists for."""
    response = await _list(
        id="$",
        backend=["shared"],
        bindings=_BOUND,
        allowed=["namespace:acme", "namespace:shared"],
        monkeypatch=monkeypatch,
    )

    assert response.namespaces == ["acme", "shared"], "bind86 is bound but invisible to this caller"


@pytest.mark.asyncio
async def test_an_unreadable_registry_does_not_blank_the_root_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry is object storage and can fail. Degrading to the default root's answer is the same
    tolerance `GET /v1/table` applies per seed — a listing narrower than the truth, never a 500."""
    monkeypatch.setattr(ns_ep.native, "call", lambda _ns, _op, _req: ListNamespacesResponse(namespaces=["shared"]))

    def _boom(_root: str, _options: dict[str, str]) -> list[dict[str, str]]:
        raise OSError("registry bucket unreachable")

    monkeypatch.setattr(ns_ep.warehouses, "list_bindings", _boom)

    response = await ns_ep.list_namespaces(
        id="$",
        ns=MagicMock(),
        settings=cast(Settings, _Settings()),
        token=None,
        client=None,
    )

    assert response.namespaces == ["shared"]


def test_the_merge_is_sorted_and_deduped() -> None:
    """The route's cursor is keyset over this list, so its ORDER is part of the contract: an unsorted
    merge would make `page_token` skip or repeat rows."""
    merged: Any = ns_ep._merge_bound_top_namespaces(["shared", "acme"], _BOUND)

    assert merged == ["acme", "bind86", "shared"]
