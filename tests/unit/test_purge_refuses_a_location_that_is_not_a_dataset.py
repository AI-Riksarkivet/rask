"""The purge deletes a whole directory tree, and never checked it was a Lance dataset.

the lakehouse register, row H6 (drained 2026-09-10; in git history), whose close condition is exactly this: *"Verify the location is a
Lance root before `delete_dir`."*

`delete_location` performs a RECURSIVE `delete_dir` on whatever a trash record's `location` names. Its
refusal ladder is real and covers three shapes — a location outside the maintained estate, a location
that IS a store root ("refusing to delete a whole bucket"), and one crossing a control prefix
(`_warehouses`, `_policies`, …). What it never asked is the simplest question: **is the thing at this
path a Lance dataset at all?**

A trash record's location comes from `describe_table` at drop time, so on the happy path it always is.
The guard is for the path where it is not: a hand-edited or corrupted record, a location reused after
a rename, a bug upstream that writes the wrong string. In every one of those the blast radius is a
recursive delete of live data that no other refusal in the ladder catches, and the estate's own rule
for this module is that a partial answer refuses rather than proceeds.

THE MARKER IS THE ONE DISCOVERY ALREADY TRUSTS. `discover_datasets` decides "this directory IS a
dataset" by the presence of `_versions/`, so using the same marker here keeps one definition of what a
dataset is rather than inventing a second.

COSTS NOTHING. `delete_location` already performs a recursive listing to sum the bytes it will
reclaim; the marker is in that listing. No extra round trip.

AN EMPTY OR ABSENT LOCATION IS STILL IDEMPOTENT SUCCESS, deliberately. A crash between the delete and
the record clear re-runs the whole record, and the existing contract is that `delete_dir` on an
already-absent path succeeds. Refusing there would strand every half-completed purge forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maintenance.services.purge import NotADatasetRootError, delete_location


def _dataset(root: Path) -> str:
    """A directory shaped like a Lance dataset: the `_versions/` marker plus a data file."""
    (root / "_versions").mkdir(parents=True)
    (root / "_versions" / "1.manifest").write_bytes(b"x" * 32)
    (root / "data").mkdir()
    (root / "data" / "a.lance").write_bytes(b"y" * 64)
    return f"file://{root}"


def test_a_real_dataset_is_deleted(tmp_path: Path) -> None:
    """The happy path must be untouched — this guard is worthless if it refuses ordinary purges."""
    location = _dataset(tmp_path / "table.lance")

    deleted_bytes, deleted_files = delete_location(location, {})

    assert deleted_files == 2, f"expected the manifest and the data file to be reclaimed, got {deleted_files}"
    assert deleted_bytes == 96, f"expected 32 + 64 bytes reclaimed, got {deleted_bytes}"
    assert not (tmp_path / "table.lance").exists(), "the dataset directory survived the purge"


def test_a_directory_that_is_NOT_a_dataset_is_refused(tmp_path: Path) -> None:
    """The headline: a location with no `_versions/` is not a dataset, and a recursive delete of it is
    not a purge — it is data loss with a trash record for an alibi."""
    stray = tmp_path / "not-a-dataset"
    stray.mkdir()
    (stray / "someones-real-file.txt").write_bytes(b"z" * 128)

    with pytest.raises(NotADatasetRootError) as caught:
        delete_location(f"file://{stray}", {})

    assert "not-a-dataset" in str(caught.value), f"the refusal does not name the location: {caught.value}"
    assert (stray / "someones-real-file.txt").exists(), "the refusal did not actually stop the delete"


def test_an_absent_location_is_still_idempotent_success(tmp_path: Path) -> None:
    """The contract the guard must not break.

    A crash between the delete and the record clear re-runs the whole record, so an already-gone path
    has to succeed. Refusing it — it has no `_versions/` either — would strand every interrupted purge.
    """
    deleted_bytes, deleted_files = delete_location(f"file://{tmp_path / 'gone'}", {})

    assert (deleted_bytes, deleted_files) == (0, 0), "an absent location should reclaim nothing and raise nothing"


def test_an_empty_directory_is_deleted_rather_than_refused(tmp_path: Path) -> None:
    """The other half of the same contract: a purge interrupted after its files were removed leaves an
    empty directory, and that must still be reclaimable."""
    empty = tmp_path / "empty.lance"
    empty.mkdir()

    deleted_bytes, deleted_files = delete_location(f"file://{empty}", {})

    assert (deleted_bytes, deleted_files) == (0, 0)
    assert not empty.exists(), "an empty leftover directory was not reclaimed"
