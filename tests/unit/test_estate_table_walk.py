"""The estate table listing must SEE bound namespaces the native root enumeration cannot.

Measured live 2026-08-07: the shipped ``dir`` backend answers per-namespace ``list_tables`` fine, but
``list_namespaces`` at the ROOT yields nothing — a namespace is a ``__manifest`` row — so `GET /v1/table`
said 2 of 9 while media/silver/alpha/beta held registered tables. What makes the listing complete is the
BINDINGS registry: `list_all_tables` lists each bound ``top_ns`` through its own warehouse-rooted
connection and merges that with the root rows and the native walk.

DRIVEN THROUGH `list_all_tables`, the route handler, because every property here is a property of the
MERGE of those three ingredients. A test of the walk alone cannot see a merge that drops the bound
seeds, and that is the half the live defect was in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from lance_namespace import LanceNamespace, ListNamespacesRequest, ListNamespacesResponse, ListTablesRequest, ListTablesResponse

from catalog.api.v1.endpoints import tables as t_ep
from catalog.core.config import Settings


class _DirLike:
    """One connection in the measured live shape: per-namespace ``list_tables`` answers, and the root's
    ``list_namespaces`` names only ``children`` — nothing at all by default, as the ``dir`` backend does."""

    def __init__(self, tree: dict[str, list[str]], *, root_rows: tuple[str, ...] = (), children: tuple[str, ...] = (), unreadable: str = "") -> None:
        self._tree = tree
        self._root_rows = root_rows
        self._children = children
        self._unreadable = unreadable

    def list_all_tables(self, req: ListTablesRequest) -> ListTablesResponse:
        del req
        return ListTablesResponse(tables=list(self._root_rows))

    def list_namespaces(self, req: ListNamespacesRequest) -> ListNamespacesResponse:
        return ListNamespacesResponse(namespaces=[] if req.id else list(self._children))

    def list_tables(self, req: ListTablesRequest) -> ListTablesResponse:
        name = "$".join(req.id or [])
        if name == self._unreadable:
            raise RuntimeError(f"manifest unreadable: {name}")
        return ListTablesResponse(tables=list(self._tree.get(name, [])))


_ROOT_ROWS = ("bronze$pages", "transcripts_v2$annotations")
_WAREHOUSE = {"media": ["chunks", "documents"], "silver": ["vasa-publish", "consensus-live"]}


def _settings(tmp_path: object) -> Settings:
    return Settings.model_validate({"root": f"file://{tmp_path}", "registry_root": f"file://{tmp_path}", "s3_access_key_id": "x", "s3_secret_access_key": "x"})


def _list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    *,
    root: _DirLike,
    bound: dict[str, _DirLike | Exception],
) -> list[str]:
    """`GET /v1/table` over a root connection and a bindings registry naming ``bound``.

    Each bound ``top_ns`` resolves to its own connection — or raises, for a warehouse that cannot be
    reached — through the same seam the route uses, so the merge under test is the production one.
    """

    async def _resolve(request: Request, settings: Settings, top_ns: str) -> LanceNamespace:
        del request, settings
        target = bound[top_ns]
        if isinstance(target, Exception):
            raise target
        return cast(LanceNamespace, target)

    monkeypatch.setattr(t_ep.warehouses, "list_bindings", lambda *_a, **_kw: [{"top_ns": top} for top in bound])
    monkeypatch.setattr(t_ep, "namespace_for_top_ns", _resolve)
    response = asyncio.run(
        t_ep.list_all_tables(
            request=cast(Request, SimpleNamespace()),
            ns=cast(LanceNamespace, root),
            settings=_settings(tmp_path),
            token=None,
            client=None,
        )
    )
    return list(response.tables or [])


def test_a_bound_namespace_is_listed_although_the_root_cannot_name_it(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """The live defect: a blind root plus two bound warehouses must list every table in all three."""
    warehouse = _DirLike(_WAREHOUSE)

    got = _list(monkeypatch, tmp_path, root=_DirLike({}, root_rows=_ROOT_ROWS), bound={"media": warehouse, "silver": warehouse})

    assert got == sorted([*_ROOT_ROWS, "media$chunks", "media$documents", "silver$consensus-live", "silver$vasa-publish"])


def test_a_namespace_reachable_both_ways_is_reported_once(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A namespace the root CAN name and that is also bound reaches the merge twice — never two rows."""
    root = _DirLike(_WAREHOUSE, children=("media",))

    got = _list(monkeypatch, tmp_path, root=root, bound={"media": _DirLike(_WAREHOUSE)})

    assert got.count("media$chunks") == 1, got


def test_one_unreadable_native_namespace_does_not_blank_the_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A native child whose ``list_tables`` raises costs that namespace, not the registry page."""
    root = _DirLike(_WAREHOUSE, root_rows=_ROOT_ROWS, children=("media", "silver"), unreadable="silver")

    got = _list(monkeypatch, tmp_path, root=root, bound={})

    assert got == sorted([*_ROOT_ROWS, "media$chunks", "media$documents"])


def test_one_unreachable_bound_warehouse_does_not_blank_the_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A bound seed that cannot be resolved costs that warehouse's tables, not every other tenant's."""
    got = _list(
        monkeypatch,
        tmp_path,
        root=_DirLike({}, root_rows=_ROOT_ROWS),
        bound={"media": _DirLike(_WAREHOUSE), "silver": RuntimeError("warehouse deactivated")},
    )

    assert got == sorted([*_ROOT_ROWS, "media$chunks", "media$documents"])
