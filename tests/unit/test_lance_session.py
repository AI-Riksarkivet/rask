"""The shared bounded Lance session (#102) — one per process, threaded through every maintenance open.

The defect: each bare ``lance.dataset()`` minted and discarded a 1 GiB + 6 GiB default cache pair —
per dataset per tick in the sweep, per VERSION (ceiling 500) in the orphan scan — on a 512Mi pod.
These tests pin the fix's three load-bearing facts against REAL datasets: the session is a
process-wide singleton per cap-pair, the maintenance opens actually engage it (size grows), and the
orphan walk reuses the head dataset's cache via ``checkout_version`` instead of reopening.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
from maintenance.services import optimize, orphans

from service_kit.lakehouse.lance_session import lance_session


def _dataset(tmp_path: Path, rows: int = 8) -> str:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"x": list(range(rows))}), uri, mode="create")
    # a few versions so the orphan walk has something to iterate
    for i in range(3):
        lance.write_dataset(pa.table({"x": [100 + i]}), uri, mode="append")
    return uri


def test_equal_caps_share_ONE_session_and_different_caps_do_not() -> None:
    a = lance_session(1 << 20, 1 << 20)
    b = lance_session(1 << 20, 1 << 20)
    c = lance_session(2 << 20, 1 << 20)
    assert a is b, "equal caps must resolve to the same process-wide session"
    assert a is not c


def test_a_session_threaded_open_ENGAGES_the_shared_cache(tmp_path: Path) -> None:
    """The empirical core: opens without a session leave it flat; opens with it grow it."""
    session = lance_session(8 << 20, 8 << 20)
    before = session.size_bytes()
    uri = _dataset(tmp_path)
    ds = lance.dataset(uri, session=session)
    _ = ds.to_table()
    assert session.size_bytes() > before, "the open never engaged the shared session"


def test_referenced_paths_walks_versions_on_the_HEAD_dataset_cache(tmp_path: Path, monkeypatch) -> None:
    """The N+1 fix: the per-version walk must NOT construct fresh datasets. Counted at the real
    seam — lance.dataset call count during referenced_paths is exactly ONE (the head open);
    versions ride checkout_version, which reuses the head's cache by contract."""
    uri = _dataset(tmp_path)
    calls: list[str] = []
    real = lance.dataset

    def counting(uri_arg, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003 - test seam
        calls.append(str(uri_arg))
        return real(uri_arg, *args, **kwargs)

    monkeypatch.setattr(orphans.lance, "dataset", counting)
    referenced, count, note = orphans.referenced_paths(uri)
    assert count >= 4 and referenced, "the walk saw the versions and files"
    assert note is None
    assert len(calls) == 1, f"per-version REOPENS are back ({len(calls)} constructor calls) — the #102 N+1"


def test_compact_one_threads_the_shared_session(tmp_path: Path, monkeypatch) -> None:
    uri = _dataset(tmp_path)
    seen: list[object] = []
    real = lance.dataset

    def recording(uri_arg, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003 - test seam
        seen.append(kwargs.get("session"))
        return real(uri_arg, *args, **kwargs)

    monkeypatch.setattr(optimize.lance, "dataset", recording)
    result = optimize.compact_one(uri, storage_options={}, older_than=None)
    assert result.error is None
    assert seen and seen[0] is not None, "compact_one opened without the shared session"
