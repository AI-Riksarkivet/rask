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
`fresh_lance_session`. Both are recorded in `service_kit.lakehouse.lance_session`, with its
thread-safety under concurrent opens.

What this file pins is what is specific to the catalog's own session.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
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


def test_the_evidence_session_is_held_to_its_own_small_bound(tmp_path: Path) -> None:
    """`fresh_lance_session` is a SECOND session beside the shared one, which the cgroup clamp sizes for
    ONE; at the shared caps each erasure would add a whole budget. The verification reads every retained
    version once, so it gains nothing from a cache: measured on pylance 12.0.0 over these 251 versions of
    growing fragment counts, a session at the shared caps held 23.5 MB."""
    from catalog.core.config import fresh_lance_session

    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": list(range(100))}), uri)
    for start in range(100, 25_100, 100):
        lance.write_dataset(pa.table({"id": list(range(start, start + 100))}), uri, mode="append")

    session = fresh_lance_session()
    root = lance.dataset(uri, session=session)
    for entry in root.versions():
        root.checkout_version(entry["version"]).count_rows(filter="id = -1")

    assert session.size_bytes() <= 16 << 20, f"{session.size_bytes()} bytes held by one erasure's evidence session"
