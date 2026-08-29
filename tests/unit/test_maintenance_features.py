"""The manifest feature-flag detector (#64), against REAL Lance datasets on the local filesystem.

Two things are being pinned, and they fail differently on purpose:

* **the field numbers.** ``reader_feature_flags``/``writer_feature_flags`` are read out of the
  Manifest protobuf by hand, because pylance exposes neither. If a future pylance reshuffles the
  manifest, ``test_feature_flags_read_the_documented_values`` fails LOUDLY here — rather than the
  detector silently reading some other field and either refusing a healthy estate or waving a
  shallow clone through.
* **the whitelist.** ``SUPPORTED`` is what makes the gate a gate. A test suite that only asserted
  "flag 16 is refused" would still pass if someone widened the mask and the refusal came from
  somewhere else, so the mask itself is asserted as a value.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable

import lance
import pyarrow as pa
import pytest
from lance.dataset import DatasetBasePath

from service_kit.lakehouse import features
from service_kit.lakehouse.objectfs import dataset_root_probe, is_lance_dataset_root, same_store_uri


def _table(n: int = 20) -> pa.Table:
    return pa.table({"id": pa.array(range(n), pa.int64()), "v": pa.array([f"v{i}" for i in range(n)])})


# --------------------------------------------------------------------------- #
# the parse
# --------------------------------------------------------------------------- #


def test_feature_flags_read_the_documented_values(tmp_path: pathlib.Path) -> None:
    """The four flag states a healthy estate actually produces, against the spec's own table
    (`lance_docs/file_format.md`: deletion files = 1, stable row ids = 2).

    Both fields are asserted, not just the reader's: the maintenance pass is a WRITER, and the spec
    says a writer checks `writer_feature_flags`. Reading only field 9 would look identical on every
    dataset here and be wrong on the one that mattered.
    """
    plain = str(tmp_path / "plain.lance")
    lance.write_dataset(_table(), plain)
    assert features.manifest_feature_flags(lance.dataset(plain)) == (0, 0), "proto3 omits a zero varint — absent must read as 'no flags'"

    deleted = str(tmp_path / "deleted.lance")
    lance.write_dataset(_table(), deleted).delete("id = 1")
    assert features.manifest_feature_flags(lance.dataset(deleted)) == (features.FLAG_DELETION_FILES, features.FLAG_DELETION_FILES)

    stable = str(tmp_path / "stable.lance")
    lance.write_dataset(_table(), stable, enable_stable_row_ids=True)
    assert features.manifest_feature_flags(lance.dataset(stable)) == (features.FLAG_STABLE_ROW_IDS, features.FLAG_STABLE_ROW_IDS)

    both = str(tmp_path / "both.lance")
    lance.write_dataset(_table(), both, enable_stable_row_ids=True).delete("id = 1")
    expected = features.FLAG_DELETION_FILES | features.FLAG_STABLE_ROW_IDS
    assert features.manifest_feature_flags(lance.dataset(both)) == (expected, expected)


def test_the_supported_mask_is_the_four_flags_a_rewrite_accounts_for() -> None:
    """`SUPPORTED` asserted as a VALUE, because widening it is the one edit that silently disarms
    every refusal in this service. 16 and 64 are named explicitly as excluded — those are the two
    that were measured to be unsafe."""
    assert features.SUPPORTED == 1 | 2 | 4 | 8
    assert not features.SUPPORTED & features.FLAG_BASE_PATHS, "shallow clones are rewritten into full copies — never supported by accident"
    assert not features.SUPPORTED & features.FLAG_DATA_OVERLAYS, "an ignored overlay returns stale cell values — a correctness bug"


def test_an_ordinary_dataset_is_not_refused(tmp_path: pathlib.Path) -> None:
    """The negative that makes every refusal below meaningful: a gate that refused everything would
    pass all of them. Deletion files + stable row ids are the flags a normal rask table carries."""
    uri = str(tmp_path / "ordinary.lance")
    lance.write_dataset(_table(), uri, enable_stable_row_ids=True).delete("id = 1")
    ds = lance.dataset(uri)

    assert features.manifest_feature_flags(ds) == (3, 3), "fixture must really set both supported flags"
    assert features.unsupported_features(ds) is None


# --------------------------------------------------------------------------- #
# flag 16 — base_paths. Genuinely unsafe in compaction today.
# --------------------------------------------------------------------------- #


def test_a_shallow_clone_sets_the_base_paths_flag(tmp_path: pathlib.Path) -> None:
    """A clone is metadata-only — it has NO `data/` directory at all, and resolves every file through
    the manifest's `base_paths` to the source root."""
    source = str(tmp_path / "src.lance")
    ds = lance.write_dataset(_table(), source)
    clone = str(tmp_path / "clone.lance")
    ds.shallow_clone(clone, reference=ds.version)

    assert not (tmp_path / "clone.lance" / "data").exists(), "fixture must really be a metadata-only clone"
    reader, writer = features.manifest_feature_flags(lance.dataset(clone))
    assert reader & features.FLAG_BASE_PATHS and writer & features.FLAG_BASE_PATHS

    reason = features.unsupported_features(lance.dataset(clone))
    assert reason is not None
    assert "16" in reason and "base_paths" in reason, f"the refusal must NAME the flag, got: {reason}"


def test_a_registered_but_unused_base_is_caught_by_the_flag_and_by_nothing_else(tmp_path: pathlib.Path) -> None:
    """The hole the flags close, and the reason the flag check is not redundant with the orphan
    scan's consequence check.

    `add_bases` registers an alternative base path that NO DataFile resolves through yet. Measured on
    pylance 9.0.0: every `DataFile.base_id` stays `None` and `tracked_files()` reports one base_uri —
    so nothing about the dataset's FILES looks different. The manifest flag is set all the same, and
    the very next write can place a file under that base.
    """
    uri = str(tmp_path / "based.lance")
    lance.write_dataset(_table(), uri)
    alt = tmp_path / "altbase"
    alt.mkdir()
    lance.dataset(uri).add_bases([DatasetBasePath(path=str(alt), name="alt")])

    ds = lance.dataset(uri)
    # The consequence a file-based detector would look for is genuinely absent …
    assert all(f.base_id is None for fr in ds.get_fragments() for f in fr.data_files()), "fixture assumption: no file resolves through the new base"
    # … while the manifest says plainly that this dataset is multi-base.
    assert features.manifest_feature_flags(ds) == (features.FLAG_BASE_PATHS, features.FLAG_BASE_PATHS)
    assert "16" in (features.unsupported_features(ds) or "")


# --------------------------------------------------------------------------- #
# flag 64 — data overlays. Refused by pylance's OWN open, which we must classify.
# --------------------------------------------------------------------------- #


def test_a_data_overlay_makes_pylance_refuse_the_open_and_we_classify_it(tmp_path: pathlib.Path, overlay_dataset: Callable[[pathlib.Path], str]) -> None:
    """Flag 64 is only ACCIDENTALLY safe today: pylance refuses the open, so our own manifest read
    never runs. Left unclassified that refusal is a generic `open:` error carrying a Rust source path
    from a GitHub CI runner — noise the sweep's lineage layer drops entirely.

    This pins the classification, so the day pylance gains overlay READ support the dataset opens,
    the manifest read runs, and `SUPPORTED` refuses it anyway. Either way it is a refusal.
    """
    uri = overlay_dataset(tmp_path)

    with pytest.raises(ValueError) as caught:
        lance.dataset(uri)
    assert "Flags: 64" in str(caught.value), "fixture must really commit a flag-64 overlay"

    reason = features.unsupported_features_from_open_error(caught.value)
    assert reason is not None, "pylance's own feature refusal must not read as an ordinary open failure"
    assert "64" in reason and "overlay" in reason.lower(), f"the refusal must NAME the flag, got: {reason}"


def test_an_ordinary_open_failure_is_not_a_feature_refusal(tmp_path: pathlib.Path) -> None:
    """The negative, and it is load-bearing: a missing directory must keep reading as `open:` noise.

    The sweep's lineage selection is prefix-keyed on `open:` / `maintain:`, and the estate's buckets
    contain declared-only prefixes that never open. Classifying those as feature refusals would make
    the refusal counter — the one signal that a pylance upgrade silently disarmed maintenance —
    permanently non-zero and therefore useless.
    """
    try:
        lance.dataset(str(tmp_path / "nope.lance"))
    except Exception as exc:  # noqa: BLE001 — pylance's error type for a missing dataset is not the point
        assert features.unsupported_features_from_open_error(exc) is None
    else:
        pytest.fail("opening a missing dataset must raise")


def test_a_wire_type_it_cannot_skip_stops_rather_than_misreading() -> None:
    """The parser must never invent flags out of bytes it does not understand — a garbage reader
    value would refuse a healthy dataset forever, and the whole estate with it."""

    class _Manifest:
        def serialized_manifest(self) -> bytes:
            # field 3, wire type 3 (group start — removed from proto3 and unskippable here)
            return bytes([3 << 3 | 3, 0xFF, 0xFF])

    # A `ManifestCarrier`, structurally: the one private attribute the detector reads is all it declares.
    class _Dataset:
        _ds = _Manifest()

    assert features.manifest_feature_flags(_Dataset()) == (0, 0)


# --------------------------------------------------------------------------- #
# flag 16, the OTHER half: an external blob BASE is not a shallow clone.
# --------------------------------------------------------------------------- #


def test_the_manifest_says_WHICH_kind_of_base_a_dataset_declares(tmp_path: pathlib.Path) -> None:
    """`BasePath.is_dataset_root` — the discriminator, measured off real manifest bytes.

    Flag 16 means "this dataset spans bases" and says nothing about WHAT the bases are, which is why
    one bit could not carry both answers. Two shapes set it and they are not the same object:

      * a SHALLOW CLONE's base is another DATASET's root, and every one of the clone's data files
        resolves through it — compacting the clone materialises that dataset's bytes into its own root;
      * an INGEST bronze table's base is a plain object-store prefix where the external blobs already
        live (`ingest/lander.py::create_empty` registers exactly one through `initial_bases`), and the
        dataset's own data files sit under its own root like any other table's.

    Measured on pylance 10.0.0, submessage bytes: the clone's carries field 3 (`is_dataset_root`) set
    to 1, the external base's omits it (proto3 drops a false) and carries a `name` instead.
    """
    source = str(tmp_path / "src.lance")
    src = lance.write_dataset(_table(), source)
    clone = str(tmp_path / "clone.lance")
    src.shallow_clone(clone, reference=src.version)

    external = tmp_path / "external"
    external.mkdir()
    based = str(tmp_path / "bronze.lance")
    lance.write_dataset(_table(), based, initial_bases=[DatasetBasePath(str(external), "external")])

    clone_refs = features.manifest_base_path_refs(lance.dataset(clone))
    assert [(ref.path, ref.is_dataset_root) for ref in clone_refs] == [(source, True)]

    base_refs = features.manifest_base_path_refs(lance.dataset(based))
    assert [(ref.path, ref.is_dataset_root) for ref in base_refs] == [(str(external), False)]

    # …and the paths-only view every caller already uses stays exactly what it was.
    assert features.manifest_base_paths(lance.dataset(clone)) == [source]


def test_compaction_is_PERMITTED_on_an_external_blob_base_and_still_refused_on_a_clone(tmp_path: pathlib.Path) -> None:
    """The cost refusal must land on the case it was measured on, and on no other.

    The blanket flag-16 exclusion was justified by ONE measurement — a pristine clone going
    1,072 -> 108,199 bytes against a 119,693-byte base, because compaction materialises the shared
    data into the clone's own root. That reasoning does not reach a dataset whose base is an external
    blob prefix: its data files were always its own, so compaction merges its own fragments and copies
    nothing. Measured on pylance 10.0.0 for the blob case: 4 fragments -> 1, 9,445 -> 14,366 bytes,
    the base directory byte-identical, 20/20 payloads still resolving.

    With the two folded together, every ingest bronze table and every medallion tier — the rows with
    the most fragments in the estate — accumulated fragments forever while the sweep reported success.

    Driven through the SHIPPED gatherer with the SHIPPED object-store probe, so the two halves cannot
    agree in a test and disagree in the sweep.
    """
    external = tmp_path / "external"
    external.mkdir()
    based = str(tmp_path / "bronze.lance")
    lance.write_dataset(_table(), based, initial_bases=[DatasetBasePath(str(external), "external")])
    ds = lance.dataset(based)
    assert features.manifest_feature_flags(ds) == (16, 16), "fixture must really set flag 16"

    probe = dataset_root_probe(based, {})

    reader, writer = features.manifest_feature_flags(ds)
    assert features.describe_compaction_unsupported_flags(reader, writer, features.gather_compaction_bases(ds, probe)) is None

    source = str(tmp_path / "src.lance")
    src = lance.write_dataset(_table(), source)
    clone = str(tmp_path / "clone.lance")
    src.shallow_clone(clone, reference=src.version)
    cds = lance.dataset(clone)
    refusal = features.describe_compaction_unsupported_flags(
        *features.manifest_feature_flags(cds), features.gather_compaction_bases(cds, dataset_root_probe(clone, {}))
    )
    assert refusal is not None and "16" in refusal, f"the clone refusal must survive, got: {refusal}"


def test_the_OBJECT_STORE_probe_is_what_separates_a_clone_from_a_blob_prefix(tmp_path: pathlib.Path) -> None:
    """Why the gate reads a listing instead of trusting `BasePath.is_dataset_root`.

    Measured on pylance 10.0.0: `shallow_clone` is the ONLY writer that sets that bit. `add_bases`
    pointed straight at a live Lance dataset root reports False — so a gate reading the manifest alone
    would permit the clone shape wearing the blob shape's manifest. `<base>/_versions/` does not lie.
    """
    source = str(tmp_path / "src.lance")
    lance.write_dataset(_table(), source)
    based = str(tmp_path / "registered.lance")
    lance.write_dataset(_table(), based)
    lance.dataset(based).add_bases([DatasetBasePath(path=source, name="src")])
    ds = lance.dataset(based)

    refs = features.manifest_base_path_refs(ds)
    assert [ref.is_dataset_root for ref in refs] == [False], "the manifest bit would have been enough after all — this test's premise is gone"
    assert is_lance_dataset_root(source, {}) is True
    assert is_lance_dataset_root(str(tmp_path), {}) is False

    gathered = features.gather_compaction_bases(ds, dataset_root_probe(str(ds.uri), {}))
    assert [base.probed_dataset_root for base in gathered.bases] == [True]
    assert features.describe_compaction_unsupported_flags(*features.manifest_feature_flags(ds), gathered) is not None


def test_DATA_LIVING_UNDER_A_BASE_refuses_even_when_the_base_is_no_dataset_root(tmp_path: pathlib.Path) -> None:
    """The third reading, and the shape neither of the other two can see.

    `write_dataset(..., target_bases=["alt"])` lands this dataset's own data files under a registered
    base which is not declared a dataset root and has no `_versions/` — and compacting pulls them home
    (measured on pylance 10.0.0: local root 3,540 -> 5,991 bytes, the base's three files orphaned).
    `DataFile.base_id` is the only signal that says so.
    """
    alt = tmp_path / "altbase"
    alt.mkdir()
    uri = str(tmp_path / "targeted.lance")
    lance.write_dataset(_table(), uri, initial_bases=[DatasetBasePath(str(alt), "alt")])
    lance.write_dataset(_table(), uri, mode="append", target_bases=["alt"])
    ds = lance.dataset(uri)

    gathered = features.gather_compaction_bases(ds, dataset_root_probe(str(ds.uri), {}))
    assert gathered.bases == [features.BaseEvidence(path=str(alt), declares_dataset_root=False, probed_dataset_root=False)], (
        "neither the manifest bit nor the listing calls this base a dataset root — that is the premise"
    )
    assert gathered.data_resolves_through_a_base is True
    assert features.describe_compaction_unsupported_flags(*features.manifest_feature_flags(ds), gathered) is not None


def test_EVERY_UNREADABLE_reading_keeps_the_flag_16_refusal() -> None:
    """The whitelist is loud on purpose, so the allowance only applies where the evidence is present.

    Four ways to not know, and every one of them refuses. The asymmetry is deliberate and stated at
    the gate: a wrong refusal costs disk and a counted line in the sweep summary; a wrong permit costs
    a clone its whole reason to exist.
    """
    external = features.BaseEvidence(path="/x/external", probed_dataset_root=False)

    # (1) no evidence gathered at all.
    assert features.describe_compaction_unsupported_flags(16, 16, None) is not None
    # (2) flag 16 set, yet no BasePath the walker could parse — a renumbered field, a wire type it stops on.
    assert features.describe_compaction_unsupported_flags(16, 16, features.CompactionBases(bases=[], data_resolves_through_a_base=False)) is not None
    # (3) the object store could not answer for a base.
    unprobed = features.CompactionBases(bases=[features.BaseEvidence(path="/x/external", probed_dataset_root=None)], data_resolves_through_a_base=False)
    assert features.describe_compaction_unsupported_flags(16, 16, unprobed) is not None
    # (4) the fragments could not be read, so whether our data lives under the base is unknown.
    unread = features.CompactionBases(bases=[external], data_resolves_through_a_base=None)
    assert features.describe_compaction_unsupported_flags(16, 16, unread) is not None

    # A mixed manifest is a clone as far as this gate is concerned: one dataset-root base is enough.
    mixed = features.CompactionBases(
        bases=[external, features.BaseEvidence(path="/x/src.lance", declares_dataset_root=True, probed_dataset_root=True)],
        data_resolves_through_a_base=False,
    )
    assert features.describe_compaction_unsupported_flags(16, 16, mixed) is not None
    # And an unrelated unknown flag is never waved through by the base-path allowance.
    clean = features.CompactionBases(bases=[external], data_resolves_through_a_base=False)
    assert features.describe_compaction_unsupported_flags(16 | 64, 16 | 64, clean) is not None
    assert features.describe_compaction_unsupported_flags(16, 16, clean) is None, "the permitted case must still be permitted"


def test_a_gatherer_whose_probe_raises_records_the_unknown_rather_than_raising(tmp_path: pathlib.Path) -> None:
    """`gather_compaction_bases` runs inside `compact_one`'s pass, where a raise would be reported as a
    per-dataset ERROR — "something failed" — rather than as the REFUSAL it is. It records `None`, and
    the gate turns that into a refusal that names the base it could not read."""
    external = tmp_path / "external"
    external.mkdir()
    based = str(tmp_path / "bronze.lance")
    lance.write_dataset(_table(), based, initial_bases=[DatasetBasePath(str(external), "external")])
    ds = lance.dataset(based)

    def exploding(path: str) -> bool:
        raise OSError("the endpoint is unreachable")

    gathered = features.gather_compaction_bases(ds, exploding)

    assert [base.probed_dataset_root for base in gathered.bases] == [None]
    refusal = features.describe_compaction_unsupported_flags(*features.manifest_feature_flags(ds), gathered)
    assert refusal is not None and str(external) in refusal, f"the refusal must name the base it could not read: {refusal}"


def test_a_base_is_probed_in_the_DATASETS_OWN_store_never_the_local_filesystem() -> None:
    """The respelling between the manifest and the probe, and why leaving it out fails OPEN.

    A Lance manifest states a base as the object store reads it: on S3 that is the schemeless
    `/bucket/ns/src.lance`, while the sweep holds `s3://bucket/ns/t.lance` — the two spellings
    `base_refs.normalise` exists to reconcile for COMPARISON. A probe cannot use that one, it has to
    hand a resolver something resolvable. Handed the schemeless form, `pyarrow.fs` reads it as a LOCAL
    absolute path, finds nothing there, and answers "not a dataset root" — which for a real shallow
    clone is a wrong PERMIT, the one direction this gate must never take.

    A base in a store the dataset does not live in RAISES, and `gather_compaction_bases` turns that
    into `probed_dataset_root=None` — unknown reaching the gate as unknown.
    """
    assert same_store_uri("s3://bucket/ns/t.lance", "/bucket/ns/src.lance") == "s3://bucket/ns/src.lance"
    assert same_store_uri("s3://bucket/ns/t.lance", "s3://bucket/ns/src.lance") == "s3://bucket/ns/src.lance"
    assert same_store_uri("/tmp/a.lance", "/tmp/b") == "/tmp/b"

    with pytest.raises(ValueError):
        same_store_uri("s3://bucket/x", "gs://other/y")
    with pytest.raises(ValueError):
        same_store_uri("/tmp/a.lance", "s3://bucket/y")


def test_a_base_in_another_store_is_UNKNOWN_and_therefore_refused(tmp_path: pathlib.Path) -> None:
    """The end of that path, through the shipped binding: a base the probe cannot answer for is
    recorded as None and the gate turns None into a refusal — never into a permit.

    Offline on purpose. The unreconcilable spelling raises inside `dataset_root_probe` before any
    filesystem is touched, which is the behaviour that matters: no store is consulted about a path
    that does not belong to it.
    """
    probe = dataset_root_probe("s3://bucket/ns/t.lance", {})
    with pytest.raises(ValueError):
        probe("gs://elsewhere/blobs")

    unreadable = features.CompactionBases(
        bases=[features.BaseEvidence(path="gs://elsewhere/blobs", probed_dataset_root=None)],
        data_resolves_through_a_base=False,
    )
    refusal = features.describe_compaction_unsupported_flags(16, 16, unreadable)
    assert refusal is not None and "gs://elsewhere/blobs" in refusal, f"the refusal must name the base it could not read: {refusal}"

    # And the probe DOES answer, offline, for a base that really is in the dataset's own store.
    source = str(tmp_path / "src.lance")
    lance.write_dataset(_table(), source)
    assert dataset_root_probe(str(tmp_path / "clone.lance"), {})(source) is True
    assert dataset_root_probe(str(tmp_path / "clone.lance"), {})(str(tmp_path)) is False
