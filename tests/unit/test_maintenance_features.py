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

    class _Dataset:
        _ds = _Manifest()

    assert features.manifest_feature_flags(_Dataset()) == (0, 0)  # ty: ignore[invalid-argument-type] — a stand-in for the one private attribute read
