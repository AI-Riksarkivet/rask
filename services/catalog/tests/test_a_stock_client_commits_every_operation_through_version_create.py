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

import asyncio
import contextlib
import json
import logging
import re
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
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
from lance.commit import CommitConflictError
from lance.dataset import Index, Transaction
from lance.optimize import Compaction
from moto.server import ThreadedMotoServer
from pydantic import BaseModel

from catalog.core import manifest_bytes
from catalog.services import dataplane, table_claims
from lakehouse_kit import base_refs, commit_records, commit_turns, maintenance_due, records
from service_kit.governed import fga as fga_module
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from service_kit.storage_options import StorageOptions
from storage import S3Client
from tests.integration import store_requests


_TOKEN = {"Authorization": "Bearer t"}
_TABLE = ["db", "t"]
#: What ``writer`` and ``maintainer`` on a table grant (``model.fga``: writer -> reader -> can_read_data, can_get_metadata;
#: either one -> can_create_version).
_WRITER_AND_MAINTAINER = frozenset({"can_get_metadata", "can_read_data", "can_write_data", "can_maintain", "can_create_version"})
#: What ``reader`` and ``maintainer`` grant: a compaction job's rungs, which read every row and change none, so it may
#: commit a version and holds no ``can_write_data``.
_READER_AND_MAINTAINER = frozenset({"can_get_metadata", "can_read_data", "can_maintain", "can_create_version"})


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


@pytest.fixture
def catalogs_store(moto_url: str) -> Iterator[store_requests.CountedStore]:
    """The catalog's own way to the moto store: a proxy that answers the requests a test names 503, as a store that cannot serve them."""
    with store_requests.counted(moto_url) as store:
        yield store


@pytest.fixture
def served_through_a_proxy(moto_url: str, catalogs_store: store_requests.CountedStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """``served_on_s3`` with the catalog reaching its store through ``catalogs_store``, while the client reaches the store
    directly at the endpoint the catalog vends, as an off-cluster writer does (``LANCE_S3_VENDED_ENDPOINT``, [[LH-489]])."""
    bucket = f"lh545-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_url, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test").create_bucket(Bucket=bucket)
    env = {
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": catalogs_store.url,
        "LANCE_S3_VENDED_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        # A request the store cannot serve fails the door at once rather than after Lance's own retries.
        "LANCE_STORE_MAX_RETRIES": "0",
        "LANCE_DATA_STORE_MAX_RETRIES": "0",
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
    verifier.verify.return_value = PersonPrincipal(
        principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    )
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
        # The append names its own author, which `version/create` admits for the caller it verified.
        lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append", transaction_properties={"rask.author": "dex~alice"})
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
    # The catalog recorded its verified caller as each version's author, the one naming itself included (ADR 0187).
    from catalog.core.config import get_settings

    # Each names the manifest it judged, which is the one that landed: the backend finalizes those bytes unchanged.
    settings, table_uuid = get_settings(), str((stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).properties or {})["rask.uuid"])
    signed = []
    for version in range(1, 7):
        transaction = restored.read_transaction(version)
        assert transaction is not None
        record = commit_records.recorded(
            settings.registry_root, settings.storage_options(), table_uuid, transaction_uuid=transaction.uuid, version=version, ref=""
        )
        assert record is not None, version
        signed.append((record.author, record.manifest == dataplane.committed_manifest_digest(restored.uri, StorageOptions(), version)))
    assert signed == [("dex~alice", True)] * 6


def _compact_distributed(dataset: lance.LanceDataset) -> None:
    plan = Compaction.plan(dataset, {"target_rows_per_fragment": 1024})
    Compaction.commit(dataset, [task.execute(dataset) for task in plan.tasks])


def _compact_files(dataset: lance.LanceDataset) -> None:
    dataset.optimize.compact_files(target_rows_per_fragment=1024)


@pytest.mark.parametrize(
    "compact", [pytest.param(_compact_distributed, id="the-distributed-plan-and-commit"), pytest.param(_compact_files, id="compact-files")]
)
def test_a_stock_compaction_by_a_reader_and_maintainer_commits_through_version_create(
    served_on_s3: Served, caplog: pytest.LogCaptureFixture, compact: Callable[[lance.LanceDataset], None]
) -> None:
    """Lance's own compactions (``lance_docs/lance/format/table/transaction.md`` § Rewrite) reserve the new fragment ids, then
    commit the rewrite; measured on pylance 13.0.0, ``compact_files`` makes the same two commits as the distributed
    protocol. Both are commits a stock client stages, so both are judged here, the rewrite row by row on its values
    ([[LH-561]]), and the job that stages them holds no writer rung: a maintainer rewrites how a table is stored and
    never what it says (``model.fga``)."""
    stock = _created(served_on_s3)
    for row, value in ((4, "d"), (5, "e"), (6, "f")):
        lance.write_dataset(_keyed([row], [value]), namespace_client=stock, table_id=_TABLE, mode="append", storage_options=served_on_s3.storage_options)
    _sweep_wrote_a_clone_list(stock, clones_of_the_table=[])
    served_on_s3.granted = _READER_AND_MAINTAINER
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="lance.audit"):
        compact(_opened(stock, served_on_s3.storage_options))

    compacted = _opened(stock, served_on_s3.storage_options)
    assert (compacted.version, len(compacted.get_fragments())) == (6, 1)
    assert _rows(compacted) == [(1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e"), (6, "f")]
    committed = [(getattr(r, "audit.version"), getattr(r, "audit.operation")) for r in caplog.records if getattr(r, "audit.action", None) == "commit_version"]
    assert committed == [(5, "ReserveFragments"), (6, "Rewrite")]


def test_the_index_door_refuses_and_a_writers_own_index_build_commits_through_version_create(served: Served, caplog: pytest.LogCaptureFixture) -> None:
    """The catalog builds no index (ADR 0162): its spec door answers 406 naming the bring-your-own path, and a writer's
    own pylance builds the index and later folds new fragments into it, each commit judged at ``version/create``."""
    stock = _created(served)
    served.granted = frozenset({"can_get_metadata", "can_read_data", "can_write_data", "can_create_version"})

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


def test_an_erasure_job_commits_every_rewrite_through_version_create_and_the_catalog_releases_what_it_verifies(
    served_on_s3: Served, caplog: pytest.LogCaptureFixture
) -> None:
    """[[LH-493]], [[LH-490]], ADR 0163: the erasure door records an obligation, and every rewrite is a bring-your-own job's.

    The job holds ``writer`` and ``maintainer`` and no owner rung. With stock pylance it deletes the subject by row id,
    compacts with Lance's own protocol and drops the index, on main and on the branch that inherited the subject, each
    a commit judged here; the catalog's verification then releases the obligation, no version lands that a stock
    commit did not make, and the job rebuilds its index."""
    stock = _created(served_on_s3)
    options = served_on_s3.storage_options
    lance.write_dataset(_keyed([4, 5], ["d", "e"]), namespace_client=stock, table_id=_TABLE, mode="append", storage_options=options)
    _opened(stock, options).create_scalar_index("v", index_type="BTREE", name="v_idx")
    stock.create_table_branch(lance_namespace.CreateTableBranchRequest(id=_TABLE, name="work"))
    _sweep_wrote_a_clone_list(stock, clones_of_the_table=[])
    served_on_s3.granted = frozenset({*_WRITER_AND_MAINTAINER, "can_drop"})
    recorded = httpx.post(f"{served_on_s3.url}/management/v1/table/db$t/erasure", json={"predicate": "id = 2"}, headers=_TOKEN)
    assert recorded.status_code == 202, recorded.text
    before = _opened(stock, options).version
    served_on_s3.granted = _WRITER_AND_MAINTAINER
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="lance.audit"):
        for head in (lambda: _opened(stock, options), lambda: _branch(stock, options)):
            job = head()
            ids = job.to_table(columns=[], filter="id = 2", with_row_id=True).column("_rowid").to_pylist()
            job.delete(f"_rowid IN ({', '.join(str(row_id) for row_id in ids)})")
            job = head()
            Compaction.commit(job, [task.execute(job) for task in Compaction.plan(job, {"materialize_deletions_threshold": 0.0}).tasks])
            head().drop_index("v_idx")
        verified = httpx.post(
            f"{served_on_s3.url}/management/v1/table/db$t/erasure/{recorded.json()['obligation_id']}/verify", json={"predicate": "id = 2"}, headers=_TOKEN
        )
        _opened(stock, options).create_scalar_index("v", index_type="BTREE", name="v_idx")

    assert verified.status_code == 200, verified.text
    assert (verified.json()["obligation"]["state"], verified.json()["report"]["complete"]) == ("released", True), verified.json()["report"]["surfaces"]
    rewrite = ["Delete", "ReserveFragments", "Rewrite", "CreateIndex"]
    assert [(branch, operation) for branch, _, operation in _commits(caplog)] == [
        *((None, operation) for operation in rewrite),
        *(("work", operation) for operation in rewrite),
        (None, "CreateIndex"),
    ]
    assert _opened(stock, options).version == before + len(rewrite) + 1, "a version no stock commit made has landed"
    assert _rows(_opened(stock, options)) == _rows(_branch(stock, options)) == [(1, "a"), (3, "c"), (4, "d"), (5, "e")]


def _branch(stock: lance_namespace.LanceNamespace, storage_options: dict[str, str] | None = None) -> lance.LanceDataset:
    """The head of branch ``work``, opened as a stock client opens it: a main ``DescribeTable``, then the ref."""
    return _opened(stock, storage_options).checkout_version(("work", None))


def _commits(caplog: pytest.LogCaptureFixture) -> list[tuple[str | None, int, str]]:
    """Each version the catalog finalized, as ``(branch, version, operation)``; ``None`` names main."""
    return [
        (getattr(r, "audit.branch", None), getattr(r, "audit.version"), getattr(r, "audit.operation"))
        for r in caplog.records
        if getattr(r, "audit.action", None) == "commit_version"
    ]


def test_a_stock_client_commits_on_a_branch_through_version_create_and_main_is_unchanged(served_on_s3: Served, caplog: pytest.LogCaptureFixture) -> None:
    """[[LH-490]]: the spec's ``CreateTableBranch`` creates the branch, and every commit a stock client then makes on it is
    staged under ``tree/<branch>/`` and judged at ``version/create`` with ``branch`` (``lance_docs/ns_catalog/spec.yaml``
    § CreateTableVersion; the branch layout is ``lance_docs/lance/format/table/branch_tag.md`` § Branch Dataset Layout).

    A branch reads its parent's fragments through a dataset-root base on the table's own root, so the compaction copies
    them into the branch, and that base is the table's own history, not a clone's source."""
    stock = _created(served_on_s3)
    options = served_on_s3.storage_options
    stock.create_table_branch(lance_namespace.CreateTableBranchRequest(id=_TABLE, name="work"))
    _sweep_wrote_a_clone_list(stock, clones_of_the_table=[])
    served_on_s3.granted = _WRITER_AND_MAINTAINER
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="lance.audit"):
        lance.write_dataset(_keyed([4], ["d"]), _branch(stock, options), mode="append")
        _branch(stock, options).delete("id = 2")
        _branch(stock, options).update({"v": "'A'"}, where="id = 1")
        work = _branch(stock, options)
        Compaction.commit(work, [task.execute(work) for task in Compaction.plan(work, {"materialize_deletions_threshold": 0.0}).tasks])
        _branch(stock, options).create_scalar_index("v", index_type="BTREE", name="v_idx")
        _branch(stock, options).drop_index("v_idx")

    assert _commits(caplog) == [
        ("work", 2, "Append"),
        ("work", 3, "Delete"),
        ("work", 4, "Update"),
        ("work", 5, "ReserveFragments"),
        ("work", 6, "Rewrite"),
        ("work", 7, "CreateIndex"),
        ("work", 8, "CreateIndex"),
    ]
    branch = _branch(stock, options)
    assert (branch.version, len(branch.get_fragments()), _rows(branch)) == (8, 1, [(1, "A"), (3, "c"), (4, "d")])
    main = _opened(stock, options)
    assert (main.version, _rows(main)) == (1, [(1, "a"), (2, "b"), (3, "c")])


def _append_at_file_version_2_1(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([9], ["z"]), namespace_client=stock, table_id=_TABLE, mode="append", data_storage_version="2.1")


def _create_at_file_version_2_1(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([9], ["z"]), namespace_client=stock, table_id=["db", "u"], mode="create", data_storage_version="2.1", enable_stable_row_ids=True)


def _append_a_new_key(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([9], ["z"]), namespace_client=stock, table_id=_TABLE, mode="append")


def _create_a_table(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([9], ["z"]), namespace_client=stock, table_id=["db", "u"], mode="create", data_storage_version="2.2", enable_stable_row_ids=True)


def _create_a_labelled_table(stock: lance_namespace.LanceNamespace) -> None:
    labelled = _keyed([9], ["z"])
    fields = [field.with_metadata({**(field.metadata or {}), b"rask.classification": b"pii"}) if field.name == "v" else field for field in labelled.schema]
    rows = labelled.cast(pa.schema(fields, metadata=labelled.schema.metadata))
    lance.write_dataset(rows, namespace_client=stock, table_id=["db", "u"], mode="create", data_storage_version="2.2", enable_stable_row_ids=True)


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


def _steer_compaction(stock: lance_namespace.LanceNamespace) -> None:
    _opened(stock).update_config({"lance.compaction.max_source_fragments": "2"})


def _stamp_a_scheme_lance_does_not_know(stock: lance_namespace.LanceNamespace) -> None:
    _opened(stock).update_field_metadata({"v": {"lance-encoding:compression": "bogus"}})


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


def _rewrite(stock: lance_namespace.LanceNamespace, *, compacted: list[int], replaced: list[int], values: list[int] | None = None) -> None:
    """A Rewrite of a keyless ``db$u`` (fragments ``[1, 2]``, ``[3]``, ``[4]``) claiming to replace ``replaced``.

    Its one new fragment is what Lance's own compaction task writes for fragments ``compacted``, so it carries their
    rows and stable row ids, unless ``values`` names the rows its files hold instead, under those same row ids.
    Measured on pylance 13.0.0, Lance commits the claim whatever fragments it names and whatever its files hold.
    Keyless, because on a keyed table the primary key judge refuses a duplicated row first.
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
    files = written.files if values is None else lance.fragment.write_fragments(pa.table({"n": values}), dataset)[0].files
    new = lance.FragmentMetadata(id=max(held) + 1, files=files, physical_rows=written.physical_rows, row_id_meta=written.row_id_meta)
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


def _rewrite_changing_a_carried_rows_value(stock: lance_namespace.LanceNamespace) -> None:
    _rewrite(stock, compacted=[0, 1], replaced=[0, 1], values=[1, 2, 30])


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


def _append_under_a_transaction_recorded_for_another_commit(stock: lance_namespace.LanceNamespace) -> None:
    """An append staged as version 4 under a transaction the catalog recorded at version 4 for another caller's commit, still
    in flight: its uuid is on that commit's staged manifest, which every reader of the table sees (ADR 0187)."""
    from catalog.core.config import get_settings

    settings, dataset = get_settings(), _opened(stock)
    held = str(uuid.uuid4())
    table_uuid = str((stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).properties or {})["rask.uuid"])
    commit_records.record_author(
        settings.registry_root,
        settings.storage_options(),
        table_uuid,
        transaction_uuid=held,
        version=dataset.version + 1,
        ref="",
        author="dex~bob",
        manifest=commit_records.manifest_digest(b"the other commit's staged manifest"),
    )
    (written,) = lance.fragment.write_fragments(_keyed([5], ["e"]), dataset)
    lance.LanceDataset.commit(
        dataset,
        Transaction(read_version=dataset.version, operation=lance.LanceOperation.Append([written]), uuid=held),
        namespace_client=stock,
        table_id=_TABLE,
        namespace_client_managed_versioning=True,
    )


def _append_under_a_transaction_whose_record_is_unreadable(stock: lance_namespace.LanceNamespace) -> None:
    """An append staged as version 4 under a transaction whose record at version 4 the store holds but that does not read
    as one: it signs nothing, and is never taken for this commit's."""
    from catalog.core.config import get_settings

    settings, dataset = get_settings(), _opened(stock)
    held = str(uuid.uuid4())
    table_uuid = str((stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).properties or {})["rask.uuid"])
    unreadable = Path(settings.registry_root) / commit_records.AUTHORS_PREFIX / table_uuid / held / f"{dataset.version + 1}.json"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_bytes(b'{"author": ')
    (written,) = lance.fragment.write_fragments(_keyed([5], ["e"]), dataset)
    lance.LanceDataset.commit(
        dataset,
        Transaction(read_version=dataset.version, operation=lance.LanceOperation.Append([written]), uuid=held),
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
        pytest.param(_append_at_file_version_2_1, set(), False, 409, 14, ("db$t", 3), id="a-version-the-table-already-holds-is-taken"),
        pytest.param(_arm_auto_cleanup, set(), False, 400, 13, ("db$t", 4), id="auto-cleanup-is-not-armed-by-a-commit"),
        pytest.param(_steer_compaction, set(), False, 400, 13, ("db$t", 4), id="compaction-is-not-steered-by-a-commit"),
        pytest.param(_stamp_a_scheme_lance_does_not_know, set(), False, 400, 13, ("db$t", 4), id="an-encoding-knob-lance-cannot-honour-is-not-stamped"),
        pytest.param(_restore_version_1, {"can_restore"}, False, 403, 15, ("db$t", 4), id="a-writer-cannot-stage-a-restore"),
        pytest.param(_overwrite, set(), True, 409, 19, ("db$t", 4), id="an-overwrite-of-a-protected-table-is-refused"),
        pytest.param(_compact, {"can_maintain"}, False, 403, 15, ("db$t", 4), id="a-writer-cannot-stage-a-compaction"),
        pytest.param(_append_a_new_key, {"can_write_data"}, False, 403, 15, ("db$t", 4), id="a-maintainer-cannot-stage-an-append"),
        pytest.param(_create_a_table, {"can_write_data"}, False, 403, 15, ("db$u", 1), id="a-maintainer-cannot-commit-a-tables-first-version"),
        pytest.param(_create_a_labelled_table, {"can_classify"}, False, 403, 15, ("db$u", 1), id="a-first-version-labels-a-field-only-for-a-classifier"),
        pytest.param(_compact, set(), False, 503, 17, ("db$t", 4), id="a-compaction-waits-for-the-sweeps-clone-list"),
        pytest.param(_compact_on_a_clone_list_past_its_age, set(), False, 503, 17, ("db$t", 4), id="a-compaction-waits-for-a-fresh-clone-list"),
        pytest.param(_compact_a_table_a_clone_reads_through, set(), False, 406, 0, ("db$t", 4), id="a-table-a-clone-reads-through-is-not-compacted"),
        pytest.param(_rewrite_losing_a_replaced_fragments_rows, set(), False, 400, 13, ("db$u", 4), id="a-rewrite-loses-no-row"),
        pytest.param(_rewrite_duplicating_a_carried_fragments_rows, set(), False, 400, 13, ("db$u", 4), id="a-rewrite-duplicates-no-row"),
        pytest.param(_rewrite_changing_a_carried_rows_value, set(), False, 400, 13, ("db$u", 4), id="a-rewrite-changes-no-value"),
        pytest.param(_index_read_through_a_base, set(), False, 400, 13, ("db$t", 4), id="an-index-is-read-from-the-tables-own-indices"),
        pytest.param(_index_with_no_files, set(), False, 400, 13, ("db$t", 4), id="an-index-names-files-that-are-there"),
        pytest.param(_data_staged_under_an_index_label, set(), False, 400, 13, ("db$t", 4), id="an-index-commit-changes-no-data"),
        pytest.param(
            _append_under_a_transaction_recorded_for_another_commit, set(), False, 400, 13, ("db$t", 4), id="another-commits-transaction-is-not-reused"
        ),
        pytest.param(_append_under_a_transaction_whose_record_is_unreadable, set(), False, 400, 13, ("db$t", 4), id="an-unreadable-record-is-no-retrys"),
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


def _answer_to_the_staged_commit(served: Served, table: str, version: int, *, branch: str | None = None, location: str | None = None) -> tuple[int, int, str]:
    """The status, spec code and detail the catalog answers the one staged manifest of ``table`` (or of its ``branch``)
    at ``version``; the table's ``location`` is described unless given.

    pylance reports a refused finalize as an unknown outcome and keeps its staged manifest; asking for it again shows
    the answer the client was given.
    """
    ref = f"tree/{branch}/" if branch is not None else ""
    if location is None:
        location = httpx.post(f"{served.url}/v1/table/{table}/describe", json={"vend_credentials": False}, headers=_TOKEN).json()["location"]
    pending = _staged_manifests(served, location, ref)
    assert len(pending) == 1, f"one staged manifest of {table} awaits its answer, found {pending}: none means the commit landed"
    (staged,) = pending
    request = {"version": version, "manifest_path": staged, **({"branch": branch} if branch is not None else {})}
    again = httpx.post(f"{served.url}/v1/table/{table}/version/create", json=request, headers=_TOKEN)
    body = again.json()
    return again.status_code, body.get("code"), body.get("detail", "")


def _staged_manifests(served: Served, location: str, ref: str) -> list[str]:
    """The object-store path pylance names each manifest staged under the ref's ``_versions/`` by, read off the store."""
    if served.root is not None:
        return [path.relative_to("/").as_posix() for path in Path(location.removeprefix("file://")).glob(f"{ref}_versions/*.manifest-*")]
    bucket, _, key = location.removeprefix("s3://").partition("/")
    listed = _s3(served).list_objects_v2(Bucket=bucket, Prefix=f"{key}/{ref}_versions/")
    return [item["Key"] for item in listed.get("Contents", []) if ".manifest-" in item["Key"]]


def _s3(served: Served) -> S3Client:
    """A client of the store at the endpoint and key the stock client reaches it with."""
    options = served.storage_options
    return boto3.client(
        "s3",
        endpoint_url=options["aws_endpoint"],
        region_name=options["aws_region"],
        aws_access_key_id=options["aws_access_key_id"],
        aws_secret_access_key=options["aws_secret_access_key"],
    )


def _its_versions_listed_once_a_commit_is_staged(served: Served, location: str) -> Callable[[store_requests.StoreRequest], bool]:
    """The listing of the table's ``_versions/`` every open of it makes to find its latest version, from the moment a commit
    is staged there: the listings before it are the describes the client opens the table through."""
    bucket, _, key = location.removeprefix("s3://").partition("/")
    return lambda request: request.lists(f"{key}/_versions/") and bool(_staged_manifests(served, f"s3://{bucket}/{key}", ""))


def _its_staged_manifest_read(served: Served, location: str) -> Callable[[store_requests.StoreRequest], bool]:
    """The catalog's read of the manifest the client staged, which it reads through pyarrow's S3 filesystem."""
    staged = re.compile(rf"/{re.escape(location.removeprefix('s3://'))}/_versions/[0-9]+\.manifest-.+")
    return lambda request: request.method in ("HEAD", "GET") and staged.fullmatch(request.path) is not None


def _its_staged_version_read(served: Served, location: str) -> Callable[[store_requests.StoreRequest], bool]:
    """The reads of the detached copy the catalog plants to open the staged version through (``managed_versions.stage``)."""
    detached = re.compile(rf"/{re.escape(location.removeprefix('s3://'))}/_versions/d[0-9]+\.manifest")
    return lambda request: request.method in ("HEAD", "GET") and detached.fullmatch(request.path) is not None


@pytest.mark.parametrize(
    "failing",
    [
        pytest.param(_its_versions_listed_once_a_commit_is_staged, id="the-tables-latest-version"),
        pytest.param(_its_staged_manifest_read, id="the-staged-manifest"),
        pytest.param(_its_staged_version_read, id="the-staged-version"),
    ],
)
def test_a_commit_whose_store_read_fails_is_service_unavailable_and_leaves_the_table_at_its_version(
    served_through_a_proxy: Served,
    catalogs_store: store_requests.CountedStore,
    failing: Callable[[Served, str], Callable[[store_requests.StoreRequest], bool]],
) -> None:
    """[[LH-545]]: a read the judgment needs that the store cannot serve says nothing about the commit, so it is the
    spec's 17, which a stock client waits on and retries, never the 13 it takes for its own malformed commit and drops
    (`lance_docs/ns_catalog/namespace/operations/errors.md`)."""
    served = served_through_a_proxy
    stock = _created(served)
    location = str(stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).location)

    with catalogs_store.answering_unavailable(failing(served, location)):
        # pylance reports the refused finalize as an OSError; the catalog's own answer is read back below.
        with contextlib.suppress(OSError):
            lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append", storage_options=served.storage_options)
        status, code, detail = _answer_to_the_staged_commit(served, "db$t", 2, location=location)

    assert (status, code) == (503, 17), detail
    after = _opened(stock, served.storage_options)
    assert (after.version, _rows(after)) == (1, [(1, "a"), (2, "b"), (3, "c")])


def test_a_commit_on_a_table_whose_latest_version_does_not_open_is_refused_as_internal_and_lands_nothing(served: Served) -> None:
    """[[LH-545]]: only a table that holds no committed version follows version 0 (``dataplane.holds_a_version``). One whose
    latest committed manifest Lance cannot read is no table with none: read as one, the version after none is the slot a
    reclaimed version 1 left, which the backend's put-if-not-exists would fill as a create."""
    stock = _created(served)
    lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")
    location = str(stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).location)
    versions = Path(location.removeprefix("file://")) / "_versions"
    latest = versions / f"{(1 << 64) - 1 - 2}.manifest"
    staged = versions / f"{(1 << 64) - 1 - 3}.manifest-{uuid.uuid4()}"
    staged.write_bytes(manifest_bytes.with_manifest_version(latest.read_bytes(), 3))
    latest.write_bytes(b"not a manifest")

    status, code, detail = _answer_to_the_staged_commit(served, "db$t", 3, location=location)

    assert (status, code) == (500, 18), detail
    assert sorted(path.name for path in versions.glob("*.manifest")) == sorted({latest.name, f"{(1 << 64) - 1 - 1}.manifest"})


def _create_a_branch_with_pylance(stock: lance_namespace.LanceNamespace) -> None:
    """Lance's own ``create_branch``: a ``Clone`` committed on a branch no ref names yet, then the ref written by the client."""
    _opened(stock).create_branch("new")


def _append_a_held_key_on_the_branch(stock: lance_namespace.LanceNamespace) -> None:
    lance.write_dataset(_keyed([1], ["again"]), _branch(stock), mode="append")


@pytest.mark.parametrize(
    ("stage", "staged_at", "answer", "names"),
    [
        pytest.param(_create_a_branch_with_pylance, ("new", 1), (404, 22), "CreateTableBranch", id="a-branch-is-created-through-create-table-branch"),
        pytest.param(_append_a_held_key_on_the_branch, ("work", 3), (400, 13), "primary key", id="a-held-primary-key-is-refused-on-a-branch"),
        pytest.param(
            _append_a_held_key_on_the_branch,
            ("work", 2),
            (409, 14),
            "version 2 is taken, the latest version is 2",
            id="a-version-the-branch-head-already-holds-is-taken",
        ),
    ],
)
def test_a_refused_branch_commit_leaves_the_branch_at_its_version_and_answers_a_spec_error(
    served: Served, stage: Callable[[lance_namespace.LanceNamespace], None], staged_at: tuple[str, int], answer: tuple[int, int], names: str
) -> None:
    """[[LH-490]]: a branch commit is judged like main's, and a refusal leaves every ref at its version. A branch is created
    through ``CreateTableBranch``: a stock ``create_branch`` would write ``_refs/branches/<name>.json`` itself after its
    commit (Lance ``rust/lance/src/dataset.rs`` ``create_branch`` at v13.0.0), and no writer is vended ``_refs/``."""
    stock = _created(served)
    stock.create_table_branch(lance_namespace.CreateTableBranchRequest(id=_TABLE, name="work"))
    lance.write_dataset(_keyed([4], ["d"]), _branch(stock), mode="append")

    # pylance reports a refused finalize as an OSError; the catalog's own answer is read back below.
    with contextlib.suppress(OSError):
        stage(stock)

    branch, version = staged_at
    status, code, detail = _answer_to_the_staged_commit(served, "db$t", version, branch=branch)
    assert ((status, code), names in detail) == (answer, True), detail
    assert list(_opened(stock).branches.list()) == ["work"]
    work = _branch(stock)
    assert (work.version, _rows(work)) == (2, [(1, "a"), (2, "b"), (3, "c"), (4, "d")])
    assert _opened(stock).version == 1


def test_what_lands_is_what_was_judged_whatever_the_writer_puts_over_its_staged_manifest_meanwhile(served: Served, monkeypatch: pytest.MonkeyPatch) -> None:
    """The write tier may put over its own staged key until the finalize; the catalog finalizes the bytes it judged."""
    from catalog.services import managed_versions

    judged_then = managed_versions.discard

    def swap_after_judging(staged: managed_versions.StagedVersion, so: StorageOptions) -> None:
        Path(staged.staged_uri.removeprefix("file://")).write_bytes(b"not the manifest that was judged")
        judged_then(staged, so)

    stock = _created(served)
    monkeypatch.setattr(managed_versions, "discard", swap_after_judging)

    lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")

    landed = _opened(stock)
    assert landed.version == 2
    assert _rows(landed) == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


def test_a_commit_whose_answer_is_lost_lands_once_and_the_client_gets_its_version_back(served: Served, monkeypatch: pytest.MonkeyPatch) -> None:
    """[[LH-491]]: after a failed commit attempt Lance reads the target version back and keeps the commit as its own
    when the transaction there, the manifest's inline one, equals the one it staged (``verify_commit_outcome`` in
    ``rust/lance/src/io/commit.rs`` at v13.0.0; ``lance_docs/lance/format/table/transaction.md`` § Commit Algorithm covers only the rebase
    and retry). That holds only while the catalog finalizes the writer's transaction unchanged: measured on pylance 13.0.0
    with a finalize that changed its uuid, Lance read the landed version as another writer's, rebased and re-staged the
    same fragment as the next version, and reported the commit's outcome unknown."""
    from catalog.services import managed_versions

    finalize = managed_versions.finalize
    answered: list[int] = []

    def answer_lost_after_the_finalize(*args: Any, **kwargs: Any) -> Any:
        landed = finalize(*args, **kwargs)
        assert landed.version is not None
        answered.append(landed.version.version)
        if len(answered) == 1:
            raise lance_namespace.ServiceUnavailableError("the answer to a finalized commit was lost on its way back")
        return landed

    stock = _created(served)
    monkeypatch.setattr(managed_versions, "finalize", answer_lost_after_the_finalize)

    written = lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")

    table = _opened(stock)
    assert (written.version, answered, [entry["version"] for entry in table.versions()]) == (2, [2], [1, 2])
    assert _rows(table) == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]
    # The request whose answer was lost failed after its version landed, so its author record stands (ADR 0187).
    from catalog.core.config import get_settings

    settings, transaction = get_settings(), table.read_transaction(2)
    assert transaction is not None
    table_uuid = str((stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).properties or {})["rask.uuid"])
    record = commit_records.recorded(settings.registry_root, settings.storage_options(), table_uuid, transaction_uuid=transaction.uuid, version=2, ref="")
    assert record is not None
    assert (record.author, record.manifest) == ("dex~alice", dataplane.committed_manifest_digest(table.uri, StorageOptions(), 2))


def test_a_version_landed_past_every_door_under_an_in_flight_commits_transaction_is_signed_for_no_one(served: Served, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0187: a writer past every door reads an in-flight stock commit's transaction uuid off its staged manifest and
    lands that version first, with other rows (pylance 13.0.0 commits a transaction under any uuid it is given,
    measured). The commit's finalize loses, and the delete of its author record is refused here, so a record names that
    transaction at that version; it names the manifest the catalog judged too, which is not the one there, so the
    reconciler signs the version for no one and signs the commit's retry, a version later, for its caller."""
    from catalog.core.config import get_settings
    from catalog.services import commit_reconcile, managed_versions

    finalize = managed_versions.finalize
    forged: list[int] = []

    def beaten_to_its_version(table: table_claims.DoorTable, staged: managed_versions.StagedVersion, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — finalize's own arguments and answer
        if not forged:
            root = staged.table_location
            planned = lance.fragment.write_fragments(_keyed([9], ["forged"]), root, return_transaction=True, data_storage_version="2.2")
            lance.LanceDataset.commit(root, replace(planned, read_version=staged.version - 1, uuid=staged.transaction_uuid))
            forged.append(staged.version)
        return finalize(table, staged, *args, **kwargs)

    def refused(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — the delete's own arguments
        raise OSError("the store refused the delete")

    stock = _created(served)
    monkeypatch.setattr(managed_versions, "finalize", beaten_to_its_version)
    monkeypatch.setattr(commit_records, "forget_author", refused)

    lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")

    landed = _opened(stock)
    (taken,) = forged
    held, retried = landed.read_transaction(taken), landed.read_transaction(taken + 1)
    assert held is not None and retried is not None and held.uuid == retried.uuid
    settings = get_settings()
    table_uuid = str((stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).properties or {})["rask.uuid"])
    scan = commit_reconcile.scan_ref(
        landed.uri,
        StorageOptions(),
        branch=None,
        branch_identifier=None,
        mark=taken - 1,
        announced=set(),
        after=datetime.now(UTC) - timedelta(hours=1),
        before=datetime.now(UTC) + timedelta(hours=1),
        record_of=lambda transaction_uuid, version, ref: commit_records.recorded(
            settings.registry_root, settings.storage_options(), table_uuid, transaction_uuid=transaction_uuid, version=version, ref=ref
        ),
        authenticates=True,
        unannounced=lambda _operation: False,
    )
    assert (scan.unauthored, [(commit.version, commit.author) for commit in scan.missing]) == ((taken,), [(taken + 1, "dex~alice")])
    # The record that signs nothing there is forgotten with the version it was counted for.
    assert [commit.version for commit in scan.forget] == [taken]


def test_a_retry_of_an_in_flight_commit_leaves_the_record_its_twin_made(served: Served, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0187: a request that finds its commit's author already recorded, a retry of that commit made while the first
    request is in flight, deletes nothing when it fails before its finalize: the first request lands its version, and
    the record names its author."""
    from catalog.core.config import get_settings

    waited = table_claims.DoorTable.wait_for_turn_in_loop
    posted: list[str] = []
    twins: list[int] = []

    async def _a_twin_while_waiting(table: table_claims.DoorTable, ref: str | None, *, mine: str | None = None, handed: bool = True) -> bool:
        if posted:
            raise table_claims.TurnWaitRefusedError("every seat a commit waits in is taken")
        described = await asyncio.to_thread(httpx.post, f"{served.url}/v1/table/db$t/describe", json={"vend_credentials": False}, headers=_TOKEN)
        (pending,) = Path(described.json()["location"].removeprefix("file://")).glob("_versions/*.manifest-*")
        # Off the event loop, which serves the describe a stock client's open sends.
        request = {"version": (await asyncio.to_thread(_opened, stock)).version + 1, "manifest_path": pending.relative_to("/").as_posix()}
        posted.append(request["manifest_path"])
        twins.append((await asyncio.to_thread(httpx.post, f"{served.url}/v1/table/db$t/version/create", json=request, headers=_TOKEN)).status_code)
        return await waited(table, ref, mine=mine, handed=handed)

    stock = _created(served)
    monkeypatch.setattr(table_claims.DoorTable, "wait_for_turn_in_loop", _a_twin_while_waiting)

    written = lance.write_dataset(_keyed([4], ["d"]), namespace_client=stock, table_id=_TABLE, mode="append")

    transaction = written.read_transaction(written.version)
    assert transaction is not None
    settings = get_settings()
    table_uuid = str((stock.describe_table(lance_namespace.DescribeTableRequest(id=_TABLE)).properties or {})["rask.uuid"])
    record = commit_records.recorded(
        settings.registry_root, settings.storage_options(), table_uuid, transaction_uuid=transaction.uuid, version=written.version, ref=""
    )
    assert (twins, written.version, record.author if record is not None else None) == ([409], 2, "dex~alice")


#: An unkeyed table, so the catalog's ``/insert`` appends through Lance's own commit rather than a keyed plan.
_APPENDED_TO = ["db", "w"]


class _Appender:
    """Appends one row after another through ``write``, on a thread of its own, until stopped; keeps what landed and what failed."""

    def __init__(self, write: Callable[[int], object], first_id: int) -> None:
        self.landed: list[int] = []
        self.failed: list[BaseException] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(write, first_id), daemon=True)
        self._thread.start()

    def _run(self, write: Callable[[int], object], row: int) -> None:
        while not self._stop.is_set():
            try:
                write(row)
            except BaseException as exc:  # noqa: BLE001 — every append's outcome is half of the claim
                self.failed.append(exc)
                return
            self.landed.append(row)
            row += 1

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=60)


def _row(row: int) -> pa.Table:
    return pa.table({"id": pa.array([row], pa.int64()), "v": pa.array([row], pa.int64())})


#: How late a stock writer's commit reaches the catalog on the estate: measured 2026-10-31, a ``version/create`` took 0.8 to
#: 2.4 s inside the catalog alone, and pylance waits 0 to 3 slots of 1.1 times that first attempt before its next.
_ESTATE_ROUND_TRIP_SECONDS = 2.5


class _SlowRoundTrip(lance_namespace.LanceNamespace):
    """pylance's REST namespace with each commit reaching the catalog ``delay`` seconds late, ``before`` called ahead of
    it; the other calls pylance makes for a commit pass straight through."""

    def __init__(self, inner: lance_namespace.LanceNamespace, delay: float, *, before: Callable[[], None] | None = None) -> None:
        self._inner, self._delay, self._before = inner, delay, before

    def namespace_id(self) -> str:
        return self._inner.namespace_id()

    def describe_table(self, request: lance_namespace.DescribeTableRequest) -> lance_namespace.DescribeTableResponse:
        return self._inner.describe_table(request)

    def list_table_versions(self, request: lance_namespace.ListTableVersionsRequest) -> lance_namespace.ListTableVersionsResponse:
        return self._inner.list_table_versions(request)

    def describe_table_version(self, request: lance_namespace.DescribeTableVersionRequest) -> lance_namespace.DescribeTableVersionResponse:
        return self._inner.describe_table_version(request)

    def create_table_version(self, request: lance_namespace.CreateTableVersionRequest) -> lance_namespace.CreateTableVersionResponse:
        if self._before is not None:
            self._before()
        time.sleep(self._delay)
        return self._inner.create_table_version(request)


@pytest.mark.parametrize(
    ("round_trip_seconds", "appends", "within_seconds"),
    [
        pytest.param(0.0, 30, 60, id="next-to-the-catalog"),
        pytest.param(_ESTATE_ROUND_TRIP_SECONDS, 3, 120, id="as-far-as-on-the-estate"),
    ],
)
def test_a_stock_writer_beside_a_writer_appending_through_insert_in_a_tight_loop_lands_every_append(
    served: Served, round_trip_seconds: float, appends: int, within_seconds: int
) -> None:
    """[[LH-259]] The catalog finalizes exactly the version a writer staged, so beside a writer the catalog's ``/insert``
    commits in-process in a tight loop, a stock writer, whose every retry is a round trip through ``version/create``, wins no
    version: measured without the turn, it landed 0 appends beside 639 and failed its commit once Lance's 30 s of retries
    ran out. A loss found either way, its staged version already taken (26 of 41 measured next to the catalog) or its
    finalize beaten, hands the writer the ref's next turn under its transaction's uuid, which pylance keeps when it stages
    the commit again (measured on pylance 13.0.0), for as long as Lance itself retries: as far from the catalog as on the
    estate, its retries came 6.2 to 11.7 s after each 409. Its retry lands while ``/insert`` waits, and neither fails."""
    stock = served.stock()
    stock.create_namespace(lance_namespace.CreateNamespaceRequest(id=["db"]))
    lance.write_dataset(_row(0), namespace_client=stock, table_id=_APPENDED_TO, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    through_version_create = _SlowRoundTrip(served.stock(), round_trip_seconds) if round_trip_seconds else served.stock()
    through_insert = served.stock()
    writers = [
        _Appender(lambda row: lance.write_dataset(_row(row), namespace_client=through_version_create, table_id=_APPENDED_TO, mode="append"), 1),
        _Appender(lambda row: through_insert.insert_into_table(lance_namespace.InsertIntoTableRequest(id=_APPENDED_TO, mode="append"), _ipc(_row(row))), 10**6),
    ]
    deadline = time.monotonic() + within_seconds
    while len(writers[0].landed) < appends and not any(writer.failed for writer in writers):
        assert time.monotonic() < deadline, f"the stock writer landed fewer than {appends} appends in {within_seconds} s"
        time.sleep(0.01)
    for writer in writers:
        writer.stop()

    ids = lance.dataset(namespace_client=stock, table_id=_APPENDED_TO).to_table(columns=["id"]).column("id").to_pylist()
    assert ([writer.failed for writer in writers], sorted(ids)) == ([[], []], sorted([0, *writers[0].landed, *writers[1].landed]))


def test_a_stock_schema_change_that_loses_to_a_writer_appending_through_insert_holds_that_writer_no_turn(served: Served) -> None:
    """[[LH-259]] A stock writer's ``add_columns`` commits a Merge, which Lance never retries itself: a Merge that lost to an
    append is a conflict its application re-runs (``lance_docs/lance/format/table/transaction.md`` § Merge Compatibility), so
    pylance never stages it again, and the catalog hands it no turn. Measured with one handed: the writer appending through
    ``/insert`` waited 30 s for a retry that never came."""
    stock = served.stock()
    stock.create_namespace(lance_namespace.CreateNamespaceRequest(id=["db"]))
    lance.write_dataset(_row(0), namespace_client=stock, table_id=_APPENDED_TO, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    through_insert = served.stock()
    landed_at: list[float] = []

    def append(row: int) -> None:
        through_insert.insert_into_table(lance_namespace.InsertIntoTableRequest(id=_APPENDED_TO, mode="append"), _ipc(_row(row)))
        landed_at.append(time.monotonic())

    writer = _Appender(append, 10**6)
    try:
        deadline = time.monotonic() + 60
        while len(landed_at) < 3 and not writer.failed:
            assert time.monotonic() < deadline, "the writer appending through /insert never got going"
            time.sleep(0.01)
        with pytest.raises(CommitConflictError, match="Merge transaction was preempted by concurrent transaction Append"):
            lance.dataset(namespace_client=_SlowRoundTrip(served.stock(), _ESTATE_ROUND_TRIP_SECONDS), table_id=_APPENDED_TO).add_columns(
                {"n": "cast(NULL as string)"}
            )
        refused = time.monotonic()
        while sum(at > refused for at in landed_at) < 3 and not writer.failed:
            assert time.monotonic() < refused + 5, "the writer appending through /insert landed fewer than 3 appends in the 5 s after the change lost"
            time.sleep(0.01)
    finally:
        writer.stop()
    assert writer.failed == []


def _turns(stock: lance_namespace.LanceNamespace, tmp_path: Path) -> commit_turns.TableTurns:
    """The commit turns of ``_APPENDED_TO`` on the served catalog's control root, where a door's commit or a lost race takes
    them."""
    described = stock.describe_table(lance_namespace.DescribeTableRequest(id=_APPENDED_TO))
    return commit_turns.TableTurns(
        control_root=str(tmp_path / "control"), storage_options=StorageOptions(), table_uuid=(described.properties or {})["rask.uuid"]
    )


def test_a_stock_writer_commits_while_a_turn_handed_to_another_stock_writer_is_held(served: Served, tmp_path: Path) -> None:
    """[[LH-259]] A turn handed to a stock writer holds the catalog's own writers, which commit in-process far faster than a
    stock writer's round trip, and not that writer's peers, which race it as Lance's own backoff spreads them: measured with
    the peers waiting, 16 stock writers on one table at the estate's round trip landed 4 of 32 appends, and racing it 10 of
    32, as many as with no turn. The turn here is handed to a writer that never stages its commit again."""
    stock = served.stock()
    stock.create_namespace(lance_namespace.CreateNamespaceRequest(id=["db"]))
    lance.write_dataset(_row(0), namespace_client=stock, table_id=_APPENDED_TO, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    _turns(stock, tmp_path).hand(None, commit_turns.Writer(caller="a-peer", transaction="a-commit-it-never-stages-again"))

    began = time.monotonic()
    lance.write_dataset(_row(1), namespace_client=served.stock(), table_id=_APPENDED_TO, mode="append")

    assert time.monotonic() - began < 5


def test_a_stock_writer_whose_finalize_loses_is_handed_the_refs_next_turn(served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[[LH-259]] A stock writer's version can be taken after ``version/create`` found it free: by a writer committing
    while this one waits out a door's turn, right before its finalize. The finalize then loses, and the writer is handed
    the ref's next turn there, so its retry, staged again under its transaction, finalizes before the catalog's own
    writers: when the retry reaches ``version/create``, the record keeps a turn under the writer's own token, read from
    the store, and holds no door's."""
    stock = served.stock()
    stock.create_namespace(lance_namespace.CreateNamespaceRequest(id=["db"]))
    lance.write_dataset(_row(0), namespace_client=stock, table_id=_APPENDED_TO, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    competitor = served.stock()
    waited = table_claims.DoorTable.wait_for_turn_in_loop
    beaten: list[str] = []
    at_the_retry: list[tuple[bool, bool]] = []

    async def _waited_then_beaten(table: table_claims.DoorTable, ref: str | None, *, mine: str | None = None, handed: bool = True) -> bool:
        kept = await waited(table, ref, mine=mine, handed=handed)
        if mine is not None and not beaten:
            beaten.append(mine)
            await asyncio.to_thread(competitor.insert_into_table, lance_namespace.InsertIntoTableRequest(id=_APPENDED_TO, mode="append"), _ipc(_row(1)))
        return kept

    def _note_the_turn() -> None:
        if beaten:
            turns = _turns(stock, tmp_path)
            at_the_retry.append((turns.keeps(beaten[0]), turns.poll(None, handed=False)))

    monkeypatch.setattr(table_claims.DoorTable, "wait_for_turn_in_loop", _waited_then_beaten)

    lance.write_dataset(_row(2), namespace_client=_SlowRoundTrip(served.stock(), 0.0, before=_note_the_turn), table_id=_APPENDED_TO, mode="append")

    landed = lance.dataset(namespace_client=stock, table_id=_APPENDED_TO)
    ids = landed.to_table(columns=["id"]).column("id").to_pylist()
    assert (at_the_retry[:1], sorted(ids)) == ([(True, False)], [0, 1, 2])
    # Each version keeps its commit's author record, and the attempt whose finalize lost deleted its own: the retry staged
    # again under the same transaction landed a version later (ADR 0187).
    table_uuid = str((stock.describe_table(lance_namespace.DescribeTableRequest(id=_APPENDED_TO)).properties or {})["rask.uuid"])
    control = str(tmp_path / "control")
    authors = []
    for version in (1, 2, 3):
        transaction = landed.read_transaction(version)
        assert transaction is not None
        record = commit_records.recorded(control, StorageOptions(), table_uuid, transaction_uuid=transaction.uuid, version=version, ref="")
        authors.append(record.author if record is not None else None)
    kept = list((tmp_path / "control" / commit_records.AUTHORS_PREFIX / table_uuid).rglob("*.json"))
    assert (authors, len(kept)) == (["dex~alice"] * 3, 3)


@pytest.mark.parametrize(
    ("door", "may_fail_with"),
    [
        pytest.param("insert", (), id="writers-through-insert"),
        # Fifty stock writers then contend, each first attempt having lasted the hold, so Lance's own 30 s of retries can
        # run out for some ("Too many concurrent writers"): Lance's answer, not a catalog refusal.
        pytest.param("version-create", (OSError,), id="stock-writers-through-version-create"),
        pytest.param("delete", (lance_namespace.ConcurrentModificationError,), id="door-commits"),
    ],
)
def test_writers_waiting_on_a_held_commit_turn_leave_the_catalog_answering_other_tables(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str, may_fail_with: tuple[type[Exception], ...]
) -> None:
    """[[LH-259]] A writer that finds its ref's commit turn held waits it out, up to 30 s, and the catalog serves every sync
    door from one pool of request threads (anyio's default limiter: 40): measured, 50 writers waiting in threads made a
    describe of another table wait 26.8 s. ``/insert`` and ``version/create`` wait in the event loop, holding no thread, and
    a door's commit waits in one of a bounded number of threads, past which it is refused as the retryable conflict a lost
    race is. Either way no write lands while the turn is held, and the catalog keeps answering. The waiters share one read
    of the turn record a tenth of a second at a time: read per waiter, 53 writers waiting on one held turn pegged the
    estate's catalog at its 1-CPU limit (measured 2026-10-31)."""
    stock = served.stock()
    stock.create_namespace(lance_namespace.CreateNamespaceRequest(id=["db"]))
    seeded = pa.table({"id": pa.array(range(51), pa.int64()), "v": pa.array(range(51), pa.int64())})
    for table_id in (_APPENDED_TO, ["db", "other"]):
        lance.write_dataset(seeded, namespace_client=stock, table_id=table_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    write = {
        "insert": lambda row: served.stock().insert_into_table(lance_namespace.InsertIntoTableRequest(id=_APPENDED_TO, mode="append"), _ipc(_row(100 + row))),
        "version-create": lambda row: lance.write_dataset(_row(100 + row), namespace_client=served.stock(), table_id=_APPENDED_TO, mode="append"),
        "delete": lambda row: served.stock().delete_from_table(lance_namespace.DeleteFromTableRequest(id=_APPENDED_TO, predicate=f"id = {row}")),
    }[door]
    finished: list[tuple[float, BaseException | None]] = []

    def run(row: int) -> None:
        try:
            write(row)
        except Exception as exc:  # noqa: BLE001 — every writer's outcome is half of the claim
            finished.append((time.monotonic(), exc))
        else:
            finished.append((time.monotonic(), None))

    reads: list[float] = []
    read_json_at = records.read_json_at

    def counted(root_uri: str, storage_options: StorageOptions, key: str) -> records.StoreRead:
        if key.startswith(commit_turns.PREFIX):
            reads.append(time.monotonic())
        return read_json_at(root_uri, storage_options, key)

    monkeypatch.setattr(records, "read_json_at", counted)
    turns = _turns(stock, tmp_path)
    token = turns.take(None)
    held_from = time.monotonic()
    writers = [threading.Thread(target=run, args=(row,), daemon=True) for row in range(1, 51)]
    try:
        for writer in writers:
            writer.start()
        slowest, until = 0.0, time.monotonic() + 4
        while time.monotonic() < until:
            began = time.monotonic()
            stock.describe_table(lance_namespace.DescribeTableRequest(id=["db", "other"]))
            slowest = max(slowest, time.monotonic() - began)
            time.sleep(0.2)
    finally:
        released = time.monotonic()
        turns.release(token)
    for writer in writers:
        writer.join(timeout=120)

    held_reads = [at for at in reads if held_from <= at < released]
    assert slowest < 2, f"a describe of another table took {slowest:.1f} s while 50 writers waited on a held turn"
    assert len(held_reads) <= 20 * (released - held_from), f"50 waiters read the turn record {len(held_reads)} times in {released - held_from:.1f} s"
    assert [at for at, exc in finished if exc is None and at < released] == []
    assert [exc for _, exc in finished if exc is not None and not isinstance(exc, may_fail_with)] == []


#: The provenance a pipeline names on a governed write (ADR 0156 C2): its run, its job, and each input at a version.
_RUN = {
    "x-rask-run-id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
    "x-rask-job-namespace": "pipelines",
    "x-rask-job": "nightly",
    "x-rask-inputs": json.dumps([{"table": ["db", "src"], "version": 1}]),
}


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


@pytest.mark.parametrize(("door", "operation"), [pytest.param(_insert, "insert", id="insert"), pytest.param(_merge_insert, "merge_insert", id="merge_insert")])
def test_a_catalog_write_door_commits_the_run_its_context_names(
    served: Served, door: Callable[[lance_namespace.LanceNamespace], None], operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog.main import app

    _with_source(served)
    recorder = _Recorder()
    monkeypatch.setattr(app.state, "lineage_emitter", recorder, raising=False)

    door(served.stock(_RUN))

    landed = _opened(served.stock())
    assert landed.version == 2
    properties = _committed_properties(landed, 2)
    assert {key: value for key, value in properties.items() if key != "rask.inputs"} == {
        "rask.run_id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
        "rask.job_namespace": "pipelines",
        "rask.job": "nightly",
        "rask.author": "dex~alice",
        "rask.operation": operation,
    }
    assert json.loads(properties["rask.inputs"]) == [{"table": ["db", "src"], "version": 1}]
    (announced,) = recorder.writes
    assert (announced["version"], [ref[:3] for ref in announced["inputs"]], str(announced["run"].run_id), announced["run"].job) == (
        2,
        [("db$src", 1, None)],
        "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
        "nightly",
    )


_SILVER = ["lab", "t"]
_PROVENANCE = {
    "rask.run_id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
    "rask.job_namespace": "pipelines",
    "rask.job": "nightly",
    "rask.author": "dex~alice",
    "rask.inputs": json.dumps([{"table": ["db", "src"], "version": 1}]),
}


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
    context = {
        "x-rask-run-id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
        "x-rask-job-namespace": "pipelines",
        "x-rask-job": "nightly",
        "x-rask-inputs": _PROVENANCE["rask.inputs"],
    }
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
    assert [(write["version"], [ref[:3] for ref in write["inputs"]]) for write in recorder.writes] == [(2, [("db$src", 1, None)])]
    # A restore brings back rows the table held, here a fragment the overwrite dropped, so it names no run.
    landed.checkout_version(1).restore()
    assert _rows(lance.dataset(namespace_client=stock, table_id=_SILVER)) == [(1, "a")]


def _stock_append_naming(served: Served, inputs: str) -> tuple[int, str]:
    provenance = {
        "rask.run_id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
        "rask.job_namespace": "pipelines",
        "rask.job": "nightly",
        "rask.author": "dex~alice",
        "rask.inputs": inputs,
    }
    with pytest.raises(OSError):
        lance.write_dataset(_keyed([4], ["d"]), namespace_client=served.stock(), table_id=_TABLE, mode="append", transaction_properties=provenance)
    _, code, detail = _answer_to_the_staged_commit(served, "db$t", 2)
    return code, detail


def _insert_door_naming(served: Served, inputs: str) -> tuple[int, str]:
    with pytest.raises(lance_namespace.InvalidInputError) as refused:
        _insert(
            served.stock(
                {"x-rask-run-id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07", "x-rask-job-namespace": "pipelines", "x-rask-job": "nightly", "x-rask-inputs": inputs}
            )
        )
    return refused.value.code, str(refused.value)


def _a_name_holding_no_table(served: Served, stock: lance_namespace.LanceNamespace) -> list[str]:
    return ["db", "nowhere"]


def _a_table_the_caller_cannot_read(served: Served, stock: lance_namespace.LanceNamespace) -> list[str]:
    # The input's grants are held by the uuid the catalog minted for it, which its describe answers (ADR 0173 §8).
    source_uuid = (stock.describe_table(lance_namespace.DescribeTableRequest(id=["db", "src"])).properties or {})["rask.uuid"]
    served.denied.add(f"can_read_data table:{source_uuid}")
    return ["db", "src"]


@pytest.mark.parametrize("commit", [pytest.param(_stock_append_naming, id="a-stock-commit"), pytest.param(_insert_door_naming, id="the-insert-door")])
@pytest.mark.parametrize(
    "named", [pytest.param(_a_name_holding_no_table, id="a-name-holding-no-table"), pytest.param(_a_table_the_caller_cannot_read, id="an-unreadable-table")]
)
def test_a_commit_declaring_an_input_that_is_no_table_the_caller_may_read_is_refused(
    served: Served,
    commit: Callable[[Served, str], tuple[int, str]],
    named: Callable[[Served, lance_namespace.LanceNamespace], list[str]],
) -> None:
    """ADR 0183 §2: one refusal for both, so it tells no committer which tables exist."""
    stock = _with_source(served)
    table = named(served, stock)

    code, detail = commit(served, json.dumps([{"table": table, "version": 1}]))

    refusal = f"the input {'$'.join(table)!r} its run declares (rask.inputs) names no table the committer may read (can_read_data)"
    assert (code, refusal in detail) == (13, True), detail
    assert _opened(stock).version == 1
