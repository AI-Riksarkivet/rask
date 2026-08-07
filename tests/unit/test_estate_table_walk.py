"""The estate-wide table walk must SEE namespaces the native root enumeration cannot.

Measured live 2026-08-07: the shipped ``dir`` backend answers per-namespace ``list_tables`` fine,
but ``list_namespaces`` at the ROOT yields nothing — a namespace is a ``__manifest`` row. The walk
therefore returned only the two flat root rows while media/silver/alpha/beta held registered
tables: `GET /v1/table` said 2 of 9, and the lakehouse Tables registry hid every registered table.
The fix seeds the walk with the BINDINGS registry's ``top_ns`` names (the sanctioned tolerant
enumerator). These tests fail on the pre-fix code.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from catalog.api.v1.endpoints.tables import _collect_tables
from lance_namespace import LanceNamespace


class _DirLikeNamespace:
    """The measured live shape: per-namespace listing works, root children come back EMPTY."""

    def __init__(self, tree: dict[str, list[str]]) -> None:
        self._tree = tree  # namespace name -> its tables

    def list_namespaces(self, req: Any) -> Any:  # noqa: ARG002
        del req
        return SimpleNamespace(namespaces=[])

    def list_tables(self, req: Any) -> Any:
        return SimpleNamespace(tables=list(self._tree.get("$".join(req.id), [])))


def _ns(tree: dict[str, list[str]]) -> LanceNamespace:
    return cast(LanceNamespace, _DirLikeNamespace(tree))


_TREE = {"media": ["chunks", "documents"], "silver": ["vasa-publish", "consensus-live"]}
_ROOT = ["bronze$pages", "transcripts_v2$annotations"]


def test_without_the_seed_the_walk_is_blind() -> None:
    """The pre-fix behaviour, pinned so the seed's value is measurable: root rows only."""
    assert _collect_tables(_ns(_TREE), "$", _ROOT, True) == sorted(_ROOT)


def test_bound_namespaces_are_walked_and_their_tables_listed() -> None:
    got = _collect_tables(_ns(_TREE), "$", _ROOT, True, extra_roots=["media", "silver"])
    assert "media$chunks" in got
    assert "media$documents" in got
    assert "silver$vasa-publish" in got
    assert set(_ROOT) <= set(got)


def test_a_namespace_reachable_both_ways_is_reported_once() -> None:
    """Bindings + native children can hand the walk the same namespace — never a duplicate row."""

    class _WithChildren(_DirLikeNamespace):
        def list_namespaces(self, req: Any) -> Any:
            return SimpleNamespace(namespaces=["media"] if not req.id else [])

    got = _collect_tables(cast(LanceNamespace, _WithChildren(_TREE)), "$", [], True, extra_roots=["media"])
    assert got.count("media$chunks") == 1


def test_one_unreadable_namespace_does_not_blank_the_registry() -> None:
    class _OneBad(_DirLikeNamespace):
        def list_tables(self, req: Any) -> Any:
            if req.id == ["silver"]:
                raise RuntimeError("boom")
            return super().list_tables(req)

    got = _collect_tables(cast(LanceNamespace, _OneBad(_TREE)), "$", _ROOT, True, extra_roots=["media", "silver"])
    assert "media$chunks" in got and set(_ROOT) <= set(got)
