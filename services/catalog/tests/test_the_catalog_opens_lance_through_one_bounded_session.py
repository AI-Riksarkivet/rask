"""The catalog's Lance opens share one bounded session, so the cache exists at all.

[[LH-096]]. Every lakehouse pod runs a 512 Mi limit (128 Mi request). A bare `lance.dataset(uri)` mints
Lance's DEFAULT ceilings — 1 GiB metadata, 6 GiB index — and discards them WITH the handle, so a
process that opens per request never caches anything and carries caps that dwarf the container it runs
in. The failure is not theoretical: `rask-maintenance` was OOMKilled (exit 137) on 2026-09-10, which is
why maintenance already threads `shared_lance_session()` and the caps are clamped to the cgroup.

THE CATALOG IS THE ONE THAT OPENS PER REQUEST, which is what makes it the next one to convert:
`namespace.py`, `tables/`, `publication.py`, `vending.py`, `models.py` and the credentials door all
open on the request path, so the mint-and-discard happens per call rather than per sweep.

A SESSION IS NOT A HANDLE CACHE, and the distinction is the whole reason this is safe to do. Caching a
DATASET HANDLE pins a version and needs a freshness contract; a `lance.Session`'s keys carry
`(uri, version, etag)`, so a compaction writes NEW keys and a new version is never served stale. An old
version whose data files were reclaimed can be, which is why evidence reads open on
`lakehouse.lancekit.lance_session.fresh_session`. Both are recorded in `lakehouse.lancekit.lance_session`, with its
thread-safety under concurrent opens.

What this file pins is what is specific to the catalog's own session.
"""

from pathlib import Path

import lance
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


@pytest.fixture
def empty_shared_session(catalog_env: None) -> lance.Session:
    """The process's shared session, built anew so that its caches are empty.

    The session is process-wide and bounded, so a long run in one process fills its metadata cache to the cap, and from
    there a read evicts as much as it adds: `size_bytes()` no longer grows. Measured in the serial lane, 2026-11-22: the
    cache held 134,213,416 of its 134,217,728 bytes, and the read below left `size_bytes()` 2,182 bytes smaller.
    """
    from catalog.core.config import shared_lance_session
    from lakehouse.lancekit.lance_session import lance_session

    lance_session.cache_clear()
    return shared_lance_session()


def test_a_warehouse_rooted_namespace_caches_in_the_shared_session(empty_shared_session: lance.Session, tmp_path: Path) -> None:
    """A warehouse's connection reads its manifest table through the bounded session, not a session of its own.

    `lance.Session.size_bytes()` is the only reading pylance 14.0.3 exposes of a session's caches, so the claim is read
    off it: a namespace built on another session leaves the shared one exactly where it was.
    """
    from lance_namespace import CreateNamespaceRequest, ListNamespacesRequest

    from catalog.core.config import get_settings
    from catalog.core.namespace import build_namespace_for_root

    session = empty_shared_session
    before = session.size_bytes()

    namespace = build_namespace_for_root(get_settings(), str(tmp_path / "warehouse"))
    namespace.create_namespace(CreateNamespaceRequest(id=["a"]))
    namespace.list_namespaces(ListNamespacesRequest(id=[]))

    assert session.size_bytes() > before
