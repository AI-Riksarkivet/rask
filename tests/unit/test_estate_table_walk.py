"""The estate table listing walks every warehouse, and one that cannot be read costs only its own tables.

`GET /v1/table` carries no ``{id}`` to route by, so `list_all_tables` walks each warehouse a name may begin with (the
estate's own and every registered one) through that warehouse's own directory catalog, from the warehouse's row down
([[LH-593]]). A namespace that cannot be listed is left out of its warehouse's walk, and a warehouse that cannot be
reached is left out of the listing: neither blanks the registry.

DRIVEN THROUGH `list_all_tables`, the route handler, over real warehouse records: the property is the merge of the
per-warehouse walks, which a test of one walk alone cannot see.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from lance_namespace import LanceNamespace, ListNamespacesRequest, ListNamespacesResponse, ListTablesRequest, ListTablesResponse

from catalog.api.pagination import MAX_LIST_LIMIT
from catalog.api.v1.endpoints import tables as t_ep
from catalog.core.config import Settings
from catalog.services import warehouses
from lakehouse.storage_options import StorageOptions
from tests.unit.named_resolver import NamedResolver


class _DirLike:
    """One warehouse's directory catalog: ``tables`` and ``children`` keyed by the parent's full name, and ``unreadable``
    a namespace whose ``list_tables`` raises."""

    def __init__(self, tables: dict[str, list[str]], children: dict[str, list[str]], *, unreadable: str = "") -> None:
        self._tables = tables
        self._children = children
        self._unreadable = unreadable

    def list_namespaces(self, req: ListNamespacesRequest) -> ListNamespacesResponse:
        return ListNamespacesResponse(namespaces=list(self._children.get("$".join(req.id or []), [])))

    def list_tables(self, req: ListTablesRequest) -> ListTablesResponse:
        name = "$".join(req.id or [])
        if name == self._unreadable:
            raise RuntimeError(f"manifest unreadable: {name}")
        return ListTablesResponse(tables=list(self._tables.get(name, [])))


_ESTATE = _DirLike({"lance_catalog$bronze": ["pages"]}, {"lance_catalog": ["bronze"]})
_MEDIA = {"media$bronze": ["chunks", "documents"], "media$silver": ["consensus"]}
_MEDIA_CHILDREN = {"media": ["bronze", "silver"]}


def _settings(tmp_path: object) -> Settings:
    return Settings.model_validate({"root": f"file://{tmp_path}", "registry_root": f"file://{tmp_path}", "s3_access_key_id": "x", "s3_secret_access_key": "x"})


def _list(monkeypatch: pytest.MonkeyPatch, tmp_path: object, catalogs: dict[str, _DirLike | Exception]) -> list[str]:
    """`GET /v1/table` over the estate's catalog and a registry holding a record for every other warehouse in ``catalogs``.

    Each warehouse resolves to its own catalog, or raises for one that cannot be reached, through the seam the route
    uses, so the merge under test is the production one.
    """
    settings = _settings(tmp_path)
    for warehouse_id in catalogs:
        if warehouse_id != settings.estate_warehouse:
            record = {"id": warehouse_id, "bucket": f"{warehouse_id}-bkt", "root_uri": f"s3://{warehouse_id}-bkt", "project": "acme", "created_at": "t"}
            warehouses.put_warehouse(settings.registry_root, StorageOptions(), record)

    async def _resolve(request: Request, settings: Settings, warehouse_id: str) -> LanceNamespace:
        del request, settings
        target = catalogs[warehouse_id]
        if isinstance(target, Exception):
            raise target
        return cast(LanceNamespace, target)

    monkeypatch.setattr(t_ep, "namespace_for_warehouse", _resolve)
    response = asyncio.run(
        t_ep.list_all_tables(
            request=cast(Request, SimpleNamespace()),
            settings=settings,
            so=StorageOptions(),
            token=None,
            client=None,
            resolver=NamedResolver(),
            limit=MAX_LIST_LIMIT,
        )
    )
    return list(response.tables or [])


def test_one_unreadable_namespace_does_not_blank_the_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A namespace whose ``list_tables`` raises costs that namespace, not its warehouse's other tables."""
    got = _list(monkeypatch, tmp_path, {"lance_catalog": _ESTATE, "media": _DirLike(_MEDIA, _MEDIA_CHILDREN, unreadable="media$silver")})

    assert got == ["lance_catalog$bronze$pages", "media$bronze$chunks", "media$bronze$documents"]


def test_one_unreachable_warehouse_does_not_blank_the_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A warehouse that cannot be reached costs that warehouse's tables, not every other tenant's."""
    got = _list(
        monkeypatch,
        tmp_path,
        {"lance_catalog": _ESTATE, "media": _DirLike(_MEDIA, _MEDIA_CHILDREN), "vault": RuntimeError("warehouse deactivated")},
    )

    assert got == ["lance_catalog$bronze$pages", "media$bronze$chunks", "media$bronze$documents", "media$silver$consensus"]
