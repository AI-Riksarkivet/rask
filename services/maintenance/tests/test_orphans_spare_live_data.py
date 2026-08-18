"""The orphan scan decides what a reclaimer may delete, and three live file classes look like garbage.

This module's own docstrings record all three as found the hard way, on a real estate rather than by
reading the layout: `_refs/tags/*.json` are TAGS, which PIN versions (`cleanup_old_versions` exempts
tagged versions for exactly that reason, so a reclaimer acting on one would unpin published data);
`.lance-reserved` is a structural marker no manifest ever references; and a blob column's bytes live in
a `data/<data-file-stem>/` SIDECAR that `data_files()` does not name, so a scan that stops at the
`.lance` reports every page image in the estate as reclaimable.

None of it had a test. `services/maintenance` had no `tests/` directory and was not in `testpaths`, on
the one service in the estate whose job is deleting things.

These run against a REAL Lance dataset in a temp directory — no cluster, no S3. The scan's whole
contract is what it says about files on a filesystem, and a double for the filesystem would be a
double for the thing under test.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.fs as pafs
import pytest
from lance import blob_array, blob_field
from maintenance.services.orphans import _kind_of, scan_dataset, scan_datasets


@pytest.fixture
def dataset(tmp_path: Path) -> str:
    """A two-version table, so `_versions/` and `_transactions/` are both populated."""
    uri = str(tmp_path / "features.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), uri)
    lance.write_dataset(pa.table({"id": [4, 5]}), uri, mode="append")
    return uri


#: Payload size that forces a blob column OUT of the data file and into a sidecar. Measured: at 40 KB
#: the bytes inline into the `.lance` and no sidecar exists at all, so a smaller fixture would assert
#: nothing while passing.
_SPILLS_TO_SIDECAR = 2_000_000


@pytest.fixture
def blob_dataset(tmp_path: Path) -> str:
    """A table with a real blob-v2 column whose payload bytes land in `data/<stem>/*.blob`."""
    uri = str(tmp_path / "pages.lance")
    payloads = [b"\x89PNG" + b"x" * _SPILLS_TO_SIDECAR, b"\x89PNG" + b"y" * _SPILLS_TO_SIDECAR]
    lance.write_dataset(
        pa.table({"payload": blob_array(payloads)}, schema=pa.schema([blob_field("payload")])),
        uri,
        data_storage_version="2.2",
    )
    return uri


def _scan(uri: str) -> list[str]:
    result = scan_dataset(pafs.LocalFileSystem(), uri, uri)
    assert result.checked, f"the dataset was unreadable: {result.reason}"
    return sorted(o.path for o in result.orphans)


def _plant(uri: str, rel: str, body: bytes = b"x") -> None:
    path = Path(uri) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


class TestTheThreeThingsThatLookLikeGarbageAndAreNot:
    def test_a_TAG_is_never_an_orphan(self, dataset: str) -> None:
        """The failure this module exists to avoid: the first live run reported every `publish-*`
        promotion tag in the estate. A tag PINS a version, so deleting one unpins published data."""
        _plant(dataset, "_refs/tags/publish-1.json", b'{"version": 1}')

        assert not [p for p in _scan(dataset) if p.startswith("_refs/")]

    def test_the_RESERVED_MARKER_is_never_an_orphan(self, dataset: str) -> None:
        """Zero-byte, structural, referenced by no manifest — so a naive scan reports it once per
        dataset forever, which is how a report trains its reader to ignore it."""
        _plant(dataset, ".lance-reserved", b"")

        assert ".lance-reserved" not in _scan(dataset)

    def test_a_BLOB_SIDECAR_is_never_an_orphan(self, blob_dataset: str) -> None:
        """`data_files()` names only the `.lance`; the payload bytes sit in `data/<stem>/` beside it.
        A scan that stops at the manifest calls every page image in the estate reclaimable."""
        sidecars = [posixpath.relpath(str(p), blob_dataset) for p in Path(blob_dataset).rglob("*") if p.is_file() and p.suffix == ".blob"]
        assert sidecars, "the fixture wrote no blob sidecar — the test would pass vacuously"

        orphans = _scan(blob_dataset)

        assert not [p for p in orphans if p in sidecars], f"live blob payloads reported reclaimable: {orphans}"

    def test_a_MANIFEST_is_never_an_orphan(self, dataset: str) -> None:
        """A manifest is what makes a version live; it cannot be unreferenced while it is present."""
        assert not [p for p in _scan(dataset) if p.startswith("_versions/")]


class TestItStillFindsRealGarbage:
    def test_an_unreferenced_data_file_IS_reported(self, dataset: str) -> None:
        """The scan must still be worth running. A test that only proves it reports nothing would pass
        against a scanner that had been turned off."""
        _plant(dataset, "data/stray-0000.lance", b"not a real fragment")

        assert "data/stray-0000.lance" in _scan(dataset)

    def test_the_finding_is_classified_by_AREA(self, dataset: str) -> None:
        _plant(dataset, "data/stray-0000.lance")
        result = scan_dataset(pafs.LocalFileSystem(), dataset, dataset)

        assert [(o.path, o.kind) for o in result.orphans if o.path.startswith("data/")] == [("data/stray-0000.lance", "data")]

    def test_the_reported_size_is_the_real_one(self, dataset: str) -> None:
        """A reclamation report whose sizes are wrong cannot be used to decide what to reclaim."""
        _plant(dataset, "data/stray-0000.lance", b"z" * 4096)
        result = scan_dataset(pafs.LocalFileSystem(), dataset, dataset)

        assert [o.size_bytes for o in result.orphans if o.path == "data/stray-0000.lance"] == [4096]


class TestKindClassification:
    @pytest.mark.parametrize(
        ("rel", "kind"),
        [
            ("data/x.lance", "data"),
            ("_deletions/1.arrow", "deletions"),
            ("_indices/uuid/index.idx", "indices"),
            ("_transactions/3-uuid.txn", "transactions"),
            ("_versions/2.manifest", "versions"),
            ("_refs/tags/publish-1.json", "refs"),
            ("latest_version_hint.json", "other"),
            (".lance-reserved", "other"),
        ],
    )
    def test_each_area_is_named(self, rel: str, kind: str) -> None:
        assert _kind_of(rel) == kind


class TestUnreadableIsNotClean:
    def test_an_unreadable_dataset_reports_checked_FALSE_with_no_orphans(self, tmp_path: Path) -> None:
        """ "We could not look" and "there was nothing there" are different answers, and only one of
        them is safe to act on."""
        result = scan_dataset(pafs.LocalFileSystem(), str(tmp_path / "nope.lance"), str(tmp_path / "nope.lance"))

        assert result.checked is False
        assert result.orphans == []
        assert result.reason

    def test_the_aggregate_counts_it_as_UNREADABLE_not_as_scanned(self, dataset: str, tmp_path: Path) -> None:
        """An unreadable dataset must not silently reduce the orphan count."""
        missing = str(tmp_path / "nope.lance")
        _plant(dataset, "data/stray-0000.lance")

        report = scan_datasets(pafs.LocalFileSystem(), [(dataset, dataset), (missing, missing)])

        assert report.datasets_scanned == 1
        assert report.datasets_unreadable == 1
        assert report.incomplete, "an unreadable dataset must be NAMED, so the reader knows what to distrust"
        assert report.total == len(report.orphans)
