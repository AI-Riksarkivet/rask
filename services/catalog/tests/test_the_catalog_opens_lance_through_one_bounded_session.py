"""The catalog's Lance opens share one bounded session, so the cache exists at all.

[[LH-096]]. Every lakehouse pod runs a 512 Mi limit (128 Mi request). A bare `lance.dataset(uri)` mints
Lance's DEFAULT ceilings — 1 GiB metadata, 6 GiB index — and discards them WITH the handle, so a
process that opens per request never caches anything and carries caps that dwarf the container it runs
in. The failure is not theoretical: `rask-maintenance` was OOMKilled (exit 137) on 2026-09-10, which is
why maintenance already threads `shared_lance_session()` and the caps are clamped to the cgroup.

THE CATALOG IS THE ONE THAT OPENS PER REQUEST, which is what makes it the next one to convert:
`namespace.py`, `dataplane.py`, `publication.py`, `vending.py`, `models.py` and the credentials door all
open on the request path, so the mint-and-discard happens per call rather than per sweep.

A SESSION IS NOT A HANDLE CACHE, and the distinction is the whole reason this is safe to do. Caching a
DATASET HANDLE pins a version and needs a freshness contract; a `lance.Session`'s keys carry
`(uri, version, etag)`, so a compaction writes NEW keys and a new version is never served stale. An old
version whose data files were reclaimed can be, which is why evidence reads open on
`lakehouse.lancekit.lance_session.fresh_session`. Both are recorded in `lakehouse.lancekit.lance_session`, with its
thread-safety under concurrent opens.

What this file pins is what is specific to the catalog's own session.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def catalog_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimum `Settings` needs to construct, plus a cleared settings cache.

    `get_settings` is `@lru_cache`d process-wide, so a test that did not clear it would read whatever
    an earlier test happened to build — the session's caps included.
    """
    from catalog.core import config

    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "s")
    config.get_settings.cache_clear()


def test_the_session_is_ONE_object_across_calls(catalog_env: None) -> None:
    """`lance_session` is `@cache`d on its cap pair, so equal caps share one session process-wide. If
    each caller built its own, every open would still mint a fresh cache and the conversion would be
    decoration."""
    from catalog.core.config import shared_lance_session

    assert shared_lance_session() is shared_lance_session()


def test_a_warehouse_rooted_namespace_caches_in_the_shared_session(catalog_env: None, tmp_path: Path) -> None:
    """A warehouse's connection reads its manifest table through the bounded session, not a session of its own.

    `lance.Session.size_bytes()` is the only reading pylance 14.0.3 exposes of a session's caches, so the claim is read
    off it: a namespace built on another session leaves the shared one exactly where it was.
    """
    from lance_namespace import CreateNamespaceRequest, ListNamespacesRequest

    from catalog.core.config import get_settings, shared_lance_session
    from catalog.core.namespace import build_namespace_for_root

    session = shared_lance_session()
    before = session.size_bytes()

    namespace = build_namespace_for_root(get_settings(), str(tmp_path / "warehouse"))
    namespace.create_namespace(CreateNamespaceRequest(id=["a"]))
    namespace.list_namespaces(ListNamespacesRequest(id=[]))

    assert session.size_bytes() > before
