"""Real-Lance tests for version expiry's refusals and for the flag-16 compaction gate the due evaluation applies.

Maintenance compacts nothing (ADR 0155); it decides. Two gates come out of the manifest's feature flags (#64): the
reclamation gate (`SUPPORTED_FOR_GC`, which admits flag 16 because `cleanup_old_versions` is root-scoped) in front of
expiry, and the compaction gate in front of listing a table as due for compaction, which asks about the BASES: a
shallow clone's compaction would materialise the shared data into the clone, while a table with one external blob
prefix compacts as an ordinary merge. Local filesystem, no S3.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa
from lance.blob import Blob

from maintenance.services.due import DueQuestion, compaction_due
from maintenance.services.expiry import DatasetResult, ExpiryPolicy, ExpiryTarget, expire_versions
from service_kit.lakehouse.maintenance_due import CompactionDue


def _expire(uri: str, *, older_than: timedelta | None = None, retain_versions: int | None = None) -> DatasetResult:
    return expire_versions(ExpiryTarget(uri=uri, storage_options={}), ExpiryPolicy(older_than=older_than, retain_versions=retain_versions))


def _compaction_due(uri: str) -> CompactionDue | None:
    return compaction_due(lance.dataset(uri), DueQuestion(table="ns$t", location=uri), {})


def _fragmented_indexed_dataset(root: Path) -> str:
    """A local Lance dataset with a BTREE index and several small fragments (each append = 1 fragment)."""
    uri = str(root / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(100), pa.int64())}), uri)
    ds = lance.dataset(uri)
    ds.create_scalar_index("id", "BTREE")
    for i in range(4):
        base = 100 + i * 10
        lance.write_dataset(pa.table({"id": pa.array(range(base, base + 10), pa.int64())}), uri, mode="append")
    return uri


def test_expiry_open_error_prefix_for_a_missing_dataset(tmp_path: Path) -> None:
    # The sweep's FAIL selection keys on these prefixes: an unopenable dir is "open:" (transient
    # non-dataset noise → never a FAIL event). Pin the prefix so a reword can't silently flip selection.
    result = _expire(str(tmp_path / "nope.lance"), older_than=timedelta(days=7))
    assert result.error is not None and result.error.startswith("open:")


def test_sweep_buckets_unions_primary_and_extras() -> None:
    """GC must cover EVERY bucket that can hold Lance data (audit 2026-07-14).

    The sweep discovered exactly ONE bucket, so every #3-A per-warehouse bucket and #3-B multi-base data
    bucket was invisible to it: their tables accumulated superseded manifest versions and small fragments
    forever. A storage leak created by the very features that introduce new buckets.
    """
    from maintenance.core.config import MaintenanceSettings

    s = MaintenanceSettings.model_validate({"s3_bucket": "lance-catalog", "s3_extra_buckets": "lance-source, s3://mb-a/, lance-catalog, "})
    # primary first, extras normalized (s3:// + slashes stripped), de-duplicated, empties dropped
    assert s.sweep_buckets == ["lance-catalog", "lance-source", "mb-a"]

    bare = MaintenanceSettings.model_validate({"s3_bucket": "only"})
    assert bare.sweep_buckets == ["only"]  # no extras => unchanged single-bucket behavior


# --------------------------------------------------------------------------- #
# #64 — the feature-flag gates. Every fixture below is a REAL dataset carrying a REAL manifest flag:
# compacting a shallow clone was measured to leave the clone holding its own full copy of data it had
# only referenced, so a clone is never listed as due for compaction.
# --------------------------------------------------------------------------- #


def test_a_shallow_clone_is_not_due_for_compaction_and_its_versions_are_still_expired(tmp_path: Path) -> None:
    """Flag 16 gates COMPACTION alone, and for cost: compacting a clone materialises the shared data into its own root
    (1,072 -> 108,199 bytes against a 119,693-byte base, measured). Reclamation is root-scoped, so it is safe on a clone:
    one `cleanup_old_versions` on a clone with dead fragments on both sides removed the 2 clone-owned files and left all
    4 base-owned ones. MEASURED on this estate 2026-08-16: a blanket flag-16 refusal stopped all of it, and the 17
    refused datasets were exactly the ones with version history.
    """
    source = str(tmp_path / "src.lance")
    ds = lance.write_dataset(pa.table({"v": list(range(64))}), source, max_rows_per_file=16)
    clone = str(tmp_path / "clone.lance")
    ds.shallow_clone(clone, reference=ds.version)
    for extra in range(3):
        lance.write_dataset(pa.table({"v": [extra]}), clone, mode="append")
    assert len(lance.dataset(clone).get_fragments()) > 1, "fixture must give a planner something to merge"

    due = _compaction_due(clone)
    result = _expire(clone, retain_versions=1)

    assert due is None, f"a shallow clone was listed for compaction: {due}"
    assert (result.refused, result.error) == (None, None), (result.refused, result.error)
    assert result.old_versions_removed > 0, "the clone got no version reclamation"


def test_a_table_with_an_external_blob_base_IS_due_for_compaction(tmp_path: Path) -> None:
    """The over-refusal this test exists to kill. A table registering ONE external blob base through `initial_bases`
    sets flag 16 exactly as a shallow clone does, and a flags-only gate refused it, so such tiers accumulated
    fragments forever (`fragments_removed_total=0` over 785 ticks on the live estate). Its own data files are under its
    own root (every `DataFile.base_id` is None), so compacting it is a merge: MEASURED on pylance 10.0.0, 4 fragments
    -> 1, the base directory byte-identical, 20/20 payloads still resolving.
    """
    payloads = tmp_path / "payloads"
    payloads.mkdir()
    uris = []
    for i in range(20):
        blob = payloads / f"page-{i:03d}.bin"
        blob.write_bytes(b"X" * 4096)
        uris.append(blob.resolve().as_uri())

    def chunk(lo: int, hi: int) -> pa.Table:
        return pa.table({"id": pa.array(range(lo, hi), pa.int64()), "payload": lance.blob_array([Blob.from_uri(uris[i]) for i in range(lo, hi)])})

    uri = str(tmp_path / "bronze.lance")
    lance.write_dataset(
        chunk(0, 5),
        uri,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        initial_bases=[lance.DatasetBasePath(str(payloads.resolve()), "payloads")],
        external_blob_mode="reference",
    )
    for lo in (5, 10, 15):
        lance.write_dataset(chunk(lo, lo + 5), uri, mode="append", data_storage_version="2.2", external_blob_mode="reference")
    assert len(lance.dataset(uri).get_fragments()) == 4, "fixture must really be fragmented"

    due = _compaction_due(uri)

    assert due is not None, "the external-blob-base layout was refused compaction"
    assert (due.fragments, due.candidate_fragments) == (4, 4), due


def test_expiry_refuses_a_table_whose_data_files_live_on_a_data_base(tmp_path: Path) -> None:
    """The half of `add_bases` that is genuinely the clone hazard, and the one the manifest's own
    `BasePath.is_dataset_root` bit CANNOT see.

    `write_dataset(..., target_bases=["alt"])` lands this dataset's data files under the registered
    base. The manifest still reports `is_dataset_root=False` and the base directory still has no
    `_versions/` — it is not a dataset root by either reading — yet the files are foreign-resident
    and compaction pulls them home. MEASURED on pylance 10.0.0 with this exact fixture: local root
    3,540 -> 5,991 bytes, the base's three data files left behind as garbage, every surviving
    `base_id` None.

    So `DataFile.base_id` is a third, independent signal. A plain base outside the root holding data
    files is a DATA base, which may be read only under its own credential ([[LH-273]]), so expiry
    refuses it and touches nothing.
    """
    alt = tmp_path / "altbase"
    alt.mkdir()
    uri = str(tmp_path / "targeted.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(10), pa.int64())}), uri, initial_bases=[lance.DatasetBasePath(str(alt), "alt")])
    for i in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(10 + i * 5, 15 + i * 5), pa.int64())}), uri, mode="append", target_bases=["alt"])
    assert any(df.base_id is not None for f in lance.dataset(uri).get_fragments() for df in f.data_files()), "fixture must really put data under the base"
    base_files_before = sorted(p.name for p in alt.iterdir())
    version_before = lance.dataset(uri).version

    result = _expire(uri, older_than=timedelta(0))

    assert result.refused_by == "foreign_data_base", f"the refusal must name its gate: {result}"
    assert result.error is None
    assert result.old_versions_removed == 0
    assert lance.dataset(uri).version == version_before, "a base-resident dataset was committed to anyway"
    assert sorted(p.name for p in alt.iterdir()) == base_files_before, "the base's files were touched"


def test_a_registered_base_that_IS_A_LANCE_DATASET_ROOT_is_not_due_for_compaction(tmp_path: Path) -> None:
    """Why the gate PROBES object storage instead of trusting the manifest's self-report.

    `add_bases` pointed at another dataset's root produces `BasePath(is_dataset_root=False)` —
    measured on pylance 10.0.0, the bit is only set by `shallow_clone`. A gate reading that bit alone
    would permit compaction on a dataset registered against a live Lance root, which is the clone
    shape wearing the blob shape's manifest. The listing does not lie: `<base>/_versions/` is there.
    """
    source = str(tmp_path / "src.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(40), pa.int64())}), source)
    assert (tmp_path / "src.lance" / "_versions").is_dir(), "fixture must really be a dataset root"

    uri = str(tmp_path / "registered.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(20), pa.int64())}), uri)
    for i in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(20 + i * 5, 25 + i * 5), pa.int64())}), uri, mode="append")
    lance.dataset(uri).add_bases([lance.DatasetBasePath(path=source, name="src")])
    from service_kit.lakehouse import features

    assert [ref.is_dataset_root for ref in features.manifest_base_path_refs(lance.dataset(uri))] == [False], (
        "fixture assumption broken: the manifest bit would have been enough after all"
    )

    assert _compaction_due(uri) is None, "a table registered against a live Lance root was listed for compaction"


def test_expiry_refuses_a_dataset_that_uses_data_overlays(tmp_path: Path, overlay_dataset: Callable[[Path], str]) -> None:
    """Flag 64. An overlay supersedes individual CELL values from `data/overlay-<uuid>.lance`;
    a rewrite that does not understand them would fold stale base values back in.

    Today pylance refuses the open itself, so this asserts the CLASSIFICATION: a typed refusal that
    names the flag, not an untyped `open:` error carrying a Rust source path — which is what the
    sweep's lineage layer drops as transient non-dataset noise and `summarize` buried in `errors`.
    """
    uri = overlay_dataset(tmp_path)
    data_dir = Path(uri) / "data"
    files_before = {p.name for p in data_dir.iterdir()}

    result = _expire(uri, older_than=timedelta(0))

    assert result.refused is not None, "an overlay dataset must be REFUSED"
    assert "64" in result.refused and "overlay" in result.refused.lower(), f"the refusal must name the flag: {result.refused}"
    assert result.error is None, "a refusal is not an error — the lineage layer drops errors as noise"
    assert result.error_type is None
    assert {p.name for p in data_dir.iterdir()} == files_before, "the overlay dataset's files were rewritten"


def test_an_UNKNOWN_flag_still_refuses_everything(tmp_path: Path) -> None:
    """Splitting the gate must not weaken it. `SUPPORTED_FOR_GC` adds base_paths and nothing else, so
    data overlays (flag 64) and any future unknown flag still refuse the whole pass — the narrow gate
    is a per-operation exception, not a general relaxation."""
    from service_kit.lakehouse import features

    assert features.describe_gc_unsupported_flags(features.FLAG_DATA_OVERLAYS, 0) is not None
    assert features.describe_gc_unsupported_flags(1 << 20, 0) is not None
    # base_paths is the ONLY difference between the two masks.
    assert features.SUPPORTED_FOR_GC == features.SUPPORTED | features.FLAG_BASE_PATHS
    assert features.describe_gc_unsupported_flags(features.FLAG_BASE_PATHS, features.FLAG_BASE_PATHS) is None
    assert features.describe_unsupported_flags(features.FLAG_BASE_PATHS, features.FLAG_BASE_PATHS) is not None
