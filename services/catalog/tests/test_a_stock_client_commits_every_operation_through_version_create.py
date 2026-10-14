"""[[LH-471]] A stock Lance client commits every operation through the catalog's ``CreateTableVersion`` (ADR 0157).

The catalog answers ``managed_versioning=true`` on ``DescribeTable``, so pylance stages each manifest under
``_versions/<n>.manifest-<uuid>`` and asks ``POST /v1/table/{id}/version/create`` to finalize it
(``lance_docs/ns_catalog/spec.yaml`` § CreateTableVersion). The client here is pylance's own, over
``lance_namespace.connect("rest", ...)`` against the real catalog served by uvicorn; nothing in its construction
is rask's. The catalog's ``commit_version`` audit record names the operation it read from the staged transaction:
that record is how a commit that reached the catalog differs from one pylance finalized on its own.

A refused commit is not bypassed: the table stays at its version and the client sees the spec error.

The store is the local filesystem: the claim is the commit path, not the credential (LH-229 pins that). OIDC is on
with a stub verifier, and FGA's answers are faked by relation (the offline suite has no OpenFGA): every relation is
granted except the ones a case denies, so the door's own rung choice is what decides.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from pydantic import BaseModel

from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken


_TOKEN = {"Authorization": "Bearer t"}
_TABLE = ["db", "t"]


class Served(BaseModel):
    url: str
    denied: set[str]
    root: Path

    def stock(self) -> lance_namespace.LanceNamespace:
        return lance_namespace.connect("rest", {"uri": self.url, "headers.Authorization": _TOKEN["Authorization"]})


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """The real catalog on a local root under uvicorn, alice signed in, FGA granting all but ``denied``."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path / "root"),
        "LANCE_CONTROL_ROOT": str(tmp_path / "control"),
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_S3_ACCESS_KEY_ID": "unused",
        "LANCE_S3_SECRET_ACCESS_KEY": "unused",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    served = Served(url="", denied=set(), root=tmp_path / "root")

    async def check(_client: object, *, relation: str, **_kw: object) -> bool:
        return relation not in served.denied

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
        _keyed([1, 2, 3], ["a", "b", "c"]), namespace_client=stock, table_id=_TABLE, mode="create", data_storage_version="2.2", enable_stable_row_ids=True
    )
    return stock


def _keyed(ids: list[int], values: list[str]) -> pa.Table:
    """Rows of a table whose ``id`` is its declared (unenforced) primary key."""
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata={"lance-schema:unenforced-primary-key": "true"}), pa.field("v", pa.string())])
    return pa.table({"id": ids, "v": values}, schema=schema)


def _opened(stock: lance_namespace.LanceNamespace) -> lance.LanceDataset:
    return lance.dataset(namespace_client=stock, table_id=_TABLE)


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

    with pytest.raises(OSError):
        stage(stock)

    after = _opened(stock)
    assert after.version == 3
    assert _rows(after) == [(1, "a"), (3, "c"), (4, "d")]
    # pylance reports a refused finalize as an unknown outcome and keeps its staged manifest; asking for it again
    # shows the answer the client was given.
    table, version = staged_at
    (staged,) = served.root.glob(f"*_{table}/_versions/*.manifest-*")
    path = staged.relative_to("/").as_posix()
    again = httpx.post(f"{served.url}/v1/table/{table}/version/create", json={"version": version, "manifest_path": path}, headers=_TOKEN)
    assert (again.status_code, again.json().get("code")) == (status, code), again.text
    assert _opened(stock).version == 3


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
