"""Unreferenced-file detection, against REAL Lance datasets on the local filesystem.

Deliberately not faked: the whole risk in this pass is disagreeing with what Lance actually writes,
and a double that agrees with my assumptions would prove only that I am self-consistent. Every
fixture here is a real dataset built with `lance.write_dataset`, so the layout under test is the
layout Lance produces (`data/`, `_deletions/`, `_transactions/`, `_versions/`).

The load-bearing property is asymmetric: reporting a live file as an orphan is catastrophic (a
reclaimer would delete a table's data), while missing one is merely untidy. So most of these pin the
NEGATIVE — that nothing live is ever named.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any

import lance
import pyarrow as pa
import pyarrow.fs as pafs
import pytest
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


def test_a_healthy_dataset_reports_no_data_or_deletion_orphans(tmp_path: pathlib.Path) -> None:
    """The baseline that makes every other finding meaningful — but NOT "reports nothing".

    A freshly written dataset always carries `_transactions/*.txn`, because the spec keeps a
    transaction file per commit attempt and nothing prunes them. So "healthy" means no orphaned DATA
    and no orphaned DELETION vectors — the two kinds whose reclamation would lose rows. Writing this
    as `orphans == []` fails on a correct implementation, which is how a true finding gets mistaken
    for a bug in the detector.
    """
    uri, prefix = _dataset(tmp_path)
    result = orphans.scan_dataset(_fs(), uri, prefix)
    assert result.checked
    dangerous = [o.path for o in result.orphans if o.kind in ("data", "deletions", "indices")]
    assert dangerous == [], f"a healthy dataset must have no reclaimable data, got {dangerous}"


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

    result = orphans.scan_dataset(_fs(), uri, prefix)
    assert result.checked
    assert [o.path for o in result.orphans if o.kind == "deletions"] == []


# --------------------------------------------------------------------------- #
# what it DOES find
# --------------------------------------------------------------------------- #


def test_a_stray_data_file_is_reported(tmp_path: pathlib.Path) -> None:
    """The partially-failed write: fragments on disk, commit never landed, so no manifest names them.
    Nothing in the estate reclaims these and nothing else reports them."""
    uri, prefix = _dataset(tmp_path)
    stray = tmp_path / "t.lance" / "data" / "00000000000000000000000000deadbeef.lance"
    stray.write_bytes(b"fragments from a write whose commit never landed")

    result = orphans.scan_dataset(_fs(), uri, prefix)
    found = {o.path: o for o in result.orphans}
    assert "data/00000000000000000000000000deadbeef.lance" in found
    assert found["data/00000000000000000000000000deadbeef.lance"].kind == "data"
    assert found["data/00000000000000000000000000deadbeef.lance"].size_bytes > 0


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

    result = orphans.scan_dataset(_fs(), uri, prefix)
    reported = {o.path for o in result.orphans if o.kind == "transactions"}
    assert reported == {f"_transactions/{orphaned.name}"}, (
        "only the UNREFERENCED txn is garbage — a txn belonging to a live version is the provenance "
        f"of a manifest being read, and reporting it would be reporting live data. live={live}"
    )


def test_manifests_and_the_version_hint_are_never_reported(tmp_path: pathlib.Path) -> None:
    """A manifest IS what makes a version live, so it can never be "unreferenced" while present; the
    hint is disposable but written every commit. Reporting either every single run is how a report
    teaches its reader to skip it — the failure mode that makes a correct detector useless."""
    uri, prefix = _dataset(tmp_path)
    hint = tmp_path / "t.lance" / "_versions" / "latest_version_hint.json"
    assert hint.exists(), "fixture must actually have the hint file"

    result = orphans.scan_dataset(_fs(), uri, prefix)
    assert [o.path for o in result.orphans if o.kind == "versions"] == []


# --------------------------------------------------------------------------- #
# unreadable is not clean
# --------------------------------------------------------------------------- #


def test_an_unreadable_dataset_yields_no_orphans_and_says_why(tmp_path: pathlib.Path) -> None:
    """ "We could not determine the referenced set" must never render as "none of these files are
    referenced" — that shape is what deletes a live table. `checked=False` with a reason, and the
    orphan list EMPTY BY CONSTRUCTION."""
    result = orphans.scan_dataset(_fs(), str(tmp_path / "not-a-dataset"), str(tmp_path / "not-a-dataset"))
    assert result.checked is False
    assert result.orphans == []
    assert result.reason


def test_an_unreadable_dataset_does_not_quietly_shrink_the_report(tmp_path: pathlib.Path) -> None:
    """Aggregated, an unreadable dataset must INCREASE the incomplete count rather than silently
    lower the orphan total — otherwise a bucket nobody can read reports as the cleanest one."""
    uri, prefix = _dataset(tmp_path)
    (tmp_path / "t.lance" / "data" / "0000000000000000000000000000000abc.lance").write_bytes(b"stray")
    missing = str(tmp_path / "gone.lance")

    report = orphans.scan_datasets(_fs(), [(uri, prefix), (missing, missing)])
    assert report.datasets_scanned == 1
    assert report.datasets_unreadable == 1
    assert report.incomplete, "an unreadable dataset must be named, not absorbed"
    assert report.total == len(report.orphans) >= 1


# --------------------------------------------------------------------------- #
# report-only, mechanically
# --------------------------------------------------------------------------- #


def test_the_module_contains_no_delete_call() -> None:
    """The property the whole design rests on. Asserted against the SOURCE because a behavioural test
    can only prove the paths it happens to exercise, and this must hold on every path."""
    # Match CALL syntax, not the bare word: the module's docstring necessarily NAMES the destructive
    # operations in order to explain that it does not perform them, and a substring check on the word
    # alone fails on its own documentation. (Same trap caught earlier on `provision`.)
    src = pathlib.Path(orphans.__file__).read_text()
    for verb in (
        "delete_file(",
        "delete_dir(",
        "delete_objects(",
        "delete_bucket(",
        "cleanup_old_versions(",
        "unlink(",
        "rmtree(",
        "open_output_stream(",
        "compact_files(",
        "optimize_indices(",
    ):
        assert verb not in src, f"the orphan pass calls {verb} — it must only REPORT"


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


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("data/x.lance", "data"),
        ("_deletions/0-1-2.arrow", "deletions"),
        ("_indices/abc-uuid/index.idx", "indices"),
        ("_transactions/3-uuid.txn", "transactions"),
        ("_versions/9.manifest", "versions"),
        ("something-else.bin", "other"),
    ],
)
def test_every_lance_owned_area_is_classified(rel: str, expected: str) -> None:
    """A finding without a kind is a finding a reader cannot triage — `data/` is potential data loss,
    `_transactions/` is expected accumulation, and they warrant different responses."""
    assert orphans._kind_of(rel) == expected


def test_a_tag_is_never_reported_as_an_orphan(tmp_path: pathlib.Path) -> None:
    """Tags PIN versions — `_refs/tags/*.json` is live metadata, not residue.

    Caught by the first live run, which reported every `publish-*` promotion tag in the estate as an
    orphan. That is the precise failure this module exists to avoid: a reclaimer acting on it would
    unpin published data, and `cleanup_old_versions` (which exempts tagged versions) would then be
    free to collect the versions those tags were protecting.
    """
    uri, prefix = _dataset(tmp_path)
    lance.dataset(uri).tags.create("blessed", 1)
    tags = list((tmp_path / "t.lance" / "_refs" / "tags").iterdir())
    assert tags, "fixture must actually write a tag file"

    result = orphans.scan_dataset(_fs(), uri, prefix)
    named = {o.path for o in result.orphans}
    assert not any(p.startswith("_refs/") for p in named), f"a tag was reported as an orphan: {named}"


def test_the_reserved_marker_is_never_reported(tmp_path: pathlib.Path) -> None:
    """`.lance-reserved` is a zero-byte structural marker at the dataset root, never named by a
    manifest — so a naive scan reports it once per dataset, every run, forever."""
    uri, prefix = _dataset(tmp_path)
    (tmp_path / "t.lance" / ".lance-reserved").write_bytes(b"")
    result = orphans.scan_dataset(_fs(), uri, prefix)
    assert ".lance-reserved" not in {o.path for o in result.orphans}


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

    named = {o.path for o in orphans.scan_dataset(_fs(), uri, prefix).orphans}
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

    result = orphans.scan_dataset(_fs(), uri, uri)
    assert result.checked is False, "a branched dataset must be refused, not scanned"
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

    result = orphans.scan_dataset(_fs(), clone, clone)
    assert result.checked is False, "a dataset spanning base_paths must be refused"
    assert result.orphans == []
    assert "base_paths" in (result.reason or "")


def test_an_ordinary_dataset_is_still_scanned(tmp_path: pathlib.Path) -> None:
    """The gate must refuse only the two hazardous layouts. A gate that refuses everything is a
    detector that does nothing, and would pass both tests above."""
    uri, prefix = _dataset(tmp_path)
    result = orphans.scan_dataset(_fs(), uri, prefix)
    assert result.checked is True
    assert result.reason is None


def test_a_refused_dataset_does_not_read_as_clean_in_the_aggregate(tmp_path: pathlib.Path) -> None:
    """Refusing must INCREASE `datasets_unreadable` and land in `incomplete` — never quietly lower
    the orphan count. Otherwise the branchiest bucket in the estate reports as the tidiest."""
    uri = str(tmp_path / "branched.lance")
    ds = lance.write_dataset(_table(), uri)
    ds.create_branch("feature-a")

    report = orphans.scan_datasets(_fs(), [(uri, uri)])
    assert report.datasets_scanned == 0
    assert report.datasets_unreadable == 1
    assert report.total == 0
    assert report.incomplete and "tree/" in report.incomplete[0]


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

    result = orphans.scan_dataset(_fs(), uri, prefix)
    assert result.checked is False
    assert result.orphans == []
    assert "_mem_wal/" in (result.reason or "")
    assert "fencing" in (result.reason or "")


def test_a_dataset_using_overlays_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """An overlay writes new cell values to `data/overlay-<uuid>.lance` — INSIDE `data/`, where a
    false positive deletes real values — and is referenced from `DataFragment.overlays`, which
    `data_files()` does not report.

    Overlays are experimental and unwritable by this pylance, so the fragment is faked at exactly the
    seam the reader uses. Feature flag 64 settles the behaviour: a reader that does not understand
    overlays must REFUSE, because ignoring one returns stale base values — "a correctness bug rather
    than a degraded experience".
    """
    uri, prefix = _dataset(tmp_path)
    real = lance.dataset(uri)

    class _FragmentWithOverlay:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.metadata = type("MD", (), {"overlays": [object()], "deletion_file": None})()
            self.fragment_id = 0

        def data_files(self) -> list:
            return self._inner.data_files()

    class _DatasetWithOverlay:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def versions(self) -> list:
            return self._inner.versions()

        def get_fragments(self) -> list:
            return [_FragmentWithOverlay(f) for f in self._inner.get_fragments()]

        def checkout_version(self, _version: int) -> _DatasetWithOverlay:
            # The #102 walk checks out versions on the HEAD handle; every version of this stand-in
            # carries the overlay, so the refusal must fire regardless of which one is inspected.
            return self

    monkeypatch.setattr(orphans.lance, "dataset", lambda *a, **k: _DatasetWithOverlay(real))

    result = orphans.scan_dataset(_fs(), uri, prefix)
    assert result.checked is False, "a dataset using overlays must be refused, not scanned"
    assert result.orphans == []
    assert "overlay" in (result.reason or "").lower()
    assert "64" in (result.reason or "")


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

    result = orphans.scan_dataset(_fs(), uri, uri)

    assert result.checked is False, "a multi-base dataset must be refused — a prefix listing cannot be subtracted"
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

    result = orphans.scan_dataset(_fs(), uri, uri)

    assert result.checked is False, "an overlay dataset must be refused, not scanned"
    assert result.orphans == []
    assert "64" in (result.reason or "") and "overlay" in (result.reason or "").lower(), result.reason


def test_an_ordinary_dataset_is_not_refused_by_the_flag_gate(tmp_path: pathlib.Path) -> None:
    """The negative for the flag gate specifically: deletion files + stable row ids are supported
    flags, and a dataset carrying both must still be scanned. A gate keyed on "any flag at all"
    would refuse most of a real estate."""
    uri = str(tmp_path / "flagged.lance")
    lance.write_dataset(_table(), uri, enable_stable_row_ids=True)
    lance.dataset(uri).delete("id = 1")

    result = orphans.scan_dataset(_fs(), uri, uri)

    assert result.checked is True, result.reason
    assert result.reason is None


def test_the_transaction_of_a_LIVE_version_is_not_an_orphan(tmp_path: pathlib.Path) -> None:
    """Nothing ever added `_transactions/*.txn` to the referenced set, so EVERY txn file on disk was
    reported as garbage — including the one that produced the version being read.

    The module docstring is right that txn files from FAILED or rolled-back commits accumulate by
    design and nothing prunes them. It does not follow that every txn is garbage: the ones belonging
    to live versions are the provenance of the manifests. The baseline test above quietly encodes the
    old behaviour by filtering transactions out of "healthy", which is how this survived.

    MEASURED live 2026-08-16: `s3://lance-catalog/bronze/pages` had exactly ONE live version and
    exactly ONE txn file, and the scan called that file an orphan. Estate-wide it was 34 of 56
    `orphan_files` — which is why the count could never reach zero and the #79 purge gate could never
    certify, on an estate with no actual garbage.
    """
    uri, prefix = _dataset(tmp_path)
    # A second commit, so there are two live versions and two transaction files.
    lance.write_dataset(pa.table({"v": [9, 9]}), uri, mode="overwrite")

    result = orphans.scan_dataset(_fs(), uri, prefix)

    assert result.checked
    txn_orphans = [o.path for o in result.orphans if o.kind == "transactions"]
    assert txn_orphans == [], f"the transactions of live versions were reported as orphans: {txn_orphans}"


def test_a_STALE_transaction_is_still_reported(tmp_path: pathlib.Path) -> None:
    """The fix references live transactions only — it must not blanket-exempt the directory.

    A txn from a failed or rolled-back commit belongs to no live version and IS the accumulation the
    module docstring describes. Referencing every `.txn` would trade a false positive for a blind
    spot, which is the worse of the two for a reclaimer's input.
    """
    uri, prefix = _dataset(tmp_path)
    stale = pathlib.Path(prefix) / "_transactions" / "999-deadbeef-0000-0000-0000-000000000000.txn"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"not a live commit")

    result = orphans.scan_dataset(_fs(), uri, prefix)

    txn_orphans = [o.path for o in result.orphans if o.kind == "transactions"]
    assert any("999-deadbeef" in p for p in txn_orphans), f"a stale transaction must still be reported, got {txn_orphans}"
