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

THE STRUCTURAL HALF LIVES IN `tests/unit/test_no_lakehouse_service_opens_lance_unbounded.py`, which
holds all four lakehouse services to the rule at once — one gate rather than four copies that drift.
What stays here is what is specific to the catalog's own session.

THE BEHAVIOURAL TEST IS THE LOAD-BEARING ONE. Asserting that a `session=` kwarg is passed would pass
for a session that is never reused — the defect in a different shape. So the test opens the same dataset
repeatedly and asserts the session's `size_bytes` GROWS, and that the same call without a session leaves
it flat. That is the row's own measurement, and it is what proves the cache engages.
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
    an earlier test happened to build — the caps under test included.
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


def test_opens_against_the_shared_session_actually_POPULATE_it(tmp_path: Path, catalog_env: None) -> None:
    """THE PROOF, and the reason a kwarg assertion is not enough.

    A session that is passed but never reused caches nothing. So: open the same dataset repeatedly
    through the shared session and require its `size_bytes` to grow, then do the identical opens with no
    session and require the session to stay where it was — the row's own measurement, which found that
    ten version-opens grow a shared session 168 -> ~75k while the same opens without one leave it flat.
    """
    from catalog.core.config import shared_lance_session

    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": list(range(64))}), uri)

    session = shared_lance_session()
    before = session.size_bytes()
    for _ in range(10):
        lance.dataset(uri, session=session).count_rows()
    after_shared = session.size_bytes()

    assert after_shared > before, f"the session did not grow ({before} -> {after_shared}) — nothing is being cached"

    for _ in range(10):
        lance.dataset(uri).count_rows()
    after_bare = session.size_bytes()

    assert after_bare == after_shared, (
        f"a bare open touched the shared session ({after_shared} -> {after_bare}), which would mean this test cannot tell the two paths apart"
    )


def test_the_caps_are_clamped_to_the_container_not_taken_literally(monkeypatch: pytest.MonkeyPatch) -> None:
    """A literal cap cannot track `resources.limits.memory`: raise the pod and it should follow, lower it
    and it MUST. Maintenance learned this by being OOMKilled with 128+256 MB configured inside a 512 Mi
    pod — the caps are LRU soft bounds, the size the cache grows toward, not a ceiling it stops at.

    THE BUDGET IS FORCED, because this suite does not run in the container it is reasoning about. Off a
    cgroup, `cache_budget_bytes()` answers `None` and granting the request verbatim is CORRECT — there
    is no limit to clamp to. Asserting a clamp without a budget would have tested the host, not the
    rule, and would have failed on every developer machine while passing in CI for the wrong reason.
    """
    from service_kit.lakehouse import lance_session as seam

    # Mirrors the real signature: `cache_budget_bytes` takes the fraction and returns the share of the
    # container it is willing to spend, so a stub that ignored it would budget the WHOLE pod for cache.
    monkeypatch.setattr(seam, "cache_budget_bytes", lambda *, fraction=0.4: int((512 << 20) * fraction))
    granted_md, granted_idx = seam.affordable_cache_bytes(8 << 30, 16 << 30)

    assert granted_md < (8 << 30), "an 8 GiB metadata cap survived a 512 Mi container; nothing is clamping"
    assert granted_idx < (16 << 30)
    assert granted_md > 0 and granted_idx > 0, "clamped to nothing at all — the cache would never engage"
    assert granted_md + granted_idx < (512 << 20), "the two caps together already exceed the pod's whole limit"


@pytest.mark.parametrize("attr", ["lance_metadata_cache_mb", "lance_index_cache_mb"])
def test_the_caps_are_configurable_per_deployment(attr: str) -> None:
    """Named settings rather than literals, so an operator who raises the pod can raise the caps with it
    — the same shape maintenance carries."""
    from catalog.core.config import Settings

    assert hasattr(Settings.model_fields.get(attr), "default"), f"{attr} is not a setting"


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
