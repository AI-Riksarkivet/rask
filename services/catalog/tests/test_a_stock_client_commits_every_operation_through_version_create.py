"""[[LH-471]] A stock Lance client commits every operation through the catalog's ``CreateTableVersion`` (ADR 0157).

The catalog answers ``managed_versioning=true`` on ``DescribeTable``, so pylance stages each manifest under
``_versions/<n>.manifest-<uuid>`` and asks ``POST /v1/table/{id}/version/create`` to finalize it
(``lance_docs/ns_catalog/spec.yaml`` § CreateTableVersion). The client here is pylance's own, over
``lance_namespace.connect("rest", ...)`` against the real catalog served by uvicorn; nothing in its construction
is rask's. The catalog's ``commit_version`` audit record names the operation it read from the staged transaction:
that record is how a commit that reached the catalog differs from one pylance finalized on its own.

A refused commit is not bypassed: the table stays at its version and the client sees the spec error.

The store is the local filesystem, and a moto S3 server for the compaction: the claim is the commit path, not the
credential (LH-229 pins that). OIDC is on with a stub verifier, and FGA's answers are faked by relation (the offline
suite has no OpenFGA): every relation is granted except the ones a case denies, or only the ones a caller holds, so
the door's own rung choice is what decides.
"""

from __future__ import annotations

import contextlib
import json
import logging
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from lance.bitmap import Bitmap
from lance.dataset import Index
from lance.optimize import Compaction
from moto.server import ThreadedMotoServer
from pydantic import BaseModel

from catalog.core import manifest_bytes
from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken
from service_kit.lakehouse import base_refs, maintenance_due


_TOKEN = {"Authorization": "Bearer t"}
_TABLE = ["db", "t"]
#: What ``writer`` and ``maintainer`` on a table grant (``model.fga``: writer -> reader -> can_read_data, can_get_metadata).
_WRITER_AND_MAINTAINER = frozenset({"can_get_metadata", "can_read_data", "can_write_data", "can_maintain"})


class Served(BaseModel):
    url: str
    #: Relations FGA denies: a bare relation everywhere, ``<relation> <object>`` on one object.
    denied: set[str]
    #: When set, the only relations FGA grants (the caller's grants); ``denied`` still applies.
    granted: frozenset[str] | None = None
    #: The local root the tables live under, for reading a staged manifest back; ``None`` on an object store.
    root: Path | None
    #: What a client passes to reach the store itself: nothing on a local root.
    storage_options: dict[str, str] = {}

    def stock(self, context: dict[str, str] | None = None) -> lance_namespace.LanceNamespace:
        """pylance's REST namespace, sending ``context`` as request headers on every call (``headers.<name>``)."""
        headers = {f"headers.{name}": value for name, value in (context or {}).items()}
        return lance_namespace.connect("rest", {"uri": self.url, "headers.Authorization": _TOKEN["Authorization"], **headers})


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """The real catalog on a local root under uvicorn, alice signed in, FGA granting all but ``denied``."""
    env = {
        "LANCE_REST_ROOT": str(tmp_path / "root"),
        "LANCE_CONTROL_ROOT": str(tmp_path / "control"),
        "LANCE_S3_ACCESS_KEY_ID": "unused",
        "LANCE_S3_SECRET_ACCESS_KEY": "unused",
    }
    yield from _serve(Served(url="", denied=set(), root=tmp_path / "root"), env, monkeypatch)


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def served_on_s3(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """``served`` on a bucket of its own on a moto S3 server, which the client reaches with its own key."""
    bucket = f"lh467-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_url, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test").create_bucket(Bucket=bucket)
    env = {
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
    }
    own = {"aws_endpoint": moto_url, "aws_region": "us-east-1", "allow_http": "true", "aws_access_key_id": "test", "aws_secret_access_key": "test"}
    yield from _serve(Served(url="", denied=set(), root=None, storage_options=own), env, monkeypatch)


def _serve(served: Served, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
        **env,
    }.items():
        monkeypatch.setenv(key, value)

    async def check(_client: object, *, relation: str, obj: str, **_kw: object) -> bool:
        if served.granted is not None and relation not in served.granted:
            return False
        return relation not in served.denied and f"{relation} {obj}" not in served.denied

    monkeypatch.setattr(fga_module, "check", check)
    monkeypatch.setattr(fga_module, "grant_on_create", AsyncMock())
    monkeypatch.setattr(fga_module, "revoke_object_tuples", AsyncMock(return_value=[]))
    from catalog.core.config import Settings, get_settings

    get_settings.cache_clear()
    from catalog.main import app

    # Auth is turned on after startup, as the TestClient suites do: the lifespan would otherwise dial the issuer.
    signed_in = get_settings().model_copy(
        update={"oidc_enabled": True, "oidc_issuer": "https://idp.example", "oidc_audience": "lance", "fga_enabled": True, "fga_api_url": "http://openfga:8080"}
    )
    assert isinstance(signed_in, Settings)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    verifier = MagicMock()
    verifier.verify.return_value = IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    app.state.oidc = verifier
    fga_client = MagicMock()
    fga_client.close = AsyncMock()
    app.state.fga = fga_client
    app.dependency_overrides[get_settings] = lambda: signed_in
    served.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    yield served
    server.should_exit = True
    thread.join(timeout=30)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _created(served: Served) -> lance_namespace.LanceNamespace:
    stock = served.stock()
    stock.create_namespace(lance_namespace.CreateNamespaceRequest(id=["db"]))
    lance.write_dataset(
        _keyed([1, 2, 3], ["a", "b", "c"]),
        namespace_client=stock,
        table_id=_TABLE,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        storage_options=served.storage_options or None,
    )
    return stock


def _keyed(ids: list[int], values: list[str]) -> pa.Table:
    """Rows of a table whose ``id`` is its declared (unenforced) primary key."""
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata={"lance-schema:unenforced-primary-key": "true"}), pa.field("v", pa.string())])
    return pa.table({"id": ids, "v": values}, schema=schema)


def _opened(stock: lance_namespace.LanceNamespace, storage_options: dict[str, str] | None = None) -> lance.LanceDataset:
    return lance.dataset(namespace_client=stock, table_id=_TABLE, storage_options=storage_options or None)


def _sweep_wrote_a_clone_list(stock: lance_namespace.LanceNamespace, *, clones_of_the_table: list[str], ago: timedelta = timedelta(0)) -> None:
    """Write the clone list as a maintenance sweep tick does ``ago``, naming the datasets that resolve through ``db$t``."""
    from catalog.core.config import get_settings

    refs = base_refs.BaseRefs()
    if clones_of_the_table:
        root = base_refs.decoded_path(stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).location or "")
        refs = base_refs.BaseRefs(protected={root}, referrers={root: set(clones_of_the_table)})
    settings = get_settings()
    maintenance_due.write_clone_list(settings.registry_root, settings.storage_options(), maintenance_due.CloneList.taken(refs, at=datetime.now(UTC) - ago))


def _rows(dataset: lance.LanceDataset) -> list[tuple[int, str]]:
    table = dataset.to_table().sort_by("id")
    return list(zip(table["id"].to_pylist(), table["v"].to_pylist(), strict=True))


def test_append_delete_merge_insert_update_and_restore_each_commit_through_version_create(served: Served, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="lance.audit"):
        stock = _created(served)
        lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")
        assert _rows(_opened(stock)) == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]
        _opened(stock).delete("id = 2")
        assert _rows(_opened(stock)) == [(1, "a"), (3, "c"), (4, "d")]
        changes = pa.table({"id": pa.array([3, 5], pa.int64()), "v": ["C", "e"]})
        _opened(stock).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(changes)
        assert _rows(_opened(stock)) == [(1, "a"), (3, "C"), (4, "d"), (5, "e")]
        _opened(stock).update({"v": "'A'"}, where="id = 1")
        assert _rows(_opened(stock)) == [(1, "A"), (3, "C"), (4, "d"), (5, "e")]
        _opened(stock).checkout_version(3).restore()
        restored = _opened(stock)
        assert _rows(restored) == [(1, "a"), (3, "c"), (4, "d")]

    assert restored.version == 6
    committed = [(getattr(r, "audit.version"), getattr(r, "audit.operation")) for r in caplog.records if getattr(r, "audit.action", None) == "commit_version"]
    assert committed == [(1, "Overwrite"), (2, "Append"), (3, "Delete"), (4, "Update"), (5, "Update"), (6, "Restore")]


def test_a_stock_compaction_by_a_writer_and_maintainer_commits_through_version_create(served_on_s3: Served, caplog: pytest.LogCaptureFixture) -> None:
    """Lance's own distributed protocol (``lance_docs/file_format.md`` § Rewrite): reserve the new fragment ids, then
    commit the rewrite. Both are commits a stock client stages, so both are judged here."""
    stock = _created(served_on_s3)
    for row, value in ((4, "d"), (5, "e"), (6, "f")):
        lance.write_dataset(_keyed([row], [value]), namespace_client=stock, table_id=_TABLE, mode="append", storage_options=served_on_s3.storage_options)
    _sweep_wrote_a_clone_list(stock, clones_of_the_table=[])
    served_on_s3.granted = _WRITER_AND_MAINTAINER
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="lance.audit"):
        dataset = _opened(stock, served_on_s3.storage_options)
        plan = Compaction.plan(dataset, {"target_rows_per_fragment": 1024})
        Compaction.commit(dataset, [task.execute(dataset) for task in plan.tasks])

    compacted = _opened(stock, served_on_s3.storage_options)
    assert (compacted.version, len(compacted.get_fragments())) == (6, 1)
    assert _rows(compacted) == [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e"), (6, "f")]
    committed = [(getattr(r, "audit.version"), getattr(r, "audit.operation")) for r in caplog.records if getattr(r, "audit.action", None) == "commit_version"]
    assert committed == [(5, "ReserveFragments"), (6, "Rewrite")]


def test_the_index_door_refuses_and_a_writers_own_index_build_commits_through_version_create(served: Served, caplog: pytest.LogCaptureFixture) -> None:
    """The catalog builds no index (ADR 0162): its spec door answers 406 naming the bring-your-own path, and a writer's
    own pylance builds the index and later folds new fragments into it, each commit judged at ``version/create``."""
    stock = _created(served)
    served.granted = frozenset({"can_get_metadata", "can_read_data", "can_write_data"})

    refused = httpx.post(f"{served.url}/v1/table/db$t/create_scalar_index", json={"column": "id", "index_type": "BTREE"}, headers=_TOKEN)
    assert refused.status_code == 406, refused.text
    assert "create_scalar_index" in refused.json()["detail"] and "version/create" in refused.json()["detail"]
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="lance.audit"):
        _opened(stock).create_scalar_index("id", index_type="BTREE", name="id_idx")
        lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")
        _opened(stock).optimize.optimize_indices()

    indexed = _opened(stock)
    covered = {fragment for segment in indexed.describe_indices()[0].segments for fragment in segment.fragment_ids}
    assert covered == {fragment.fragment_id for fragment in indexed.get_fragments()}
    committed = [(getattr(r, "audit.version"), getattr(r, "audit.operation")) for r in caplog.records if getattr(r, "audit.action", None) == "commit_version"]
    assert committed == [(2, "CreateIndex"), (3, "Append"), (4, "CreateIndex")]


def _append_at_file_version_2_1(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([9], ["z"]), namespace_client=stock, table_id=_TABLE, mode="append", data_storage_version="2.1")


def _create_at_file_version_2_1(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([9], ["z"]), namespace_client=stock, table_id=["db", "u"], mode="create", data_storage_version="2.1", enable_stable_row_ids=True)


def _append_a_held_key(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([1], ["again"]), namespace_client=stock, table_id=_TABLE, mode="append")


def _drop_the_key_column(stock: lance_namespace.LanceNamespace) -> None:
    _opened(stock).drop_columns(["id"])


def _re_append_a_held_file(stock: lance_namespace.LanceNamespace) -> None:
    """A crafted Append naming the first fragment's committed data file again, under a new fragment id."""
    dataset = _opened(stock)
    held = dataset.get_fragments()[0].metadata
    forged = lance.FragmentMetadata(id=99, files=held.files, physical_rows=held.physical_rows)
    lance.LanceDataset.commit(
        dataset,
        lance.LanceOperation.Append([forged]),
        read_version=dataset.version,
        namespace_client=stock,
        table_id=_TABLE,
        namespace_client_managed_versioning=True,
    )


def _commit_as_update(stock: lance_namespace.LanceNamespace, operation: lance.LanceOperation.Update) -> None:
    """A crafted commit the writer labels ``Update``, sent through the namespace like any stock commit."""
    dataset = _opened(stock)
    lance.LanceDataset.commit(
        dataset, operation, read_version=dataset.version, namespace_client=stock, table_id=_TABLE, namespace_client_managed_versioning=True
    )


def _undelete_as_an_update(stock: lance_namespace.LanceNamespace) -> None:
    """The first fragment kept without the deletion file the delete of id 2 gave it."""
    held = _opened(stock).get_fragments()[0].metadata
    undeleted = lance.FragmentMetadata(id=held.id, files=held.files, physical_rows=held.physical_rows, row_id_meta=held.row_id_meta)
    _commit_as_update(stock, lance.LanceOperation.Update(updated_fragments=[undeleted]))


def _reuse_live_row_ids_as_an_update(stock: lance_namespace.LanceNamespace) -> None:
    """A new fragment carrying the first fragment's stable row ids, whose rows stay alive."""
    dataset = _opened(stock)
    held = dataset.get_fragments()[0].metadata
    (written,) = lance.fragment.write_fragments(_keyed([7, 8, 9], ["x", "y", "z"]), dataset)
    copied = lance.FragmentMetadata(id=99, files=written.files, physical_rows=written.physical_rows, row_id_meta=held.row_id_meta)
    _commit_as_update(stock, lance.LanceOperation.Update(new_fragments=[copied]))


def _arm_auto_cleanup(stock: lance_namespace.LanceNamespace) -> None:
    _opened(stock).update_config({"lance.auto_cleanup.interval": "1", "lance.auto_cleanup.older_than": "0ms"})


def _restore_version_1(stock: lance_namespace.LanceNamespace) -> None:
    _opened(stock).checkout_version(1).restore()


def _overwrite(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([9], ["z"]), namespace_client=stock, table_id=_TABLE, mode="overwrite")


def _compact(stock: lance_namespace.LanceNamespace) -> None:
    dataset = _opened(stock)
    plan = Compaction.plan(dataset, {"target_rows_per_fragment": 1024})
    Compaction.commit(dataset, [task.execute(dataset) for task in plan.tasks])


def _compact_on_a_clone_list_past_its_age(stock: lance_namespace.LanceNamespace) -> None:
    """The sweep wrote the list two hours ago, past the default hour (``LANCE_CLONE_LIST_MAX_AGE_SECONDS``)."""
    _sweep_wrote_a_clone_list(stock, clones_of_the_table=[], ago=timedelta(hours=2))
    _compact(stock)


def _compact_a_table_a_clone_reads_through(stock: lance_namespace.LanceNamespace) -> None:
    _sweep_wrote_a_clone_list(stock, clones_of_the_table=["s3://elsewhere/clone"])
    _compact(stock)


_UNKEYED = ["db", "u"]


def _rewrite(stock: lance_namespace.LanceNamespace, *, compacted: list[int], replaced: list[int]) -> None:
    """A Rewrite of a keyless ``db$u`` (fragments ``[1, 2]``, ``[3]``, ``[4]``) claiming to replace ``replaced``.

    Its one new fragment is what Lance's own compaction task writes for fragments ``compacted``, so it carries their
    rows and stable row ids. Measured on pylance 13.0.0, Lance commits the claim whatever fragments it names. Keyless,
    because on a keyed table the primary key judge refuses a duplicated row first.
    """
    lance.write_dataset(
        pa.table({"n": [1, 2]}), namespace_client=stock, table_id=_UNKEYED, mode="create", data_storage_version="2.2", enable_stable_row_ids=True
    )
    for row in (3, 4):
        lance.write_dataset(pa.table({"n": [row]}), namespace_client=stock, table_id=_UNKEYED, mode="append")
    dataset = lance.dataset(namespace_client=stock, table_id=_UNKEYED)
    held = {fragment.fragment_id: fragment.metadata for fragment in dataset.get_fragments()}
    excluded = sorted(set(held) - set(compacted))
    (task,) = Compaction.plan(dataset, {"target_rows_per_fragment": 1024, "excluded_fragment_ids": excluded}).tasks
    (written,) = task.execute(dataset).new_fragments
    new = lance.FragmentMetadata(id=max(held) + 1, files=written.files, physical_rows=written.physical_rows, row_id_meta=written.row_id_meta)
    group = lance.LanceOperation.RewriteGroup(old_fragments=[held[index] for index in replaced], new_fragments=[new])
    lance.LanceDataset.commit(
        dataset,
        lance.LanceOperation.Rewrite(groups=[group], rewritten_indices=[]),
        read_version=dataset.version,
        namespace_client=stock,
        table_id=_UNKEYED,
        namespace_client_managed_versioning=True,
    )


def _rewrite_losing_a_replaced_fragments_rows(stock: lance_namespace.LanceNamespace) -> None:
    _rewrite(stock, compacted=[0, 1], replaced=[0, 1, 2])


def _rewrite_duplicating_a_carried_fragments_rows(stock: lance_namespace.LanceNamespace) -> None:
    _rewrite(stock, compacted=[0, 1], replaced=[0])


def _index(*, base_id: int | None) -> Index:
    """An index entry a writer can name in a CreateIndex: nothing under ``_indices/<uuid>/`` backs it."""
    return Index(uuid=str(uuid.uuid4()), name="forged", fields=[0], dataset_version=3, fragment_ids=Bitmap([0, 1]), index_version=0, base_id=base_id)


def _commit_an_index(stock: lance_namespace.LanceNamespace, index: Index) -> None:
    """A CreateIndex naming ``index``; measured on pylance 13.0.0, Lance commits it, and every query it serves fails."""
    dataset = _opened(stock)
    lance.LanceDataset.commit(
        dataset,
        lance.LanceOperation.CreateIndex(new_indices=[index], removed_indices=[]),
        read_version=dataset.version,
        namespace_client=stock,
        table_id=_TABLE,
        namespace_client_managed_versioning=True,
    )


def _index_read_through_a_base(stock: lance_namespace.LanceNamespace) -> None:
    _commit_an_index(stock, _index(base_id=0))


def _index_with_no_files(stock: lance_namespace.LanceNamespace) -> None:
    _commit_an_index(stock, _index(base_id=None))


def _detached(root: Path, operation: lance.LanceOperation.BaseOperation, read_version: int) -> bytes:
    """The manifest a detached commit of ``operation`` writes."""
    detached = lance.LanceDataset.commit(str(root), operation, read_version=read_version, detached=True)
    return (root / f"_versions/d{detached.version}.manifest").read_bytes()


def _carrying(manifest: bytes, transaction: bytes) -> bytes:
    """``manifest`` with ``transaction`` as its inline transaction, which Lance 13 keeps at offset 0 ahead of the message."""
    message = manifest_bytes.manifest_message(manifest)
    assert manifest_bytes.varint_field(message, manifest_bytes.TRANSACTION_SECTION_FIELD) == 0
    section = struct.pack("<I", len(transaction)) + transaction
    major, minor = struct.unpack_from("<HH", manifest, len(manifest) - 8)
    return section + struct.pack("<I", len(message)) + message + struct.pack("<QHH4s", len(section), major, minor, b"LANC")


def _data_staged_under_an_index_label(stock: lance_namespace.LanceNamespace) -> None:
    """A staged manifest adding a fragment whose transaction says CreateIndex, put by hand.

    A writer may put data files and a staged manifest; pylance builds no such manifest, so it is an Append's carrying a
    CreateIndex's transaction, both from detached commits, staged as the table's next version.
    """
    dataset = _opened(stock)
    root = Path(dataset.uri.removeprefix("file://"))
    (written,) = lance.fragment.write_fragments(_keyed([9], ["z"]), dataset)
    appended = _detached(root, lance.LanceOperation.Append([written]), dataset.version)
    indexed = _detached(root, lance.LanceOperation.CreateIndex(new_indices=[_index(base_id=None)], removed_indices=[]), dataset.version)
    label = manifest_bytes.transaction_section(indexed)
    assert label is not None
    staged = root / f"_versions/{(1 << 64) - 1 - (dataset.version + 1)}.manifest-{uuid.uuid4()}"
    staged.write_bytes(manifest_bytes.with_manifest_version(_carrying(appended, label), dataset.version + 1))


@pytest.mark.parametrize(
    ("stage", "denied", "protect", "status", "code", "staged_at"),
    [
        pytest.param(_append_at_file_version_2_1, set(), False, 400, 13, ("db$t", 4), id="foreign-data-file-versions-are-refused"),
        pytest.param(_create_at_file_version_2_1, set(), False, 400, 13, ("db$u", 1), id="a-create-is-at-2.2-with-stable-row-ids"),
        pytest.param(_append_a_held_key, set(), False, 400, 13, ("db$t", 4), id="a-held-primary-key-is-refused"),
        pytest.param(_drop_the_key_column, set(), False, 400, 13, ("db$t", 4), id="the-key-column-is-kept"),
        pytest.param(_re_append_a_held_file, set(), False, 400, 13, ("db$t", 4), id="a-held-data-file-is-not-appended-again"),
        pytest.param(_undelete_as_an_update, set(), False, 400, 13, ("db$t", 4), id="a-deleted-row-does-not-come-back-whatever-the-label"),
        pytest.param(_reuse_live_row_ids_as_an_update, set(), False, 400, 13, ("db$t", 4), id="live-row-ids-are-not-reused-whatever-the-label"),
        pytest.param(_append_at_file_version_2_1, set(), False, 400, 13, ("db$t", 3), id="a-version-that-does-not-follow-the-latest-is-refused"),
        pytest.param(_arm_auto_cleanup, set(), False, 400, 13, ("db$t", 4), id="auto-cleanup-is-not-armed-by-a-commit"),
        pytest.param(_restore_version_1, {"can_restore"}, False, 403, 15, ("db$t", 4), id="a-writer-cannot-stage-a-restore"),
        pytest.param(_overwrite, set(), True, 409, 19, ("db$t", 4), id="an-overwrite-of-a-protected-table-is-refused"),
        pytest.param(_compact, {"can_maintain"}, False, 403, 15, ("db$t", 4), id="a-writer-cannot-stage-a-compaction"),
        pytest.param(_compact, set(), False, 503, 17, ("db$t", 4), id="a-compaction-waits-for-the-sweeps-clone-list"),
        pytest.param(_compact_on_a_clone_list_past_its_age, set(), False, 503, 17, ("db$t", 4), id="a-compaction-waits-for-a-fresh-clone-list"),
        pytest.param(_compact_a_table_a_clone_reads_through, set(), False, 406, 0, ("db$t", 4), id="a-table-a-clone-reads-through-is-not-compacted"),
        pytest.param(_rewrite_losing_a_replaced_fragments_rows, set(), False, 400, 13, ("db$u", 4), id="a-rewrite-loses-no-row"),
        pytest.param(_rewrite_duplicating_a_carried_fragments_rows, set(), False, 400, 13, ("db$u", 4), id="a-rewrite-duplicates-no-row"),
        pytest.param(_index_read_through_a_base, set(), False, 400, 13, ("db$t", 4), id="an-index-is-read-from-the-tables-own-indices"),
        pytest.param(_index_with_no_files, set(), False, 400, 13, ("db$t", 4), id="an-index-names-files-that-are-there"),
        pytest.param(_data_staged_under_an_index_label, set(), False, 400, 13, ("db$t", 4), id="an-index-commit-changes-no-data"),
    ],
)
def test_a_refused_commit_leaves_the_table_at_its_version_and_answers_a_spec_error(
    served: Served,
    stage: Callable[[lance_namespace.LanceNamespace], None],
    denied: set[str],
    protect: bool,
    status: int,
    code: int,
    staged_at: tuple[str, int],
) -> None:
    stock = _created(served)
    lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")
    _opened(stock).delete("id = 2")
    if protect:
        assert httpx.post(f"{served.url}/management/v1/table/db$t/protection", json={"protected": True}, headers=_TOKEN).status_code == 200
    served.denied.update(denied)

    # pylance reports a refused finalize as an OSError; a manifest staged by hand asks nothing until it is answered below.
    with contextlib.suppress(OSError):
        stage(stock)

    after = _opened(stock)
    assert after.version == 3
    assert _rows(after) == [(1, "a"), (3, "c"), (4, "d")]
    assert _answer_to_the_staged_commit(served, *staged_at)[:2] == (status, code)
    assert _opened(stock).version == 3


def _answer_to_the_staged_commit(served: Served, table: str, version: int) -> tuple[int, int, str]:
    """The status, spec code and detail the catalog answers the one staged manifest of ``table`` at ``version``.

    pylance reports a refused finalize as an unknown outcome and keeps its staged manifest; asking for it again shows
    the answer the client was given.
    """
    assert served.root is not None, "a staged manifest is read back from a local root"
    pending = list(served.root.glob(f"*_{table}/_versions/*.manifest-*"))
    assert len(pending) == 1, f"one staged manifest of {table} awaits its answer, found {pending}: none means the commit landed"
    (staged,) = pending
    path = staged.relative_to("/").as_posix()
    again = httpx.post(f"{served.url}/v1/table/{table}/version/create", json={"version": version, "manifest_path": path}, headers=_TOKEN)
    body = again.json()
    return again.status_code, body.get("code"), body.get("detail", "")


def test_what_lands_is_what_was_judged_whatever_the_writer_puts_over_its_staged_manifest_meanwhile(served: Served, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write tier may put over its own staged key until the finalize; the catalog finalizes the bytes it judged."""
    from catalog.services import managed_versions

    judged_then = managed_versions.discard

    def swap_after_judging(staged: managed_versions.StagedVersion, so: dict[str, str]) -> None:
        Path(staged.staged_uri.removeprefix("file://")).write_bytes(b"not the manifest that was judged")
        judged_then(staged, so)

    stock = _created(served)
    monkeypatch.setattr(managed_versions, "discard", swap_after_judging)

    lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")

    landed = _opened(stock)
    assert landed.version == 2
    assert _rows(landed) == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


#: The provenance a pipeline names on a governed write (ADR 0156 C2): its run, its job, and each input at a version.
_RUN = {"x-rask-run-id": "run-7", "x-rask-job": "nightly", "x-rask-inputs": json.dumps([{"table": ["db", "src"], "version": 1}])}


def _with_source(served: Served) -> lance_namespace.LanceNamespace:
    """``db$t`` at version 1 and the input ``db$src`` at version 1."""
    stock = _created(served)
    lance.write_dataset(
        _keyed([1], ["s"]), namespace_client=stock, table_id=["db", "src"], mode="create", data_storage_version="2.2", enable_stable_row_ids=True
    )
    return stock


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _insert(stock: lance_namespace.LanceNamespace) -> None:
    stock.insert_into_table(lance_namespace.InsertIntoTableRequest(id=_TABLE, mode="append"), _ipc(_keyed([4], ["d"])))


def _merge_insert(stock: lance_namespace.LanceNamespace) -> None:
    request = lance_namespace.MergeInsertIntoTableRequest(id=_TABLE, on="id", when_matched_update_all=True, when_not_matched_insert_all=True)
    stock.merge_insert_into_table(request, _ipc(_keyed([3, 4], ["C", "d"])))


def _committed_properties(dataset: lance.LanceDataset, version: int) -> dict[str, str]:
    """The transaction properties version ``version`` of ``dataset`` committed."""
    transaction = dataset.read_transaction(version)
    assert transaction is not None, f"version {version} has no transaction"
    return dict(transaction.transaction_properties or {})


class _Recorder:
    """The lineage emitter, keeping what each write door announced."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def project_for(self, top_ns: str) -> str | None:
        return None

    async def emit_create(self, **fields: Any) -> None:
        self.writes.append(fields)

    async def emit_write(self, **fields: Any) -> None:
        self.writes.append(fields)


@pytest.mark.parametrize("door", [pytest.param(_insert, id="insert"), pytest.param(_merge_insert, id="merge_insert")])
def test_a_catalog_write_door_commits_the_run_its_context_names(
    served: Served, door: Callable[[lance_namespace.LanceNamespace], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog.core.lineage_emit import InputRef
    from catalog.main import app

    _with_source(served)
    recorder = _Recorder()
    monkeypatch.setattr(app.state, "lineage_emitter", recorder, raising=False)

    door(served.stock(_RUN))

    landed = _opened(served.stock())
    assert landed.version == 2
    properties = _committed_properties(landed, 2)
    assert {key: value for key, value in properties.items() if key != "rask.inputs"} == {"rask.run_id": "run-7", "rask.job": "nightly", "rask.author": "alice"}
    assert json.loads(properties["rask.inputs"]) == [{"table": ["db", "src"], "version": 1}]
    (announced,) = recorder.writes
    assert (announced["version"], announced["inputs"], announced["run"].run_id, announced["run"].job) == (
        2,
        [InputRef("db", "db$src", 1, None)],
        "run-7",
        "nightly",
    )


_SILVER = ["lab", "t"]
_PROVENANCE = {"rask.run_id": "run-7", "rask.author": "alice", "rask.inputs": json.dumps([{"table": ["db", "src"], "version": 1}])}


def _stock_append_naming_no_run(served: Served) -> tuple[int, int, str]:
    with pytest.raises(OSError):
        lance.write_dataset(_keyed([2], ["b"]), namespace_client=served.stock(), table_id=_SILVER, mode="append")
    return _answer_to_the_staged_commit(served, "lab$t", 2)


def _stock_append_naming_another_author(served: Served) -> tuple[int, int, str]:
    """The author is the verified caller; a run naming anyone else would be a forged provenance."""
    forged = {**_PROVENANCE, "rask.author": "mallory"}
    with pytest.raises(OSError):
        lance.write_dataset(_keyed([2], ["b"]), namespace_client=served.stock(), table_id=_SILVER, mode="append", transaction_properties=forged)
    return _answer_to_the_staged_commit(served, "lab$t", 2)


def _insert_door_naming_no_run(served: Served) -> tuple[int, int, str]:
    answer = httpx.post(
        f"{served.url}/v1/table/lab$t/insert", content=_ipc(_keyed([2], ["b"])), headers={**_TOKEN, "content-type": "application/vnd.apache.arrow.stream"}
    )
    return answer.status_code, answer.json()["code"], answer.json()["detail"]


def _update_door(served: Served) -> tuple[int, int, str]:
    """``update`` cannot carry a run into its commit, so a silver table refuses it whatever the Context names."""
    context = {"x-rask-run-id": "run-7", "x-rask-inputs": _PROVENANCE["rask.inputs"]}
    answer = httpx.post(f"{served.url}/v1/table/lab$t/update", json={"updates": [["v", "'z'"]]}, headers={**_TOKEN, **context})
    return answer.status_code, answer.json()["code"], answer.json()["detail"]


def _register_into_silver(served: Served) -> tuple[int, int, str]:
    """``register`` attaches bytes written elsewhere and commits nothing a run can reach ([[LH-463]])."""
    answer = httpx.post(f"{served.url}/v1/table/lab$registered/register", json={"location": "elsewhere"}, headers=_TOKEN)
    return answer.status_code, answer.json().get("code"), answer.json().get("detail", "")


def _rename_into_silver(served: Served) -> tuple[int, int, str]:
    """A rename commits nothing a run can reach, so it may not move another tier's table into silver."""
    body = {"new_table_name": "moved", "new_namespace_id": ["lab"]}
    answer = httpx.post(f"{served.url}/v1/table/db$src/rename", json=body, headers=_TOKEN)
    return answer.status_code, answer.json()["code"], answer.json()["detail"]


@pytest.mark.parametrize(
    ("write", "answer", "names"),
    [
        pytest.param(_stock_append_naming_no_run, (400, 13), "rask.run_id", id="a-stock-commit-naming-no-run"),
        pytest.param(_stock_append_naming_another_author, (400, 13), "rask.author", id="a-stock-commit-naming-another-author"),
        pytest.param(_insert_door_naming_no_run, (400, 13), "rask.run_id", id="the-insert-door-naming-no-run"),
        pytest.param(_update_door, (406, 0), "merge_insert", id="the-update-door"),
        pytest.param(_rename_into_silver, (400, 13), "untiered table into a silver namespace", id="a-rename-into-silver"),
        pytest.param(_register_into_silver, (406, 0), "merge_insert", id="a-register-into-silver"),
    ],
)
def test_a_silver_commit_lands_only_when_it_names_its_run_and_inputs(
    served: Served, write: Callable[[Served], tuple[int, int, str]], answer: tuple[int, int], names: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog.core.lineage_emit import InputRef
    from catalog.main import app

    stock = _with_source(served)
    stock.create_namespace(lance_namespace.CreateNamespaceRequest(id=["lab"], properties={"rask.tier": "silver"}))
    lance.write_dataset(
        _keyed([1], ["a"]),
        namespace_client=stock,
        table_id=_SILVER,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        transaction_properties=_PROVENANCE,
    )

    status, code, detail = write(served)

    assert ((status, code), names in detail) == (answer, True), detail
    assert lance.dataset(namespace_client=stock, table_id=_SILVER).version == 1
    recorder = _Recorder()
    monkeypatch.setattr(app.state, "lineage_emitter", recorder, raising=False)
    lance.write_dataset(_keyed([2], ["b"]), namespace_client=stock, table_id=_SILVER, mode="overwrite", transaction_properties=_PROVENANCE)
    landed = lance.dataset(namespace_client=stock, table_id=_SILVER)
    assert (landed.version, _committed_properties(landed, 2)) == (2, _PROVENANCE)
    assert [(write["version"], write["inputs"]) for write in recorder.writes] == [(2, [InputRef("db", "db$src", 1, None)])]
    # A restore brings back rows the table held, here a fragment the overwrite dropped, so it names no run.
    landed.checkout_version(1).restore()
    assert _rows(lance.dataset(namespace_client=stock, table_id=_SILVER)) == [(1, "a")]


def _stock_append_naming(served: Served, inputs: str) -> tuple[int, str]:
    provenance = {"rask.run_id": "run-7", "rask.author": "alice", "rask.inputs": inputs}
    with pytest.raises(OSError):
        lance.write_dataset(_keyed([4], ["d"]), namespace_client=served.stock(), table_id=_TABLE, mode="append", transaction_properties=provenance)
    _, code, detail = _answer_to_the_staged_commit(served, "db$t", 2)
    return code, detail


def _insert_door_naming(served: Served, inputs: str) -> tuple[int, str]:
    with pytest.raises(lance_namespace.PermissionDeniedError) as refused:
        _insert(served.stock({"x-rask-run-id": "run-7", "x-rask-inputs": inputs}))
    return refused.value.code, str(refused.value)


@pytest.mark.parametrize("commit", [pytest.param(_stock_append_naming, id="a-stock-commit"), pytest.param(_insert_door_naming, id="the-insert-door")])
def test_a_commit_naming_an_input_the_caller_cannot_read_is_refused(served: Served, commit: Callable[[Served, str], tuple[int, str]]) -> None:
    stock = _with_source(served)
    served.denied.add("can_read_data table:db$src")

    code, detail = commit(served, json.dumps([{"table": ["db", "src"], "version": 1}]))

    assert (code, "can_read_data required on table:db$src" in detail) == (15, True), detail
    assert _opened(stock).version == 1
