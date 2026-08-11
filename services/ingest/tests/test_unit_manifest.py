"""§2.13 — chunk descriptors are POINTERS, and the unit list lives on object storage.

THE DEFECT. `enumerate_chunks` returned every key and token inline, so the run's whole key set rode
in ONE activity result and then AGAIN in each child's input. At ~83 bytes per serialized key+token
that met grpc's 4 MiB default — which the Dapr workflow worker never raises — at roughly 38k units,
on a plane whose own docstrings advertise million-unit harvests. A guard now refuses before the wedge;
this is the fix that removes the ceiling instead of naming it: history becomes O(chunks).

WHERE THE MANIFEST LIVES IS A SAFETY ARGUMENT, not a filing preference. `discover_staged` — the
crash-recovery path — reads everything under a run's staging prefix and treats each record as a
fragment manifest, and `_read_all` is ASYMMETRIC: top-level glob on the filesystem, RECURSIVE list on
an object store. A unit manifest beneath the run prefix would therefore be invisible to recovery in
dev and read by it in production. It goes in a sibling directory, and these pin that.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ingest.staging import (
    UnitManifestMissing,
    discover_staged,
    purge_staged,
    read_unit_slice,
    stage_fragments,
    staging_root,
    unit_manifest_uri,
    write_unit_manifest,
)


RUN = "run-42"


def test_the_manifest_is_OUTSIDE_the_runs_staging_prefix(tmp_path: Path) -> None:
    """The whole safety argument in one assertion. Inside the prefix, `discover_staged` would read it
    as a fragment manifest on S3 (recursive list) and not on a filesystem (top-level glob) — a defect
    that only appears in production."""
    dataset = str(tmp_path / "bronze.lance")

    manifest = unit_manifest_uri(dataset, RUN)

    assert not manifest.startswith(staging_root(dataset, RUN)), "the unit manifest is inside the recovery path's search prefix"


def test_the_recovery_path_cannot_see_it(tmp_path: Path) -> None:
    """Driven rather than reasoned: write BOTH kinds of record, then ask the recovery path what it
    should commit. It must find the fragment manifest and nothing else."""
    dataset = str(tmp_path / "bronze.lance")
    write_unit_manifest(dataset, RUN, [("s3://b/a.tif", "etag-1"), ("s3://b/b.tif", None)])
    stage_fragments(dataset, RUN, ["s3://b/a.tif"], ['{"frag": 1}'])

    assert discover_staged(dataset, RUN) == ['{"frag": 1}']


def test_a_window_round_trips(tmp_path: Path) -> None:
    """The property the whole change rests on: a chunk carrying `(offset, count)` can recover exactly
    the units it stands for."""
    dataset = str(tmp_path / "bronze.lance")
    pairs = [(f"s3://b/{i:04d}.tif", f"etag-{i}") for i in range(10)]
    write_unit_manifest(dataset, RUN, pairs)

    assert read_unit_slice(dataset, RUN, 3, 4) == pairs[3:7]
    assert read_unit_slice(dataset, RUN, 0, 10) == pairs
    assert read_unit_slice(dataset, RUN, 9, 5) == pairs[9:], "a window running past the end is the LAST chunk, not an error"


def test_a_null_token_survives_the_round_trip(tmp_path: Path) -> None:
    """Tokens are positional-parallel identity material the workers fold into row ids. A source with
    no version token writes None, and None must come back as None rather than the string 'None'."""
    dataset = str(tmp_path / "bronze.lance")
    write_unit_manifest(dataset, RUN, [("s3://b/a.tif", None)])

    assert read_unit_slice(dataset, RUN, 0, 1) == [("s3://b/a.tif", None)]


def test_a_MISSING_manifest_raises_rather_than_publishing_nothing(tmp_path: Path) -> None:
    """The dangerous silent failure. A chunk that cannot load its intent must not report success
    having published zero units — `drain_chunk`'s reconcile is built to notice a SHORTFALL against an
    expected count, not an empty intent, so an empty publish would look like a chunk that legitimately
    had no work."""
    dataset = str(tmp_path / "bronze.lance")

    with pytest.raises(UnitManifestMissing):
        read_unit_slice(dataset, RUN, 0, 5)


def test_purge_removes_it_too(tmp_path: Path) -> None:
    """It needs its OWN removal precisely because it sits outside the run prefix: the prefix delete
    that clears the fragments cannot reach it, so without this every completed run leaks one manifest
    forever."""
    dataset = str(tmp_path / "bronze.lance")
    write_unit_manifest(dataset, RUN, [("s3://b/a.tif", None)])
    stage_fragments(dataset, RUN, ["s3://b/a.tif"], ["{}"])

    removed = purge_staged(dataset, RUN)

    assert removed == 2, "the unit manifest was not counted or not removed"
    assert not Path(unit_manifest_uri(dataset, RUN)).exists()
    with pytest.raises(UnitManifestMissing):
        read_unit_slice(dataset, RUN, 0, 1)


def test_the_manifest_is_per_RUN(tmp_path: Path) -> None:
    """Two runs against one dataset must not share or purge each other's unit list."""
    dataset = str(tmp_path / "bronze.lance")
    write_unit_manifest(dataset, "run-a", [("s3://b/a.tif", None)])
    write_unit_manifest(dataset, "run-b", [("s3://b/b.tif", None)])

    purge_staged(dataset, "run-a")

    assert read_unit_slice(dataset, "run-b", 0, 1) == [("s3://b/b.tif", None)]
