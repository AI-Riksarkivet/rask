"""#2 — client-direct APPEND commit (the catalog as governed commit coordinator).

The client writes Lance fragments DIRECTLY to object storage (``write_fragments`` — the guide's
distributed-write protocol); the catalog folds the serialized ``FragmentMetadata`` into a metadata-only
``LanceDataset.commit`` under root creds. These tests pin the dataplane commit primitive against real
pylance (no S3, no cluster): the happy append, the empty-fragment guard, the conflict classification the
design review flagged (a stale append after an Overwrite is a non-retryable 400, a lost race a 409, a
schema mismatch a 400, never a blanket 409 that loops a doomed retry), and the file-version guard that
pylance 12 no longer applies at commit.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import lance
import pyarrow as pa
import pytest
from lance_namespace import ConcurrentModificationError, InvalidInputError, ServiceUnavailableError

from catalog.services import dataplane
from catalog.services.dataplane import commit_appended_fragments
from service_kit.lakehouse.features import manifest_feature_flags, unsupported_features


def _fragments(uri: str, table: pa.Table, **write_kwargs: Any) -> list[dict[str, Any]]:
    """The client half: write fragments directly to storage, return their serialized metadata (dicts)."""
    return [f.to_json() for f in lance.fragment.write_fragments(table, uri, schema=table.schema, **write_kwargs)]


def _ids(*values: int) -> pa.Table:
    return pa.table({"id": pa.array(values, pa.int64())})


def test_commit_appends_client_written_fragments(tmp_path: Any) -> None:
    uri = str(tmp_path / "t")
    # A table already exists (create stays server-side — it centralizes the 2.2 + stable-row-id invariant).
    lance.write_dataset(
        pa.table({"id": pa.array([1, 2], pa.int64()), "v": ["a", "b"]}),
        uri,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    base = lance.dataset(uri).version

    # The client wrote these fragments itself; the catalog only commits the metadata.
    frags = _fragments(uri, pa.table({"id": pa.array([3], pa.int64()), "v": ["c"]}))
    version, row_count = commit_appended_fragments(uri, {}, frags, base)

    assert version == base + 1
    assert row_count == 3
    assert lance.dataset(uri).to_table().num_rows == 3
    # The append inherited the create-time config — stable row ids are still on (not reset by the commit).
    assert lance.dataset(uri).has_stable_row_ids


def test_commit_rejects_empty_fragment_set(tmp_path: Any) -> None:
    # An empty commit is a client error (the design review's "reject empty fragments"), not a no-op success.
    uri = str(tmp_path / "t")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), uri)
    with pytest.raises(InvalidInputError):
        commit_appended_fragments(uri, {}, [], lance.dataset(uri).version)


def test_stale_append_after_overwrite_is_a_conflict(tmp_path: Any) -> None:
    # Append auto-rebases vs Append, but an Append built at a read_version BEFORE a concurrent Overwrite is
    # Incompatible (transaction.md) -> Lance raises OSError "Incompatible transaction" -> a NON-retryable 400,
    # NOT a silent success and NOT a retryable 409 (a re-commit would replay void fragments — see below).
    uri = str(tmp_path / "t")
    schema = pa.schema([pa.field("id", pa.int64())])
    lance.write_dataset(pa.table({"id": [1]}, schema=schema), uri)  # v1
    stale_base = lance.dataset(uri).version

    # A concurrent Overwrite lands, advancing the version and making the stale append incompatible.
    ov = [
        lance.FragmentMetadata.from_json(json.dumps(f.to_json()))
        for f in lance.fragment.write_fragments(pa.table({"id": [9]}, schema=schema), uri, schema=schema)
    ]
    lance.LanceDataset.commit(uri, lance.LanceOperation.Overwrite(schema, ov), read_version=stale_base)

    frags = _fragments(uri, pa.table({"id": [5]}, schema=schema))
    # NON-RETRYABLE (spec conflict taxonomy; audit 2026-07-14). This test previously asserted
    # ConcurrentModificationError — i.e. it PINNED the dangerous contract: a 409 telling the client to
    # "re-read and re-commit". After the Overwrite above, the table's contents were REPLACED, so replaying
    # these fragments would append data describing the OLD table into a semantically different one. The
    # fragments are void; the write must be redone. A test can pin the WRONG behavior just as confidently
    # as the right one — this one did.
    with pytest.raises(InvalidInputError, match="NOT retryable"):
        commit_appended_fragments(uri, {}, frags, stale_base)  # built against the pre-overwrite version


def test_classify_commit_error_maps_the_taxonomy() -> None:
    # Do NOT collapse every commit OSError to 409. Four distinct outcomes:
    #   schema/version mismatch  -> 400 (can never succeed on retry)
    #   INCOMPATIBLE transaction -> 400, NON-RETRYABLE (spec) — re-WRITE, never re-commit
    #   genuine contention       -> 409 (the loser of a race can safely re-read + re-commit)
    #   raw store 5xx            -> 503 (ArrowIOError subclasses OSError) — an outage, not contention
    from lance_namespace import ServiceUnavailableError

    from catalog.services.dataplane import _classify_commit_error

    assert isinstance(
        _classify_commit_error(OSError("Append with different schema: fields did not match")),
        InvalidInputError,
    )
    assert isinstance(_classify_commit_error(OSError("All data files must have the same version")), InvalidInputError)
    # Incompatible => a NON-RETRYABLE client error, and the message must NOT invite a re-commit.
    incompatible = _classify_commit_error(OSError("Incompatible transaction: this Append is incompatible"))
    assert isinstance(incompatible, InvalidInputError)
    assert "NOT retryable" in str(incompatible)
    assert "re-commit" not in str(incompatible).replace("do not re-commit", "")
    # A genuine race, though, IS retryable — the loser re-reads and re-commits.
    assert isinstance(
        _classify_commit_error(OSError("commit conflict: concurrent writer won")),
        ConcurrentModificationError,
    )
    # An append to a never-created / declared-only table (no committed base) is a client error (400), NOT a
    # 503 the client would retry forever with the same read_version.
    assert isinstance(
        _classify_commit_error(OSError("Version 0 must already exist unless the operation is Overwrite")),
        InvalidInputError,
    )
    # A genuine object-store outage must NOT be mislabeled a client conflict.
    assert isinstance(
        _classify_commit_error(OSError("error performing PUT 503 to rustfs: Service Unavailable")),
        ServiceUnavailableError,
    )


def test_commit_rejects_negative_read_version(tmp_path: Any) -> None:
    uri = str(tmp_path / "t")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), uri)
    frags = _fragments(uri, pa.table({"id": pa.array([2], pa.int64())}))
    with pytest.raises(InvalidInputError):
        commit_appended_fragments(uri, {}, frags, -1)


def test_commit_rejects_malformed_fragment_as_400_not_500(tmp_path: Any) -> None:
    # A garbage fragment dict raises KeyError/TypeError from from_json (outside the OSError taxonomy) — it
    # must translate to a 400 InvalidInput, never escape as a 500.
    uri = str(tmp_path / "t")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), uri)
    with pytest.raises(InvalidInputError):
        commit_appended_fragments(uri, {}, [{"garbage": True}], lance.dataset(uri).version)


def test_commit_rejects_fragments_referencing_absent_data_files(tmp_path: Any) -> None:
    # THE high-severity fix: a fragment whose data file does NOT exist under the table location must be
    # rejected (400) BEFORE commit — otherwise it would publish a 200-OK-but-UNREADABLE current version that
    # breaks reads for every reader. The table's current version must be left untouched.
    import copy

    uri = str(tmp_path / "t")
    lance.write_dataset(
        pa.table({"id": pa.array([1], pa.int64())}),
        uri,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    base = lance.dataset(uri).version
    real = _fragments(uri, pa.table({"id": pa.array([2], pa.int64())}))
    bogus = copy.deepcopy(real)
    bogus[0]["files"][0]["path"] = "deadbeef00_nonexistent.lance"  # a file that was never written

    with pytest.raises(InvalidInputError):
        commit_appended_fragments(uri, {}, bogus, base)
    assert lance.dataset(uri).version == base  # NOT poisoned — the current version is unchanged


@pytest.mark.parametrize(
    ("table_version", "stable", "fragment_version"),
    [("2.1", True, "2.2"), ("2.2", True, "2.1"), ("2.0", False, "2.1")],
)
def test_commit_REFUSES_fragments_at_another_file_version(
    tmp_path: Any, table_version: Literal["2.0", "2.1", "2.2"], stable: bool, fragment_version: str
) -> None:
    """pylance 12 commits these and stamps the sticky reader flag 256; the door must refuse before Lance sees them."""
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1, 2), uri, data_storage_version=table_version, enable_stable_row_ids=stable)
    base = lance.dataset(uri).version
    frags = _fragments(uri, _ids(3), data_storage_version=fragment_version)

    with pytest.raises(InvalidInputError, match=rf"{re.escape(fragment_version)}.*{re.escape(table_version)}"):
        commit_appended_fragments(uri, {}, frags, base)
    assert lance.dataset(uri).version == base
    assert unsupported_features(lance.dataset(uri)) is None


def test_commit_at_read_version_0_judges_the_fragments_against_the_latest_version(tmp_path: Any) -> None:
    """read_version=0 names no manifest; Lance commits the append onto the latest one, so that is the one judged."""
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1), uri, data_storage_version="2.1", enable_stable_row_ids=True)
    base = lance.dataset(uri).version
    frags = _fragments(uri, _ids(2), data_storage_version="2.2")

    with pytest.raises(InvalidInputError, match=r"2\.2.*2\.1"):
        commit_appended_fragments(uri, {}, frags, 0)
    assert lance.dataset(uri).version == base
    assert unsupported_features(lance.dataset(uri)) is None


def test_an_overwrite_between_the_judgement_and_the_commit_cannot_slip_unjudged_files_on(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """read_version=0: the fragments are judged at the latest version, then committed AT that version.

    Committed at 0 instead, Lance runs no conflict check, so an Overwrite that moves the table to 2.2 in
    between lands the judged-clean 2.1 files on a 2.2 table and stamps flag 256 (measured on 12.0.0).
    """
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1), uri, data_storage_version="2.1", enable_stable_row_ids=True)
    frags = _fragments(uri, _ids(2))
    verify = dataplane._verify_fragment_data_files

    def overwrite_then_verify(location: str, so: Any, fragments: list[dict[str, Any]]) -> None:
        lance.write_dataset(_ids(9), uri, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
        verify(location, so, fragments)

    monkeypatch.setattr(dataplane, "_verify_fragment_data_files", overwrite_then_verify)

    with pytest.raises((InvalidInputError, ConcurrentModificationError)):
        commit_appended_fragments(uri, {}, frags, 0)
    assert unsupported_features(lance.dataset(uri)) is None


@pytest.mark.parametrize("read_version", [0, 1])
@pytest.mark.parametrize("table_version", ["2.1", "2.2"])
def test_commit_ACCEPTS_fragments_that_inherit_the_table_version(tmp_path: Any, table_version: Literal["2.1", "2.2"], read_version: int) -> None:
    """``write_fragments`` with no version inherits the table's; that append keeps the table's flags."""
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1), uri, data_storage_version=table_version, enable_stable_row_ids=True)
    flags = manifest_feature_flags(lance.dataset(uri))

    version, rows = commit_appended_fragments(uri, {}, _fragments(uri, _ids(2)), read_version)

    assert (version, rows) == (2, 2)
    assert manifest_feature_flags(lance.dataset(uri)) == flags


def test_a_stale_read_version_after_an_overwrite_to_2_2_stays_the_NOT_retryable_400(tmp_path: Any) -> None:
    """Judged at the read version the fragments match, so Lance's own verdict is the one the caller hears."""
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1), uri, data_storage_version="2.1", enable_stable_row_ids=True)
    frags = _fragments(uri, _ids(2))
    lance.write_dataset(_ids(9), uri, mode="overwrite", data_storage_version="2.2")

    with pytest.raises(InvalidInputError, match="NOT retryable"):
        commit_appended_fragments(uri, {}, frags, 1)
    assert lance.dataset(uri).version == 2


def test_fragments_written_after_an_overwrite_hear_lances_conflict_not_a_file_version_refusal(tmp_path: Any) -> None:
    """They inherited the NEW version, so no writer mixed anything; the stale read_version is the whole story."""
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1), uri, data_storage_version="2.1", enable_stable_row_ids=True)
    lance.write_dataset(_ids(9), uri, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
    frags = _fragments(uri, _ids(2))

    with pytest.raises(InvalidInputError, match="NOT retryable") as refused:
        commit_appended_fragments(uri, {}, frags, 1)
    assert "256" not in str(refused.value)
    assert unsupported_features(lance.dataset(uri)) is None


@pytest.mark.parametrize("read_version", [0, 9])
def test_commit_without_a_committed_base_stays_the_no_base_400(tmp_path: Any, read_version: int) -> None:
    """A declared-only table (read_version 0) and a read version past the latest have nothing to judge against."""
    uri = str(tmp_path / "t")
    if read_version:
        lance.write_dataset(_ids(1), uri, data_storage_version="2.2", enable_stable_row_ids=True)
    frags = _fragments(uri, _ids(2))

    with pytest.raises(InvalidInputError, match="no committed base version"):
        commit_appended_fragments(uri, {}, frags, read_version)


def test_an_unreadable_base_refuses_the_commit_as_503(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that cannot be read has not shown the fragments fit, so nothing is committed."""
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1), uri, data_storage_version="2.2", enable_stable_row_ids=True)
    frags = _fragments(uri, _ids(2))
    real = lance.dataset

    def _unreadable(*_a: Any, **_kw: Any) -> lance.LanceDataset:
        raise OSError("connection reset by peer talking to the object store")

    monkeypatch.setattr(dataplane.lance, "dataset", _unreadable)
    with pytest.raises(ServiceUnavailableError):
        commit_appended_fragments(uri, {}, frags, 1)
    monkeypatch.undo()
    assert real(uri).version == 1


@pytest.mark.parametrize(
    ("table_version", "file_version", "lance_says"),
    [("2.2", None, "V1 and V2"), ("legacy", (2, 1), "same version")],
)
def test_a_V1_V2_mix_is_a_400_not_a_503(tmp_path: Any, table_version: Literal["2.2", "legacy"], file_version: tuple[int, int] | None, lance_says: str) -> None:
    """The guard leaves V1/V2 to Lance, whose refusal is the caller's input. A file entry with no format version parses as (0, 0)."""
    uri = str(tmp_path / "t")
    lance.write_dataset(_ids(1), uri, data_storage_version=table_version)
    frags = _fragments(uri, _ids(2))
    for data_file in frags[0]["files"]:
        del data_file["file_major_version"], data_file["file_minor_version"]
        if file_version:
            data_file["file_major_version"], data_file["file_minor_version"] = file_version

    with pytest.raises(InvalidInputError, match=lance_says):
        commit_appended_fragments(uri, {}, frags, 1)
    assert lance.dataset(uri).version == 1


def test_vending_mode_requires_sts_endpoint_fail_closed() -> None:
    # A token-egress guard: web_identity/sts without an STS endpoint would POST the caller's token to the
    # PUBLIC AWS STS endpoint — the config must fail closed at boot instead.
    from catalog.core.config import Settings

    base = {"s3_access_key_id": "x", "s3_secret_access_key": "x"}
    with pytest.raises(ValueError, match="LANCE_S3_STS_ENDPOINT"):
        Settings.model_validate({**base, "vending_mode": "web_identity"})
    ok = Settings.model_validate({**base, "vending_mode": "web_identity", "s3_sts_endpoint": "http://sts:9000"})
    assert ok.vending_mode == "web_identity"
