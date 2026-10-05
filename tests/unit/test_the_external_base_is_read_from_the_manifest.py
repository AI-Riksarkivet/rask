"""A dataset's external base is read from its MANIFEST, and from nothing else.

`ds._ds.base_paths()` returns every registered base with its name, path and `is_dataset_root` flag,
and survives a reopen from disk, which is the case a stage runner actually has (probed on pylance
10.0.0). Bases are manifest state, written by the same commit as the data.

A schema-metadata key naming a base is NOT read ([[LH-208]]): schema metadata travels with a schema
copy and any table writer could set it, and on a managed tier a forged one made the in-process cascade
null every payload and register the forged base on the tier above.
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


def test_the_registered_base_is_recovered_from_the_manifest(tmp_path: Path) -> None:
    """A dataset that registered a base answers with it, from a fresh open."""
    ds, base = _dataset_with_base(tmp_path)
    assert blobs.external_base_of(ds) == base, "the base was registered in the manifest and the resolver could not see it"


def test_a_dataset_with_no_base_still_answers_none(tmp_path: Path) -> None:
    """None is the MANAGED answer and must survive: a caller reading None copies rather than refuses."""
    uri = str(tmp_path / "plain.lance")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), uri, data_storage_version="2.2")
    assert blobs.external_base_of(lance.dataset(uri)) is None


def test_a_schema_key_naming_a_base_is_not_a_base(tmp_path: Path) -> None:
    """A managed dataset carrying a forged `rask.blob.external_base` stays managed: the key is a claim
    anyone with write access could make, and only the manifest registers a base."""
    uri = str(tmp_path / "forged.lance")
    table = pa.table({"id": pa.array([1], pa.int64())}).replace_schema_metadata({b"rask.blob.external_base": b"s3://other/"})
    lance.write_dataset(table, uri, data_storage_version="2.2")
    assert blobs.external_base_of(lance.dataset(uri)) is None
