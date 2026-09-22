"""#3-B — Lance multi-base DATA distribution (unit).

The write path (`dataplane._write_blob`) is the only place multi-base touches Lance, so patch
`lance.write_dataset` and assert the base wiring (initial_bases + target_bases + base_store_params) — and,
critically, that the #5a invariant (2.2 + stable-row-ids) SURVIVES multi-base. Config allowlist parsing
mirrors the external-blob-bases property.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import lance
import pyarrow as pa
import pytest
from lance_namespace import InvalidInputError

from catalog.core.config import Settings
from catalog.services import dataplane


def _table() -> pa.Table:
    return pa.table({"id": pa.array([1, 2], pa.int64())})


_SO = {"endpoint": "http://rf:9000", "access_key_id": "k", "secret_access_key": "s", "region": "us-east-1"}


def _capture_write() -> tuple[dict[str, Any], Any]:
    captured: dict[str, Any] = {}

    def fake_write(_table_obj: Any, _uri: str, **kw: Any) -> Any:
        captured.update(kw)
        return MagicMock(version=1)

    return captured, fake_write


def test_write_threads_data_bases_into_write_dataset() -> None:
    captured, fake_write = _capture_write()
    with patch.object(lance, "write_dataset", fake_write):
        dataplane._write_blob(
            _table(),
            "s3://root/tbl",
            _SO,
            mode="create",
            allow_external=False,
            external_blob_bases=[],
            data_bases=["s3://b1", "s3://b2/data"],
        )
    assert captured["initial_bases"] is not None and len(captured["initial_bases"]) == 2  # both registered
    assert captured["target_bases"] == ["b1", "b2-data"]  # round-robin targets, referenced by derived NAME
    assert set(captured["base_store_params"]) == {"s3://b1", "s3://b2/data"}  # per-base runtime creds
    # #5a invariant MUST survive multi-base (the whole point of routing creates through this 2.2 path):
    assert captured["enable_stable_row_ids"] is True
    assert captured["data_storage_version"] == "2.2"


def test_external_and_data_bases_compose() -> None:
    captured, fake_write = _capture_write()
    with patch.object(lance, "write_dataset", fake_write):
        dataplane._write_blob(
            _table(),
            "s3://root/tbl",
            _SO,
            mode="create",
            allow_external=False,
            external_blob_bases=["s3://media"],
            data_bases=["s3://b1"],
        )
    assert len(captured["initial_bases"]) == 2  # external-blob base + data base both registered
    assert captured["target_bases"] == ["b1"]  # only the DATA base is a write target


def test_empty_data_bases_is_backward_compatible() -> None:
    captured, fake_write = _capture_write()
    with patch.object(lance, "write_dataset", fake_write):
        dataplane._write_blob(_table(), "s3://root/tbl", _SO, mode="create", allow_external=False, external_blob_bases=[])
    assert captured["initial_bases"] is None
    assert captured["target_bases"] is None
    assert captured["base_store_params"] is None
    assert captured["enable_stable_row_ids"] is True  # single-location create is still 2.2 + row-ids


def test_overwrite_registers_none_but_targets_when_resupplied() -> None:
    # audit F1: base REGISTRATION (initial_bases) is create-only, but the WRITE TARGET (target_bases) applies
    # on a re-supplied overwrite too — so a re-sent overwrite still distributes rather than silently
    # concentrating the new fragments in the primary root.
    captured, fake_write = _capture_write()
    with patch.object(lance, "write_dataset", fake_write):
        dataplane._write_blob(
            _table(),
            "s3://root/tbl",
            _SO,
            mode="overwrite",
            allow_external=False,
            external_blob_bases=[],
            data_bases=["s3://b1"],
        )
    assert captured["initial_bases"] is None  # registration is create-only (pylance rejects re-register)
    assert captured["target_bases"] == ["b1"]  # ...but the write still targets the registered base


def test_colliding_data_base_names_rejected() -> None:
    # audit F2: two DISTINCT approved URIs that collapse to the same lossy _base_name must be rejected loudly,
    # not silently misroute fragments (one base becomes unaddressable / target resolution ambiguous).
    _, fake_write = _capture_write()
    with patch.object(lance, "write_dataset", fake_write), pytest.raises(InvalidInputError):
        dataplane._write_blob(
            _table(),
            "s3://root/tbl",
            _SO,
            mode="create",
            allow_external=False,
            external_blob_bases=[],
            data_bases=["s3://bkt/a/c", "s3://bkt/a-c"],  # both → base name "bkt-a-c"
        )


def test_duplicate_data_base_is_deduped() -> None:
    # audit F2: a repeated base must not double-register / double-target the round-robin.
    captured, fake_write = _capture_write()
    with patch.object(lance, "write_dataset", fake_write):
        dataplane._write_blob(
            _table(),
            "s3://root/tbl",
            _SO,
            mode="create",
            allow_external=False,
            external_blob_bases=[],
            data_bases=["s3://b1", "s3://b1"],
        )
    assert captured["target_bases"] == ["b1"]  # deduped, not ["b1", "b1"]
    assert len(captured["initial_bases"]) == 1


def test_the_vend_door_reads_a_real_multibase_manifest_and_decides_on_its_bases(tmp_path: Any) -> None:
    """Real local Lance writes, no mocks: the vend door must decide from the MANIFEST'S declared bases.

    [[LH-057]]. It used to ask "does any fragment carry a `base_id`" and fall back to server-mediated
    for every table that answered yes, which was right while the session policy reached only the
    primary bucket. `build_session_policy` grants each sanctioned base now, so the question is whether
    a base exists that it could NOT grant — and that is answered off the same manifest read the door
    already does for the optimistic-commit version.
    """
    from catalog.core.vending import dataset_facts, unsanctioned_bases

    single = str(tmp_path / "single")
    lance.write_dataset(_table(), single, data_storage_version="2.2")
    version, bases, _classified = dataset_facts(single, {})
    assert version >= 1 and bases == (), f"a single-location table declared bases: {bases}"
    assert unsanctioned_bases(single, bases) == ()

    mb = str(tmp_path / "mb")
    base = str(tmp_path / "base")
    lance.write_dataset(
        _table(),
        mb,
        initial_bases=[lance.DatasetBasePath(base, is_dataset_root=False, name="b1")],
        target_bases=["b1"],
        data_storage_version="2.2",
    )
    _, mb_bases, _ = dataset_facts(mb, {})
    assert mb_bases, "the registered base is absent from the manifest read, so the policy would be scoped to less than the table"

    # Nobody sanctioned it → the door must proxy, exactly as it always did for this shape.
    assert unsanctioned_bases(mb, mb_bases) == mb_bases
    # AND a local path can never be sanctioned, whatever the allowlist says: the session policy is
    # written in S3 ARNs, so a location it cannot address is one a direct client could not reach. The
    # allowlist is not a way to vouch for a spelling the policy cannot express.
    assert unsanctioned_bases(mb, mb_bases, sanctioned_bases=[str(tmp_path)]) == mb_bases


def test_config_allowlist_parsing() -> None:
    s = Settings.model_validate({"multibase_data_bases": "s3://b1, s3://b2 ,", "s3_access_key_id": "x", "s3_secret_access_key": "x"})
    assert s.multibase_data_base_list == ["s3://b1", "s3://b2"]
    empty = Settings.model_validate({"s3_access_key_id": "x", "s3_secret_access_key": "x"})
    assert empty.multibase_data_base_list == []  # default off
