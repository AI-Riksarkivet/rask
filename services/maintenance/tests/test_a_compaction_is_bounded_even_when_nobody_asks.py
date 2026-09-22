"""A compaction reaches Lance bounded even when the caller names no bound.

[[LH-185]]. The sweep passes `scan_batch_size`, `compact_threads` and `max_source_bytes` from settings,
so the lane that runs in production is bounded. `compact_one`'s own defaults were `None`, and `None`
means the keyword is never added to `size_kw` — so a caller that simply does not mention them gets
Lance's defaults: an 8192-ROW read batch, the HOST's core count as `num_threads`, and no byte ceiling
at all. That is incident #93 exactly, and it is reachable by writing one call.

THE ASYMMETRY IS THE TELL. `rewrite_slots` on the same function already defaults to the safe value,
with the rationale written beside it — "a caller that does not care is bounded rather than unbounded".
The three knobs that bound the bytes of a single pass defaulted the other way, which is the direction
that ends in an OOMKill rather than in slow work.

ONE SOURCE, TWO CONSUMERS. The numbers live once in `core.config` and are read by both the `Settings`
field defaults and this function's parameter defaults, so the floor cannot drift from the configured
value — two spellings of 64 is how a bound gets raised in one place and kept in the other.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from maintenance.core.config import DEFAULT_COMPACT_THREADS, DEFAULT_MAX_SOURCE_BYTES, DEFAULT_SCAN_BATCH_SIZE, MaintenanceSettings
from maintenance.services.optimize import compact_one


def _dataset(tmp: Path) -> str:
    uri = str(tmp / "unbounded.lance")
    table = pa.table({"id": pa.array(range(64), pa.int64()), "v": pa.array([f"x{i}" for i in range(64)])})
    for start in (0, 16, 32, 48):
        lance.write_dataset(table.slice(start, 16), uri, mode="create" if start == 0 else "append", data_storage_version="2.2")
    return uri


def test_a_caller_that_names_no_bound_still_reaches_lance_with_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The behavioural half: what ARRIVES at `compact_files`, not what the signature says."""
    uri = _dataset(tmp_path)
    asked: list[dict[str, object]] = []
    optimizer = lance.dataset(uri).optimize.__class__

    def _spy(self: object, *args: object, **kwargs: object) -> None:
        """Records the call and performs none — this test is about the ceiling the call carries."""
        asked.append(dict(kwargs))

    monkeypatch.setattr(optimizer, "compact_files", _spy)
    compact_one(uri, {}, None, cleanup_enabled=False, optimize_indices_enabled=False)

    assert asked, "compact_files was never called"
    kwargs = asked[0]
    assert kwargs.get("batch_size") == DEFAULT_SCAN_BATCH_SIZE, (
        f"the read batch reached Lance as {kwargs.get('batch_size')!r} — unset means Lance's own 8192 ROWS, "
        "and rows are not a unit of memory: ~1.8 MB bronze rows make that ~15 GB per compute thread"
    )
    assert kwargs.get("num_threads") == DEFAULT_COMPACT_THREADS, (
        f"num_threads reached Lance as {kwargs.get('num_threads')!r} — unset means the HOST's core count, "
        "which multiplies the read batch by a number the pod's cgroup never agreed to"
    )
    assert kwargs.get("max_source_bytes") == DEFAULT_MAX_SOURCE_BYTES, (
        f"max_source_bytes reached Lance as {kwargs.get('max_source_bytes')!r} — unset means one pass may "
        "pull in a whole table, which is the bound the two row-count knobs were only ever standing in for"
    )


def test_the_unbounded_state_is_not_reachable_from_the_signature() -> None:
    """The structural half: a `None` default is what made the behavioural leg above possible."""
    defaults = {name: p.default for name, p in inspect.signature(compact_one).parameters.items()}
    unbounded = [name for name in ("scan_batch_size", "compact_threads", "max_source_bytes") if defaults.get(name) is None]
    assert not unbounded, (
        f"{', '.join(unbounded)} default to None on compact_one, and None means the keyword never reaches "
        "`compact_files` at all — a new caller is unbounded by omission, the way `rewrite_slots` deliberately is not"
    )


def test_the_floor_and_the_configured_value_are_the_same_number() -> None:
    """Two spellings of 64 is how a bound gets raised in one place and silently kept in the other."""
    settings = MaintenanceSettings()
    assert settings.scan_batch_size == DEFAULT_SCAN_BATCH_SIZE
    assert settings.compact_threads == DEFAULT_COMPACT_THREADS
    assert settings.max_source_bytes == DEFAULT_MAX_SOURCE_BYTES


def test_an_explicit_none_from_the_wire_model_is_the_floor_not_lances_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The parameter default cannot cover this one: `DatasetWorkItem`'s three bound fields are
    `int | None = None`, because the wire model has to express "the policy said nothing" — and the
    sweep hands `plan.scan_batch_size` straight through. So the value that crosses the queue for an
    UNPOLICIED dataset is a literal `None`, which is the estate this floor exists for."""
    uri = _dataset(tmp_path)
    asked: list[dict[str, object]] = []
    optimizer = lance.dataset(uri).optimize.__class__
    monkeypatch.setattr(optimizer, "compact_files", lambda self, *a, **kw: asked.append(dict(kw)))

    compact_one(uri, {}, None, cleanup_enabled=False, optimize_indices_enabled=False, scan_batch_size=None, max_source_bytes=None, compact_threads=None)

    assert asked, "compact_files was never called"
    kwargs = asked[0]
    assert (kwargs.get("batch_size"), kwargs.get("num_threads"), kwargs.get("max_source_bytes")) == (
        DEFAULT_SCAN_BATCH_SIZE,
        DEFAULT_COMPACT_THREADS,
        DEFAULT_MAX_SOURCE_BYTES,
    ), f"an unpolicied work item reached Lance with {kwargs!r} — None must mean the floor, never no ceiling"
