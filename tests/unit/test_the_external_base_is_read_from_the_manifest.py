"""A dataset's registered base is read from the MANIFEST, not only from a schema stamp.

`blobs.py` carried this in capitals: "THIS EXISTS BECAUSE PYLANCE EXPOSES NO WAY TO READ A DATASET'S
REGISTERED BASES. `add_bases` writes them and nothing reads them back; the base path is not
recoverable from the manifest either (probed on pylance 10.0.0)." `lander.py` repeated it.

IT IS FALSE ON THE PINNED LIBRARY. Probed on pylance 10.0.0:

    ds._ds.base_paths()
    -> {1: DatasetBasePath(id=1, name=Some("blobs"), path=/tmp/…/external, is_dataset_root=false)}

and it survives a reopen from disk, which is the case a mover actually has.

WHY THE STAMP STILL EXISTS rather than being deleted: the two answer different questions. The manifest
says which bases this dataset REGISTERED; the stamp says which base its `blob_uri` values are RELATIVE
to. They coincide for every dataset the estate writes (one external base, named `source`), and the
manifest is the AUTHORITATIVE half — it is written by the same commit as the data and cannot be
carried onto a table by a schema copy, which the stamp can (`transform_stage` forwards upstream schema
metadata downstream). So the manifest is asked first and the stamp is the fallback for a dataset
written before this landed.

A stamp that disagrees with the manifest is worth knowing about, so the resolver prefers the manifest
and this test pins that order.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa

from service_kit.lakehouse import blobs


def _dataset_with_base(tmp: Path, base_name: str = "source") -> tuple[lance.LanceDataset, str]:
    base = tmp / "external"
    base.mkdir(parents=True, exist_ok=True)
    uri = str(tmp / "t.lance")
    table = pa.table({"id": pa.array([1, 2], pa.int64()), "v": pa.array(["a", "b"])})
    lance.write_dataset(
        table,
        uri,
        enable_stable_row_ids=True,
        data_storage_version="2.2",
        initial_bases=[lance.DatasetBasePath(str(base), base_name)],
    )
    return lance.dataset(uri), str(base)


def test_the_registered_base_is_recoverable_without_a_stamp(tmp_path: Path) -> None:
    """The case the capitalised claim said was impossible: a dataset carrying a base and NO stamp."""
    ds, base = _dataset_with_base(tmp_path)
    assert blobs.EXTERNAL_BASE_KEY not in (ds.schema.metadata or {}), "fixture stamped the schema — it must not"

    assert blobs.external_base_of(ds) == base, (
        "the base was registered in the manifest and the resolver could not see it — the stamp is "
        "still the only source of truth"
    )


def test_a_dataset_with_no_base_still_answers_none(tmp_path: Path) -> None:
    """None is the MANAGED answer and must survive: a caller reading None copies rather than refuses."""
    uri = str(tmp_path / "plain.lance")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), uri, data_storage_version="2.2")
    assert blobs.external_base_of(lance.dataset(uri)) is None


def test_the_stamp_still_answers_for_a_dataset_written_before_this(tmp_path: Path) -> None:
    """No backward compatibility does not mean breaking data already on disk: a dataset stamped by an
    older writer and carrying no registered base must still resolve, or its blob pointers become
    unreadable."""
    uri = str(tmp_path / "stamped.lance")
    schema = blobs.stamp_external_base(pa.schema([pa.field("id", pa.int64())]), "s3://legacy/base")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}).cast(schema), uri, data_storage_version="2.2")
    assert blobs.external_base_of(lance.dataset(uri)) == "s3://legacy/base"


def test_the_manifest_wins_over_a_disagreeing_stamp(tmp_path: Path) -> None:
    """The stamp travels with a schema COPY and the manifest cannot, so a disagreement means the stamp
    was inherited from an upstream dataset. The dataset's own registration is the truthful answer."""
    base = tmp_path / "external"
    base.mkdir(parents=True, exist_ok=True)
    uri = str(tmp_path / "both.lance")
    schema = blobs.stamp_external_base(pa.schema([pa.field("id", pa.int64())]), "s3://inherited/from-upstream")
    lance.write_dataset(
        pa.table({"id": pa.array([1], pa.int64())}).cast(schema),
        uri,
        data_storage_version="2.2",
        initial_bases=[lance.DatasetBasePath(str(base), "source")],
    )
    assert blobs.external_base_of(lance.dataset(uri)) == str(base), (
        "a stamp inherited from an upstream dataset outranked this dataset's own registered base"
    )


def test_no_module_still_claims_the_bases_are_unreadable() -> None:
    """The claim justified two workarounds and was repeated in a second file. Falsified prose is
    rewritten, not annotated (2026-08-30 ruling), so the sentence must be gone rather than corrected
    in place."""
    repo = Path(__file__).resolve().parents[2]
    for path in ("packages/service-kit/src/service_kit/lakehouse/blobs.py", "services/ingest/src/ingest/lander.py"):
        text = (repo / path).read_text().lower()
        assert "no way to read a dataset's registered bases" not in text, f"{path} still carries the falsified claim"
