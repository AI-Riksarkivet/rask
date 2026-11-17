"""The namespace backend as every door reaches it: each add beneath a namespace and each drop of one under that namespace's
fence (ADR 0233, :mod:`lakehouse.namespace_fences`).

THE ONE SEAM. Every row the catalog puts into ``__manifest`` (a namespace's create, a table's create, declare, register or
rename in, an undrop's restore) and every namespace it drops goes through the backend connection a door was handed, so
the fence is taken here, by the connection, and no door can add or drop without it. An add holds the fence of the
namespace it adds into, a drop the fence of the namespace it drops: an add and its parent's drop then never run together,
and each reads the other's commit (measured on pylance 13.0.0, the module docstring of :mod:`lakehouse.namespace_fences`).

A row directly under the root, a warehouse's own namespace, has no parent namespace to fence: the warehouse delete holds the
warehouse's fence for its whole run instead (``endpoints/warehouses.py``).

Every other operation reaches the backend unchanged. Every method is BLOCKING IO, as the backend's are.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi.concurrency import run_in_threadpool
from lance_namespace import LanceNamespace

from catalog.core.namespace import NoWarehouse
from lakehouse.governed import fga
from lakehouse.namespace_fences import NamespaceFence, NamespaceWork
from lakehouse.storage_options import StorageOptions


#: The backend operations that add a row whose parent is the request's ``id`` less its last segment.
_ADDS = frozenset({"create_namespace", "create_table", "create_empty_table", "declare_table", "register_table", "create_materialized_view"})


class FencedNamespace:
    """A backend connection whose adds and drops hold the namespace fence their row meets (:func:`fenced`)."""

    def __init__(self, inner: LanceNamespace, *, control_root: str, storage_options: StorageOptions) -> None:
        self._inner = inner
        self._control_root = control_root
        self._storage_options = storage_options

    @property
    def inner(self) -> LanceNamespace:
        """The connection the fence wraps."""
        return self._inner

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — the backend's own methods, of its own types
        real = getattr(self._inner, name)
        if name in _ADDS:
            return lambda request, *args: self._under(_parent(request), NamespaceWork.ADD, real, request, *args)
        if name == "rename_table":
            # A rename adds the row at its destination: into ``new_namespace_id``, or the namespace it is in.
            return lambda request, *args: self._under(_rename_destination(request), NamespaceWork.ADD, real, request, *args)
        if name == "drop_namespace":
            return lambda request, *args: self._under(_segments(request), NamespaceWork.DROP, real, request, *args)
        return real

    def _under(self, namespace: list[str], work: NamespaceWork, call: Any, request: object, *args: object) -> Any:  # noqa: ANN401 — the backend's own types
        if not namespace:
            return call(request, *args)
        fence = NamespaceFence(control_root=self._control_root, storage_options=self._storage_options, name=fga.canonical_object_id(namespace))
        with fence.held(work):
            return call(request, *args)


def _segments(request: object) -> list[str]:
    return [str(segment) for segment in (getattr(request, "id", None) or [])]


def _parent(request: object) -> list[str]:
    return _segments(request)[:-1]


def _rename_destination(request: object) -> list[str]:
    into = getattr(request, "new_namespace_id", None)
    return [str(segment) for segment in into] if into is not None else _parent(request)


def fenced(ns: LanceNamespace, *, control_root: str, storage_options: StorageOptions) -> LanceNamespace:
    """``ns`` with its adds and drops under the namespace fence kept on ``control_root``; ``ns`` itself when it already is,
    or when it is the catalog of no warehouse, which holds nothing and adds nothing."""
    if isinstance(ns, FencedNamespace | NoWarehouse):
        return ns
    return cast(LanceNamespace, FencedNamespace(ns, control_root=control_root, storage_options=storage_options))


def unfenced(ns: LanceNamespace) -> LanceNamespace:
    """The connection ``ns`` wraps, or ``ns`` itself: what a lookup by the connection's identity finds."""
    return ns.inner if isinstance(ns, FencedNamespace) else ns


@asynccontextmanager
async def holding(control_root: str, storage_options: StorageOptions, segments: list[str], work: NamespaceWork) -> AsyncGenerator[None]:
    """The fence of the namespace ``segments`` names held for ``work`` while the block runs, renewed for as long as it runs:
    a door's own hold over its whole run (a drop's, an undrop's, a warehouse delete's), beside the ones its backend calls
    take.

    Raises:
        NamespaceFencedError: Work that excludes ``work`` holds it (409); the block did not run.
    """
    hold = NamespaceFence(control_root=control_root, storage_options=storage_options, name=fga.canonical_object_id(segments)).held(work)
    await run_in_threadpool(hold.__enter__)
    try:
        yield
    finally:
        await run_in_threadpool(hold.__exit__, None, None, None)
