"""The mixed-file-version predicate agrees with Lance's own commit, on real pylance 12.

pylance 11 refused to commit data files at a version other than the table's; pylance 12 commits them
and stamps the sticky reader flag 256. So ``describe_foreign_data_file_versions`` is the refusal rask
now owns, and the only thing that makes it correct is agreeing with what Lance actually does. Each
case here writes real fragments, asks the predicate, then commits and reads the flag Lance set.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import lance
import pyarrow as pa
import pytest

from service_kit.lakehouse.features import describe_foreign_data_file_versions, manifest_feature_flags, mixes_data_file_versions
from service_kit.lancekit.commit_verdict import CommitVerdict, classify_commit_failure


def _ids(*values: int) -> pa.Table:
    return pa.table({"id": pa.array(values, pa.int64())})


@pytest.mark.parametrize("stable", [False, True])
@pytest.mark.parametrize("table_version", ["2.0", "2.1", "2.2"])
@pytest.mark.parametrize("append_version", [None, "2.0", "2.1", "2.2"])
def test_the_predicate_refuses_exactly_when_lance_would_set_flag_256(
    tmp_path: Path, table_version: Literal["2.0", "2.1", "2.2"], stable: bool, append_version: str | None
) -> None:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(_ids(1, 2), uri, data_storage_version=table_version, enable_stable_row_ids=stable)
    written = lance.fragment.write_fragments(_ids(3), uri, **({"data_storage_version": append_version} if append_version else {}))
    fragments = [lance.FragmentMetadata.from_json(json.dumps(f.to_json())) for f in written]

    verdict = describe_foreign_data_file_versions(lance.dataset(uri).data_storage_version, [d for f in fragments for d in f.files])
    lance.LanceDataset.commit(uri, lance.LanceOperation.Append(fragments), read_version=1)
    reader, _ = manifest_feature_flags(lance.dataset(uri))

    assert (verdict is not None) == mixes_data_file_versions(reader)


def test_a_legacy_table_is_not_refused_for_its_own_files(tmp_path: Path) -> None:
    """A legacy table declares 0.x while its files report (0, 2); comparing those would refuse every append."""
    uri = str(tmp_path / "legacy.lance")
    lance.write_dataset(_ids(1, 2), uri, data_storage_version="legacy")
    files = [d for f in lance.fragment.write_fragments(_ids(3), uri) for d in f.files]

    assert describe_foreign_data_file_versions(lance.dataset(uri).data_storage_version, files) is None


def test_the_reason_names_both_versions(tmp_path: Path) -> None:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(_ids(1), uri, data_storage_version="2.1")
    files = [d for f in lance.fragment.write_fragments(_ids(2), uri, data_storage_version="2.2") for d in f.files]

    reason = describe_foreign_data_file_versions("2.1", files)

    assert reason is not None and "2.2" in reason and "2.1" in reason and "256" in reason


@pytest.mark.parametrize("declared", ["", "stable", "2", "x.y"])
def test_an_unreadable_table_version_fails_closed(declared: str) -> None:
    assert describe_foreign_data_file_versions(declared, []) is not None


def test_a_non_string_table_version_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="table_version must be a str"):
        describe_foreign_data_file_versions(cast(Any, 2.1), [])


def _v1_files_committed_onto_a_v2_table(uri: str) -> None:
    """A file entry with no format version parses as (0, 0); Lance refuses the snapshot at commit."""
    lance.write_dataset(_ids(1), uri, data_storage_version="2.2")
    raw = [f.to_json() for f in lance.fragment.write_fragments(_ids(2), uri)]
    for fragment in raw:
        for data_file in fragment["files"]:
            del data_file["file_major_version"], data_file["file_minor_version"]
    fragments = [lance.FragmentMetadata.from_json(json.dumps(f)) for f in raw]
    assert describe_foreign_data_file_versions("2.2", [d for f in fragments for d in f.files]) is None
    lance.LanceDataset.commit(uri, lance.LanceOperation.Append(fragments), read_version=1)


def _files_at_an_unknown_version_committed(uri: str) -> None:
    """A forged (1, 0) is no Lance format at all; Lance refuses it at commit."""
    lance.write_dataset(_ids(1), uri, data_storage_version="2.2")
    raw = [f.to_json() for f in lance.fragment.write_fragments(_ids(2), uri)]
    for fragment in raw:
        for data_file in fragment["files"]:
            data_file["file_major_version"], data_file["file_minor_version"] = 1, 0
    fragments = [lance.FragmentMetadata.from_json(json.dumps(f)) for f in raw]
    lance.LanceDataset.commit(uri, lance.LanceOperation.Append(fragments), read_version=1)


@pytest.mark.parametrize(
    ("provoke", "lance_says"),
    [
        (_v1_files_committed_onto_a_v2_table, "V1 and V2"),
        (_files_at_an_unknown_version_committed, "Unknown Lance storage version"),
    ],
)
def test_lances_own_file_version_refusal_reads_as_the_callers_input(tmp_path: Path, provoke: Callable[[str], None], lance_says: str) -> None:
    """The predicate leaves these to Lance; each refusal must classify as bad input, not as an outage to retry forever."""
    with pytest.raises(OSError, match=lance_says) as refused:
        provoke(str(tmp_path / "t.lance"))

    assert classify_commit_failure(refused.value) is CommitVerdict.CLIENT_ERROR


def _forged_as_0_3(uri: str) -> list[lance.FragmentMetadata]:
    raw = [f.to_json() for f in lance.fragment.write_fragments(_ids(3), uri)]
    for fragment in raw:
        for data_file in fragment["files"]:
            data_file["file_major_version"], data_file["file_minor_version"] = 0, 3
    return [lance.FragmentMetadata.from_json(json.dumps(f)) for f in raw]


@pytest.mark.parametrize("table_version", ["2.0", "2.1", "2.2"])
def test_a_file_stamped_0_3_is_judged_as_2_0(tmp_path: Path, table_version: Literal["2.0", "2.1", "2.2"]) -> None:
    """Lance reads (0, 3) as 2.0, so a (0, 3) file mixes a 2.1 or 2.2 table and not a 2.0 one."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(_ids(1, 2), uri, data_storage_version=table_version, enable_stable_row_ids=True)
    fragments = _forged_as_0_3(uri)

    verdict = describe_foreign_data_file_versions(table_version, [d for f in fragments for d in f.files])
    lance.LanceDataset.commit(uri, lance.LanceOperation.Append(fragments), read_version=1)
    reader, _ = manifest_feature_flags(lance.dataset(uri))

    assert (verdict is not None) == mixes_data_file_versions(reader) == (table_version != "2.0")


@pytest.mark.parametrize(("major", "minor"), [(2, 4), (3, 0)])
def test_a_version_lance_does_not_define_is_named_as_such(major: int, minor: int) -> None:
    """Lance refuses these itself ("Unknown Lance storage version"); the reason must not blame flag 256."""

    class _File:
        file_major_version = major
        file_minor_version = minor

    reason = describe_foreign_data_file_versions("2.1", [_File()])

    assert reason is not None and "not a Lance file format version" in reason and "256" not in reason
