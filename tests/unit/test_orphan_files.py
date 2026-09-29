"""Unreferenced-file detection, against REAL Lance datasets on the local filesystem.

Deliberately not faked: the whole risk in this pass is disagreeing with what Lance actually writes,
and a double that agrees with my assumptions would prove only that I am self-consistent. Every
fixture here is a real dataset built with `lance.write_dataset`, so the layout under test is the
layout Lance produces (`data/`, `_deletions/`, `_transactions/`, `_versions/`).

The load-bearing property is asymmetric: reporting a live file as an orphan is catastrophic (a
reclaimer would delete a table's data), while missing one is merely untidy. So most of these pin the
NEGATIVE — that nothing live is ever named.

Three live file classes look like garbage, each found the hard way on a real estate: `_refs/tags/*.json`
are TAGS, which PIN versions (`cleanup_old_versions` exempts tagged versions for that reason, so a
reclaimer acting on one would unpin published data); `.lance-reserved` is a structural marker no manifest
ever references; and a blob column's bytes live in a `data/<data-file-stem>/` SIDECAR that `data_files()`
does not name, so a scan that stops at the `.lance` reports every page image in the estate as reclaimable.
"""

from __future__ import annotations

import pathlib
import posixpath
from collections.abc import Callable
from typing import cast

import lance
import pyarrow as pa
import pyarrow.fs as pafs
import pytest
from lance import blob_array, blob_field
from lance.dataset import DatasetBasePath

from maintenance.services import orphans


def _fs() -> pafs.FileSystem:
    return pafs.LocalFileSystem()


def _table(n: int = 3) -> pa.Table:
    return pa.table({"id": pa.array(list(range(n))), "v": pa.array([f"r{i}" for i in range(n)])})


def _dataset(tmp_path: pathlib.Path, name: str = "t.lance") -> tuple[str, str]:
    """A real multi-version dataset. Returns ``(uri, prefix)`` — Lance opens by URI, pyarrow lists by path."""
    path = str(tmp_path / name)
    lance.write_dataset(_table(), path)
    lance.write_dataset(_table(), path, mode="append")
    return path, path


# --------------------------------------------------------------------------- #
# the safety property: a healthy dataset has NO orphans
# --------------------------------------------------------------------------- #


def test_files_only_an_OLD_version_references_are_not_orphans(tmp_path: pathlib.Path) -> None:
    """The single most dangerous way this pass could be wrong.

    Every manifest still in `_versions/` is reachable by time-travel until `cleanup_old_versions`
    removes it, so a data file that only version 1 cites is LIVE. Computing the referenced set from
    the CURRENT version alone would report most of a healthy table's history as garbage — and a
    reclaimer acting on that would destroy the table's history.
    """
    uri, _prefix = _dataset(tmp_path)
    at_v1 = lance.dataset(uri, version=1)
    only_v1 = {f"data/{f.path}" for fr in at_v1.get_fragments() for f in fr.data_files()}
    assert only_v1, "fixture must actually have a v1 data file"

    referenced, versions, note = orphans.referenced_paths(uri)
    assert note is None
    assert versions >= 2
    assert only_v1 <= referenced, "a file referenced only by an older version was treated as unreferenced"


def test_a_deletion_vector_is_referenced_not_orphaned(tmp_path: pathlib.Path) -> None:
    """`_deletions/*.arrow` is named by a fragment, not by the dataset directory, so it is exactly the
    kind of file a path-only scan would miss and report. Its on-disk name is rendered by Lance's own
    `DeletionFile.path()` rather than reconstructed from the naming convention here."""
    uri, prefix = _dataset(tmp_path)
    lance.dataset(uri).delete("id = 1")
    deletions = sorted(p.name for p in (tmp_path / "t.lance" / "_deletions").iterdir())
    assert deletions, "fixture must actually produce a deletion vector"

    result = orphans.scan_dataset(_fs(), uri, prefix=prefix)
    assert result.checked
    assert [o.path for o in result.orphans if o.kind == "deletions"] == []


# --------------------------------------------------------------------------- #
# what it DOES find
# --------------------------------------------------------------------------- #


def test_transaction_files_are_reported_because_they_accumulate_by_design(tmp_path: pathlib.Path) -> None:
    """The spec is explicit that on a conflict "transaction files remain in storage describing each
    commit attempt". So a busy table grows `_transactions/` forever, nothing prunes them, and this is
    the only thing that will ever say so."""
    uri, prefix = _dataset(tmp_path)
    txn_dir = tmp_path / "t.lance" / "_transactions"
    live = sorted(p.name for p in txn_dir.iterdir())
    assert live, "fixture must actually produce .txn files"

    # THE FIXTURE MUST CONTAIN A FAILED ATTEMPT, and until 2026-08-16 it did not — it made only
    # successful appends, whose txn files all belong to live versions and are therefore REFERENCED,
    # not garbage. Under pylance 10.0.0 `get_transactions` returns one entry per live version
    # (measured: 3 files, 3 versions, 0 unreferenced), so every txn matched and the assertion below
    # had nothing left to find. The scanner was right; the fixture was describing a different dataset
    # from the one the docstring describes.
    #
    # The class this test is about is the CONFLICTED commit — the spec's "transaction files remain in
    # storage describing each commit attempt" — so the fixture now leaves one behind explicitly. A
    # read_version far past any live version cannot be claimed by a manifest.
    orphaned = txn_dir / "999-00000000-0000-0000-0000-0000deadbeef.txn"
    orphaned.write_bytes(b"a commit attempt that never became a version")

    result = orphans.scan_dataset(_fs(), uri, prefix=prefix)
    reported = {o.path for o in result.orphans if o.kind == "transactions"}
    assert reported == {f"_transactions/{orphaned.name}"}, (
        "only the UNREFERENCED txn is garbage — a txn belonging to a live version is the provenance "
        f"of a manifest being read, and reporting it would be reporting live data. live={live}"
    )


# --------------------------------------------------------------------------- #
# report-only, mechanically
# --------------------------------------------------------------------------- #


def test_a_scan_leaves_the_dataset_byte_identical(tmp_path: pathlib.Path) -> None:
    """The behavioural half: fingerprint every file before and after a scan of a DRIFTED dataset (the
    state a reclaimer would be tempted to fix) and require they match exactly."""
    import hashlib

    uri, prefix = _dataset(tmp_path)
    (tmp_path / "t.lance" / "data" / "000000000000000000000000000000cafe.lance").write_bytes(b"stray")
    root = tmp_path / "t.lance"

    def fingerprint() -> dict[str, str]:
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}

    before = fingerprint()
    report = orphans.scan_datasets(_fs(), [(uri, prefix)])
    assert report.total >= 1, "the fixture must actually BE drifted, or 'nothing changed' proves nothing"
    assert fingerprint() == before


def test_a_blob_sidecar_of_a_referenced_data_file_is_not_an_orphan(tmp_path: pathlib.Path) -> None:
    """A large-binary column's bytes live in `data/<data-file-stem>/*.blob`, NOT inside the `.lance`.

    `data_files()` names only the `.lance`, so a scan that stops there reports every blob in the
    estate as reclaimable. Measured live 2026-08-04 against the bronze page-image table: 29 MB of
    real page images named as garbage. The sidecar DIRECTORY of a referenced data file is referenced.

    A sidecar whose parent data file is referenced by no live version is still reported, and
    correctly so — `cleanup_old_versions` reclaims the `.lance` and leaves the sidecar behind, which
    is exactly the reclamation gap this pass exists to name.
    """
    uri, prefix = _dataset(tmp_path)
    referenced, _, _ = orphans.referenced_paths(uri)
    stems = [p.removeprefix("data/").removesuffix(".lance") for p in referenced if p.endswith(".lance")]
    assert stems, "fixture must have at least one referenced data file"

    live_sidecar = tmp_path / "t.lance" / "data" / stems[0]
    live_sidecar.mkdir(parents=True, exist_ok=True)
    (live_sidecar / "10000000000000000000000000000000.blob").write_bytes(b"page image bytes")

    dead_sidecar = tmp_path / "t.lance" / "data" / ("f" * 50)
    dead_sidecar.mkdir(parents=True, exist_ok=True)
    (dead_sidecar / "10000000000000000000000000000000.blob").write_bytes(b"left by a reclaimed version")

    named = {o.path for o in orphans.scan_dataset(_fs(), uri, prefix=prefix).orphans}
    assert f"data/{stems[0]}/10000000000000000000000000000000.blob" not in named, "a LIVE blob sidecar was named as an orphan"
    assert f"data/{'f' * 50}/10000000000000000000000000000000.blob" in named, "a sidecar with no live parent must still be reported"


# --------------------------------------------------------------------------- #
# The layout gate — two shapes whose false positives are LIVE data.
# Built with the REAL lance branch/clone APIs, because the whole bug was that the
# on-disk layout does not match what a prefix scan assumes.
# --------------------------------------------------------------------------- #


def test_a_branched_dataset_is_refused_not_scanned(tmp_path: pathlib.Path) -> None:
    """A branch is a whole parallel dataset under `tree/{branch}/` — its own `_versions/`,
    `_transactions/`, `_deletions/`, `_indices/`.

    `lance.dataset(uri)` opens the MAIN branch, so nothing under `tree/` is ever in the referenced
    set: every file of every branch is unreferenced BY CONSTRUCTION. Measured before the gate: a
    two-commit branch produced 6 of 7 findings as branch files, INCLUDING the branch's own
    `data/*.lance`. A reclaimer acting on that destroys the branch.
    """
    uri = str(tmp_path / "branched.lance")
    ds = lance.write_dataset(_table(), uri)
    branch = ds.create_branch("feature-a")
    lance.write_dataset(_table(2), branch, mode="append")
    assert (tmp_path / "branched.lance" / "tree" / "feature-a").exists(), "fixture must really branch"

    result = orphans.scan_dataset(_fs(), uri, prefix=uri)
    assert result.checked is False, "a branched dataset must be refused, not scanned"
    assert result.structural is True, "a branch is refused by SHAPE, and no retry ever makes it scannable"
    assert result.orphans == []
    assert "tree/" in (result.reason or "")


def test_a_shallow_clone_is_refused_because_its_data_lives_elsewhere(tmp_path: pathlib.Path) -> None:
    """A clone's manifest references data files that resolve through `base_paths` to ANOTHER dataset
    root, so they are simply not under this prefix.

    `base_paths` is not exposed by pylance, so the gate detects the CONSEQUENCE — a referenced path
    that is not present locally — which is strictly more robust than reading the flag would be.
    """
    source = str(tmp_path / "src.lance")
    ds = lance.write_dataset(_table(), source)
    clone = str(tmp_path / "clone.lance")
    ds.shallow_clone(clone, reference=1)

    result = orphans.scan_dataset(_fs(), clone, prefix=clone)
    assert result.checked is False, "a dataset spanning base_paths must be refused"
    assert result.structural is True, "base_paths is refused by SHAPE — the flag is a permanent property of the manifest"
    assert result.orphans == []
    assert "base_paths" in (result.reason or "")


def test_a_refused_dataset_does_not_read_as_clean_in_the_aggregate(tmp_path: pathlib.Path) -> None:
    """Refusing must stay COUNTED and NAMED — never quietly lower the orphan count. Otherwise the
    branchiest bucket in the estate reports as the tidiest.

    It is counted as an EXCLUSION rather than as an unreadable scan, and the distinction is the whole
    point: `purge.report_is_clean` blocks on `incomplete`, and a branch can never stop being refused —
    its files live under `tree/{branch}/` while `lance.dataset()` opens MAIN — so counting it there
    made reclamation unreachable. A branch is the cheapest fixture for that shape; on the live estate
    it was the shallow clone that did it, 419 of 490 notes, with `incomplete` frozen at exactly 490
    across a day in which the finding total moved.
    """
    uri = str(tmp_path / "branched.lance")
    ds = lance.write_dataset(_table(), uri)
    ds.create_branch("feature-a")

    report = orphans.scan_datasets(_fs(), [(uri, uri)])
    assert report.datasets_scanned == 0
    assert report.datasets_excluded == 1
    assert report.total == 0
    assert report.excluded and "tree/" in report.excluded[0]
    assert not report.incomplete, f"a permanent exclusion must not block the purge forever: {report.incomplete}"


def test_a_memwal_shard_tree_is_refused(tmp_path: pathlib.Path) -> None:
    """`_mem_wal/{shard}/` is an LSM tree beside the base table — WAL entries and SSTable datasets
    that NO base-table manifest references, so a prefix scan reports all of it.

    Refused rather than handled, and the spec gives the sharpest reason to keep it that way:
    "Deleting WAL files can weaken writer fencing… a stalled writer may write into empty space with
    an old writer_epoch." Fencing works by a put-if-not-exists COLLISION; GC removes the thing that
    collides. A failed flush also leaves a whole `{random8}_gen_{i}/` directory behind, because a
    retry writes a NEW one rather than reusing a partial — a real orphan producer that still needs
    the shard manifests to judge.
    """
    uri, prefix = _dataset(tmp_path)
    shard = tmp_path / "t.lance" / "_mem_wal" / "shard-0" / "wal"
    shard.mkdir(parents=True)
    (shard / "1010000000000000000000000000000000000000000000000000000000000000.arrow").write_bytes(b"wal")

    result = orphans.scan_dataset(_fs(), uri, prefix=prefix)
    assert result.checked is False
    assert result.structural is True, "a _mem_wal shard tree is refused by SHAPE"
    assert result.orphans == []
    assert "_mem_wal/" in (result.reason or "")
    assert "fencing" in (result.reason or "")


# --------------------------------------------------------------------------- #
# #64 — the manifest feature-flag gate. Complements the consequence checks above:
# it names the class the format itself names, and catches shapes that have no
# observable consequence YET.
# --------------------------------------------------------------------------- #


def test_a_registered_but_unused_base_is_refused_by_the_flag(tmp_path: pathlib.Path) -> None:
    """The proven hole in the consequence check, closed.

    `add_bases` registers an alternative base path (feature flag 16) that no DataFile resolves
    through yet — every `base_id` stays `None`, `tracked_files()` reports one base_uri, and every
    referenced path IS present under the prefix. So the "a referenced file is not here" check sees
    nothing, and this scan measurably returned `checked=True` with orphans named on a multi-base
    dataset. The manifest says plainly what the files do not, and the very next write can place one
    under that base.
    """
    uri = str(tmp_path / "based.lance")
    lance.write_dataset(_table(), uri)
    lance.write_dataset(_table(), uri, mode="append")
    alt = tmp_path / "altbase"
    alt.mkdir()
    lance.dataset(uri).add_bases([DatasetBasePath(path=str(alt), name="alt")])
    # The consequence detector genuinely has nothing to go on: every referenced path is local.
    referenced, _versions, _note = orphans.referenced_paths(uri)
    assert all((tmp_path / "based.lance" / rel).exists() for rel in referenced if not rel.endswith("/"))

    result = orphans.scan_dataset(_fs(), uri, prefix=uri)

    assert result.checked is False, "a multi-base dataset must be refused — a prefix listing cannot be subtracted"
    assert result.structural is True, "a multi-base dataset is refused by SHAPE"
    assert result.orphans == []
    assert "16" in (result.reason or "") and "base_paths" in (result.reason or "")


def test_a_REAL_overlay_dataset_is_refused_with_the_flag_named(tmp_path: pathlib.Path, overlay_dataset: Callable[[pathlib.Path], str]) -> None:
    """The real-dataset twin of the monkeypatched test above.

    On pylance 9.0.0 a committed overlay is WRITABLE but not readable — the open raises
    `Not supported: … Flags: 64` — so the `fragment.metadata.overlays` seam is unreachable and the
    scan's refusal has to come from classifying that open error. Unclassified it read as
    `ValueError: Not supported: … /home/runner/work/lance/lance/rust/lance/src/dataset.rs:725:24`,
    which tells the report's reader nothing at all.
    """
    uri = overlay_dataset(tmp_path)

    result = orphans.scan_dataset(_fs(), uri, prefix=uri)

    assert result.checked is False, "an overlay dataset must be refused, not scanned"
    assert result.structural is True, "an overlay dataset is refused by SHAPE"
    assert result.orphans == []
    assert "64" in (result.reason or "") and "overlay" in (result.reason or "").lower(), result.reason


def test_an_ordinary_dataset_is_not_refused_by_the_flag_gate(tmp_path: pathlib.Path) -> None:
    """The negative for the flag gate specifically: deletion files + stable row ids are supported
    flags, and a dataset carrying both must still be scanned. A gate keyed on "any flag at all"
    would refuse most of a real estate."""
    uri = str(tmp_path / "flagged.lance")
    lance.write_dataset(_table(), uri, enable_stable_row_ids=True)
    lance.dataset(uri).delete("id = 1")

    result = orphans.scan_dataset(_fs(), uri, prefix=uri)

    assert result.checked is True, result.reason
    assert result.reason is None


# --------------------------------------------------------------------------- #
# the live file classes that look like garbage, on real datasets built by Lance
# --------------------------------------------------------------------------- #


@pytest.fixture
def dataset(tmp_path: pathlib.Path) -> str:
    """A two-version table, so `_versions/` and `_transactions/` are both populated."""
    uri = str(tmp_path / "features.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), uri)
    lance.write_dataset(pa.table({"id": [4, 5]}), uri, mode="append")
    return uri


@pytest.fixture
def indexed_dataset(tmp_path: pathlib.Path) -> str:
    """A table carrying a real scalar index, so `_indices/<uuid>/` holds LIVE files.

    256 rows, not three: measured, a BTREE over a handful of rows still writes `page_data.lance` and
    `page_lookup.lance`, but the size is what makes the fixture recognisably an index rather than an
    artefact of a degenerate build.
    """
    uri = str(tmp_path / "indexed.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(256), pa.int64())}), uri).create_scalar_index("id", index_type="BTREE")
    return uri


#: Payload size that forces a blob column OUT of the data file and into a sidecar. Measured: at 40 KB
#: the bytes inline into the `.lance` and no sidecar exists at all, so a smaller fixture would assert
#: nothing while passing.
_SPILLS_TO_SIDECAR = 2_000_000


@pytest.fixture
def blob_dataset(tmp_path: pathlib.Path) -> str:
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
    result = orphans.scan_dataset(pafs.LocalFileSystem(), uri, prefix=uri)
    assert result.checked, f"the dataset was unreadable: {result.reason}"
    return sorted(o.path for o in result.orphans)


def _plant(uri: str, rel: str, body: bytes = b"x") -> None:
    path = pathlib.Path(uri) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


class TestTheLiveFilesThatLookLikeGarbage:
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
        sidecars = [posixpath.relpath(str(p), blob_dataset) for p in pathlib.Path(blob_dataset).rglob("*") if p.is_file() and p.suffix == ".blob"]
        assert sidecars, "the fixture wrote no blob sidecar — the test would pass vacuously"

        orphans_found = _scan(blob_dataset)

        assert not [p for p in orphans_found if p in sidecars], f"live blob payloads reported reclaimable: {orphans_found}"

    def test_a_REPLACED_index_is_spared_while_the_version_that_cites_it_lives(self, indexed_dataset: str) -> None:
        """`create_scalar_index(replace=True)` mints a NEW uuid and leaves the old directory in place.
        Measured: v2 cites the old uuid, v3 the new, and both dirs are on disk — so a referenced set
        built from the LATEST version alone reclaims an index that time-travel to v2 still needs. The
        union across live versions is the same rule the module already applies to data files."""
        old = lance.dataset(indexed_dataset).describe_indices()[0].segments[0].uuid
        lance.dataset(indexed_dataset).create_scalar_index("id", index_type="BTREE", replace=True)
        assert lance.dataset(indexed_dataset).describe_indices()[0].segments[0].uuid != old, "replace reused the uuid — the fixture proves nothing"

        assert not [p for p in _scan(indexed_dataset) if p.startswith(f"_indices/{old}/")]


class TestItStillFindsRealGarbage:
    def test_an_unreferenced_data_file_IS_reported(self, dataset: str) -> None:
        """The scan must still be worth running. A test that only proves it reports nothing would pass
        against a scanner that had been turned off."""
        _plant(dataset, "data/stray-0000.lance", b"not a real fragment")

        assert "data/stray-0000.lance" in _scan(dataset)

    def test_an_index_directory_NO_live_version_cites_IS_reported(self, indexed_dataset: str) -> None:
        """The fix must spare LIVE indices, not the `_indices/` prefix. A blanket skip would pass every
        test above while making the one file class it was written for permanently invisible."""
        _plant(indexed_dataset, "_indices/00000000-0000-0000-0000-000000000000/page_data.lance")

        assert "_indices/00000000-0000-0000-0000-000000000000/page_data.lance" in _scan(indexed_dataset)


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
        assert orphans._kind_of(rel) == kind


class TestUnreadableIsNotClean:
    def test_the_aggregate_counts_it_as_UNREADABLE_not_as_scanned(self, dataset: str, tmp_path: pathlib.Path) -> None:
        """An unreadable dataset must not silently reduce the orphan count."""
        missing = str(tmp_path / "nope.lance")
        _plant(dataset, "data/stray-0000.lance")

        report = orphans.scan_datasets(pafs.LocalFileSystem(), [(dataset, dataset), (missing, missing)])

        assert report.datasets_scanned == 1
        assert report.datasets_unreadable == 1
        assert report.incomplete, "an unreadable dataset must be NAMED, so the reader knows what to distrust"
        assert report.total == len(report.orphans)


class _RaisesOnLayoutProbe:
    """A filesystem whose `tree/` layout probe RAISES (transient S3 / permission), instead of the
    NotFound a merely-absent key returns — the exact case the scan must not read as "no branches"."""

    def __init__(self, inner: pafs.FileSystem, probe_path: str) -> None:
        self._inner = inner
        self._probe_path = probe_path

    def get_file_info(self, arg: object) -> object:
        if isinstance(arg, str) and arg == self._probe_path:
            raise OSError("transient object-store error stat-ing the layout probe")
        return self._inner.get_file_info(arg)


class TestAnUncertifiableLayoutIsNotClean:
    def test_a_RAISING_branch_probe_fails_closed(self, dataset: str) -> None:
        """A `tree/` probe that raises means we could not certify the dataset has no branches. That
        must render as `checked=False`, never fall through to a scan that names a live branch's files
        as orphans — "could not look" is not "there was nothing there"."""
        # cast: the scan only calls fs.get_file_info, so the duck-typed double satisfies the contract
        # under test without being a real (C-extension, unsubclassable) pyarrow FileSystem.
        fs = cast(pafs.FileSystem, _RaisesOnLayoutProbe(pafs.LocalFileSystem(), f"{dataset}/tree"))

        result = orphans.scan_dataset(fs, dataset, prefix=dataset)

        assert result.checked is False
        assert result.orphans == []
        assert result.reason
