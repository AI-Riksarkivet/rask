"""[[LH-202]], [[LH-229]] A vended credential reaches only the files its rung may change, and a writer still lands rows.

THE LAYOUT THE FORMAT FIXES DECIDES THE GRANT. `lance_docs/lance/format/table/layout.md` places a table's files in
named directories: `data/` (data files, and blob sidecars under `data/<stem>/`), `_versions/` (the
manifests, so every commit), `_transactions/`, `_deletions/`, `_indices/`, `_refs/` (branch and tag
pointers), `tree/<branch>/` (each branch's own directories) and `_mem_wal/`. Its `_blobs/` and `_bt/` are written only
under feature flags `version/create` refuses a commit turning on, so no tier grants a put there.

A `can_write_data` holder is a managed-versioning writer (ADR 0157): it puts data files, transaction files,
deletion files, index files and a STAGED manifest `_versions/<n>.manifest-<uuid>`, and the catalog finalizes
the version under its own credential. So the writer tier never reaches a final `_versions/<n>.manifest`, the
version hint or `_refs/`, and that refusal is an explicit Deny: an Allow on `_versions/*.manifest-*` alone
also matches `<n>.manifest-<uuid>.manifest`, which pylance reads as a final manifest. Measured with stock
pylance 13.0.0 on a throwaway store and on the deployed one (LH-469, #594); RustFS lets an explicit Deny win
(R3, measured 2026-10-18, git history at 64ef118a9). Whole-prefix write and delete belong to `can_maintain`, all but
`_refs/`: a tag exempts its version from cleanup and a branch keeps its files
(`lance_docs/lance/guide/tags_and_branches.md`), so a maintainer that could write a ref could keep a version an erasure
removes or free one a hold keeps ([[XC-084]]). Its reclaim's own records on the control root, the table's history fence and
the records of its committed files, are the maintain vend's too, and only its own table's ([[LH-219]]).

DRIVEN THROUGH THE REAL DOOR AND THE REAL VENDOR. The catalog runs on a moto server, the vendor is the one its
lifespan builds, and its token exchange goes to moto's STS, which records the session policy each session carries.
moto's S3 does not enforce session policies, so the policy is judged with moto's IAM evaluator (`IAMPolicy`), which
applies IAM's wildcard semantics and an explicit Deny's precedence. The store's own 403s are the deployed probe's job.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from lance_namespace import ErrorCode
from moto.iam.access_control import IAMPolicy, PermissionResult
from moto.sts.models import sts_backends

from lakehouse import file_identities, history_fences, location_claims, trash
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.record_store import record_key


_SCHEMA = pa.schema([("id", pa.int64()), lance.blob_field("payload")])
_TABLE = "db$pages"


def _rows(ids: list[int], blob_bytes: int = 16) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "payload": lance.blob_array([bytes(blob_bytes) for _ in ids])}, schema=_SCHEMA)


class _Estate:
    """The catalog on moto, one table and a branch of it created through the door, and every session policy vended."""

    def __init__(self, client: httpx.Client, s3: Any, bucket: str) -> None:  # noqa: ANN401 — a boto3 S3 client
        self.client, self.s3, self.bucket = client, s3, bucket
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200
        self.location = self.create(_TABLE)
        created = client.post(f"/v1/table/{_TABLE}/branches/create", json={"name": "b1"})
        assert created.status_code == 200, created.text

    def create(self, table: str) -> str:
        created = self.client.post(f"/v1/table/{table}/create", content=encode_arrow_stream(_rows([0])), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert created.status_code == 200, created.text
        return str(created.json()["location"])

    def vend(self, tier: str, branch: str = "") -> tuple[dict[str, str], dict[str, Any]]:
        """The storage options the door vends, and the session policy the vendor sent to STS for them, as moto recorded it."""
        params = {"tier": tier, **({"branch": branch} if branch else {})}
        vended = self.client.post(f"/management/v1/table/{_TABLE}/credentials", params=params)
        assert vended.status_code == 200, vended.text
        assert vended.json()["mode"] == "direct", vended.text
        options = vended.json()["credentials"]["storage_options"]
        session = next(role for role in sts_backends["123456789012"]["aws"].assumed_roles if role.access_key_id == options["aws_access_key_id"])
        return options, json.loads(session.policy)

    def keys(self) -> set[str]:
        return {o["Key"] for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket) for o in page.get("Contents", [])}

    def table_uuid(self) -> str:
        described = self.client.post(f"/v1/table/{_TABLE}/describe", json={})
        assert described.status_code == 200, described.text
        return str(described.json()["properties"]["rask.uuid"])


@pytest.fixture
def estate(moto_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket = f"lh202-{uuid.uuid4().hex[:10]}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
    token = tmp_path / "projected-rask-object-store-token"
    token.write_text("projected-for-rask-object-store")
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_WEB_IDENTITY_TOKEN_FILE": str(token),
        "LANCE_S3_STS_ENDPOINT": moto_url,
        "LANCE_S3_ASSUME_ROLE_ARN": "arn:aws:iam::123456789012:role/lance-vend",
        "LANCE_VENDING_MODE": "web_identity",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LANCE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("LANCE_S3_SECRET_ACCESS_KEY", raising=False)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    with httpx.Client(base_url=f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", timeout=120) as client:
        yield _Estate(client, s3, bucket)
    server.should_exit = True
    thread.join(timeout=30)
    get_settings.cache_clear()


def _permits(policy: dict[str, Any], action: str, bucket: str, key: str) -> bool:
    """IAM's verdict on ``action`` over ``bucket/key``, by moto's evaluator.

    moto turns a Resource into a regex without escaping it, so the ``$`` a table directory carries would
    read as an end anchor; it is swapped for a character IAM gives no meaning, on both sides alike.
    """
    document = json.dumps(policy).replace("$", "~")
    return IAMPolicy(document).is_action_permitted(action, f"arn:aws:s3:::{bucket}/{key.replace('$', '~')}") == PermissionResult.PERMITTED


def _within(location: str, bucket: str) -> str:
    return location.removeprefix(f"s3://{bucket}/").rstrip("/")


_Tier = Literal["read", "write", "maintain"]


@pytest.mark.parametrize(
    ("tier", "branch", "action", "relative", "allowed"),
    [
        pytest.param("read", "", "s3:GetObject", "_versions/1.manifest", True, id="read-reads"),
        pytest.param("read", "", "s3:PutObject", "data/f.lance", False, id="read-cannot-write"),
        pytest.param("write", "", "s3:GetObject", "_versions/1.manifest", True, id="write-reads-the-table"),
        pytest.param("write", "", "s3:PutObject", "data/f.lance", True, id="write-data-file"),
        pytest.param("write", "", "s3:PutObject", "data/f/1.blob", True, id="write-blob-sidecar"),
        pytest.param("write", "", "s3:AbortMultipartUpload", "data/f.lance", True, id="write-aborts-its-upload"),
        pytest.param("write", "", "s3:DeleteObject", "data/f.lance", False, id="write-cannot-delete-data"),
        pytest.param("write", "", "s3:PutObject", "_versions/9.manifest", False, id="write-cannot-commit"),
        pytest.param("write", "", "s3:DeleteObject", "_versions/1.manifest", False, id="write-cannot-delete-a-version"),
        pytest.param("write", "", "s3:PutObject", "_versions/9.manifest-0b1c", True, id="write-stages-a-manifest"),
        pytest.param("write", "", "s3:PutObject", "_versions/18446744073709551606.manifest", False, id="write-cannot-commit-v2-naming"),
        pytest.param("write", "", "s3:PutObject", "_versions/9.manifest-0b1c.manifest", False, id="write-cannot-plant-a-final-name"),
        pytest.param("write", "", "s3:PutObject", "_versions/latest_version_hint.json", False, id="write-cannot-move-the-hint"),
        pytest.param("write", "", "s3:PutObject", "_transactions/9-x.txn", True, id="write-stages-a-transaction"),
        pytest.param("write", "", "s3:PutObject", "_deletions/0-1-1.arrow", True, id="write-stages-a-deletion"),
        pytest.param("write", "", "s3:PutObject", "_indices/u/index.idx", True, id="write-stages-an-index"),
        pytest.param("write", "", "s3:PutObject", "_refs/tags/t.json", False, id="write-cannot-move-a-tag"),
        pytest.param("write", "", "s3:PutObject", "_refs/branches/b2.json", False, id="write-cannot-forge-a-branch"),
        pytest.param("write", "", "s3:PutObject", "tree/b1/data/f.lance", True, id="write-stages-on-a-branch"),
        pytest.param("write", "", "s3:PutObject", "tree/b1/_versions/9.manifest", False, id="write-cannot-commit-on-a-branch"),
        pytest.param("write", "", "s3:PutObject", "_mem_wal/0/x", False, id="write-cannot-write-the-mem-wal"),
        pytest.param("write", "b1", "s3:PutObject", "tree/b1/data/f.lance", True, id="branch-write-data-file"),
        pytest.param("write", "b1", "s3:PutObject", "tree/b1/_versions/9.manifest-0b1c", True, id="branch-write-stages-a-manifest"),
        pytest.param("write", "b1", "s3:PutObject", "tree/b1/_versions/9.manifest", False, id="branch-write-cannot-commit"),
        pytest.param("write", "b1", "s3:PutObject", "tree/b1/_transactions/9-x.txn", True, id="branch-write-stages-a-transaction"),
        pytest.param("write", "b1", "s3:PutObject", "_versions/9.manifest-0b1c", False, id="branch-write-cannot-stage-on-main"),
        pytest.param("write", "b1", "s3:PutObject", "data/f.lance", False, id="branch-write-cannot-write-main"),
        pytest.param("maintain", "", "s3:PutObject", "_versions/9.manifest", True, id="maintain-commits"),
        pytest.param("maintain", "", "s3:PutObject", "data/f.lance", True, id="maintain-rewrites"),
        pytest.param("maintain", "", "s3:DeleteObject", "_versions/1.manifest", True, id="maintain-cleans-up-versions"),
        pytest.param("maintain", "", "s3:DeleteObject", "tree/b1/data/f.lance", True, id="maintain-cleans-up-branches"),
        pytest.param("maintain", "", "s3:PutObject", "_refs/branches/b2.json", False, id="maintain-cannot-pin-a-version"),
        pytest.param("maintain", "", "s3:DeleteObject", "_refs/tags/t.json", False, id="maintain-cannot-unpin-a-version"),
    ],
)
def test_a_vend_permits_only_what_its_tier_may_change(estate: _Estate, tier: _Tier, branch: str, action: str, relative: str, allowed: bool) -> None:
    _, policy = estate.vend(tier, branch)

    assert _permits(policy, action, estate.bucket, f"{_within(estate.location, estate.bucket)}/{relative}") is allowed
    # The prefix SIBLING (`<table>_secret/`): an IAM `*` matches across `/`, so a resource that dropped the
    # table's trailing delimiter would reach every key that merely starts with the table's directory name.
    sibling = f"{_within(estate.location, estate.bucket)}_secret/{relative}"
    assert not _permits(policy, action, estate.bucket, sibling), "a prefix sibling of the table is never reached"


def test_a_maintain_vend_writes_only_its_own_tables_records_on_the_control_root(estate: _Estate) -> None:
    """The reclaim holds its table's history fence and forgets the records of its committed files under the vend
    (`lakehouse.history_fences`, `lakehouse.file_identities`), so the vend reaches those two of its own table; it puts no
    committed-file record, since one it could put would record a replaced file as committed, and reaches no other table's.
    The control root here is the table's bucket."""
    mine, other = estate.table_uuid(), uuid.uuid4().hex

    _, policy = estate.vend("maintain")

    asked = {
        "holds-its-fence": ("s3:PutObject", history_fences.fence_key(mine)),
        "forgets-its-records": ("s3:DeleteObject", f"{file_identities.records_of(mine)}/txn.json"),
        "records-no-file": ("s3:PutObject", f"{file_identities.records_of(mine)}/txn.json"),
        "holds-no-other-fence": ("s3:PutObject", history_fences.fence_key(other)),
        "forgets-no-other-records": ("s3:DeleteObject", f"{file_identities.records_of(other)}/txn.json"),
    }
    granted = {name: _permits(policy, action, estate.bucket, key) for name, (action, key) in asked.items()}
    assert granted == {
        "holds-its-fence": True,
        "forgets-its-records": True,
        "records-no-file": False,
        "holds-no-other-fence": False,
        "forgets-no-other-records": False,
    }, policy


@pytest.mark.parametrize(
    ("tier", "answer"),
    [
        pytest.param("maintain", (409, "application/problem+json", ErrorCode.INVALID_TABLE_STATE), id="maintain-refused"),
        pytest.param("write", (200, "application/json", None), id="write-still-vended"),
    ],
)
def test_a_held_table_is_vended_no_maintain_tier(estate: _Estate, tier: _Tier, answer: tuple[int, str, int | None]) -> None:
    """[[XC-084]] A table whose maintenance policy keeps its whole history is vended no credential that could delete or
    rewrite it: the maintain tier reaches every version the hold keeps, and maintenance writes nothing on a held table.
    The other tiers are the table's readers' and writers', which the hold does not change."""
    held = estate.client.post(f"/management/v1/table/{_TABLE}/policy/set", json={"cleanup_enabled": False})
    assert held.status_code == 200, held.text

    vended = estate.client.post(f"/management/v1/table/{_TABLE}/credentials", params={"tier": tier})

    assert (vended.status_code, vended.headers["content-type"], vended.json().get("code")) == answer, vended.text


def _dropped(estate: _Estate, monkeypatch: pytest.MonkeyPatch, *, grace_days: str, name: str = "gone") -> tuple[str, str]:
    """A table dropped into the trash for ``grace_days``, by its uuid and location."""
    from catalog.core.config import get_settings

    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", grace_days)
    get_settings.cache_clear()
    location = estate.create(f"db${name}")
    described = estate.client.post(f"/v1/table/db${name}/describe", json={})
    assert described.status_code == 200, described.text
    dropped = estate.client.post(f"/v1/table/db${name}/drop")
    assert dropped.status_code == 200, dropped.text
    return str(described.json()["properties"]["rask.uuid"]), location


def _record(estate: _Estate, uuid: str) -> dict[str, Any]:
    return json.loads(estate.s3.get_object(Bucket=estate.bucket, Key=trash.key(uuid))["Body"].read())


def _due(estate: _Estate, monkeypatch: pytest.MonkeyPatch, *, name: str = "gone") -> tuple[str, str]:
    """A table dropped into the trash and past its deadline, by its uuid and location."""
    uuid, location = _dropped(estate, monkeypatch, grace_days="0.00001", name=name)
    deadline = time.monotonic() + 10
    while not trash.past_deadline(_record(estate, uuid)):
        assert time.monotonic() < deadline, "the drop's deadline never passed"
        time.sleep(0.05)
    return uuid, location


def _filed_at(estate: _Estate, uuid: str, location: str) -> None:
    """Rewrite the record ``uuid`` keeps to name ``location``, as any writer of the record could."""
    estate.s3.put_object(Bucket=estate.bucket, Key=trash.key(uuid), Body=json.dumps({**_record(estate, uuid), "location": location}).encode())


def _short_of_its_deadline(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> str:
    return _dropped(estate, monkeypatch, grace_days="1")[0]


def _at_a_place_another_table_claims(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> str:
    uuid, _ = _due(estate, monkeypatch)
    _, kept = _dropped(estate, monkeypatch, grace_days="1", name="kept")
    _filed_at(estate, uuid, kept)
    return uuid


def _at_a_place_no_table_claims(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> str:
    uuid, _ = _due(estate, monkeypatch)
    _filed_at(estate, uuid, f"s3://{estate.bucket}/db-unclaimed")
    return uuid


@pytest.mark.parametrize(
    ("door", "arrange"),
    [
        pytest.param("POST /credentials", _short_of_its_deadline, id="session-short-of-its-deadline"),
        pytest.param("POST /credentials", _at_a_place_another_table_claims, id="session-at-another-tables-place"),
        pytest.param("POST /credentials", _at_a_place_no_table_claims, id="session-at-an-unclaimed-place"),
        pytest.param("POST /release", _short_of_its_deadline, id="release-short-of-its-deadline"),
        pytest.param("DELETE ", _short_of_its_deadline, id="end-with-its-bytes"),
    ],
)
def test_a_trash_door_refuses_what_is_not_the_trashs_to_end(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: str, arrange: Callable[[_Estate, pytest.MonkeyPatch], str]
) -> None:
    """[[XC-084]] Maintenance ends the trash only through the catalog's doors, which judge each record on what no session
    can write: a drop still recoverable is released and vended nothing, a record's place must be the one its claim keeps
    for the record's own uuid (so no record names its way to another table's bytes, trashed or held), and a record is
    ended only once its place holds nothing. Each refusal is noted on the record, as maintenance's own are."""
    uuid = arrange(estate, monkeypatch)
    method, _, act = door.partition(" ")

    refused = estate.client.request(method, f"/management/v1/trash/table/{uuid}{act}")

    assert (refused.status_code, refused.headers["content-type"], refused.json()["code"], _record(estate, uuid)["attempts"]) == (
        409,
        "application/problem+json",
        ErrorCode.INVALID_TABLE_STATE,
        1,
    ), refused.text


def test_a_reclaim_session_deletes_its_tables_bytes_and_nothing_else(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """[[XC-084]] A record past its deadline is vended a session that deletes its table's bytes, and reaches nothing on
    the control root (neither its own record, which the catalog alone writes and judges on, nor a record kept under its
    uuid, such as the inputs an erasure walks to the tables derived from it), no put over its own table's bytes and no
    other table's: maintenance holds no other write."""
    uuid, location = _due(estate, monkeypatch)

    vended = estate.client.post(f"/management/v1/trash/table/{uuid}/credentials")

    assert vended.status_code == 200, vended.text
    options = vended.json()["credentials"]["storage_options"]
    policy = json.loads(next(role.policy for role in sts_backends["123456789012"]["aws"].assumed_roles if role.access_key_id == options["aws_access_key_id"]))
    asked = {
        "deletes-its-bytes": ("s3:DeleteObject", f"{_within(location, estate.bucket)}/data/f.lance"),
        "puts-none-of-its-bytes": ("s3:PutObject", f"{_within(location, estate.bucket)}/data/f.lance"),
        "writes-no-trash-record": ("s3:PutObject", trash.key(uuid)),
        "ends-no-erasure-input": ("s3:DeleteObject", record_key("_table_inputs", "table", uuid)),
        "deletes-no-other-table": ("s3:DeleteObject", f"{_within(estate.location, estate.bucket)}/data/f.lance"),
    }
    granted = {name: _permits(policy, action, estate.bucket, key) for name, (action, key) in asked.items()}
    assert granted == {
        "deletes-its-bytes": True,
        "puts-none-of-its-bytes": False,
        "writes-no-trash-record": False,
        "ends-no-erasure-input": False,
        "deletes-no-other-table": False,
    }, policy


def test_the_catalog_ends_a_trash_record_whose_place_holds_nothing(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """[[XC-084]] Once nothing the record names holds a byte, the catalog ends it under its own identity: the record goes,
    and the claim it kept on its place is released, so the place is free again."""
    uuid, location = _dropped(estate, monkeypatch, grace_days="1")
    for key in [o["Key"] for o in estate.s3.list_objects_v2(Bucket=estate.bucket, Prefix=f"{_within(location, estate.bucket)}/").get("Contents", [])]:
        estate.s3.delete_object(Bucket=estate.bucket, Key=key)

    ended = estate.client.delete(f"/management/v1/trash/table/{uuid}")

    kept = estate.s3.list_objects_v2(Bucket=estate.bucket, Prefix=trash.key(uuid)).get("KeyCount", 0)
    claim = json.loads(estate.s3.get_object(Bucket=estate.bucket, Key=location_claims.claim_key(location))["Body"].read())
    assert (ended.status_code, kept, claim["uuid"], claim["released_at"] is not None) == (200, 0, uuid, True), ended.text


#: A final manifest, in either naming scheme: what only the catalog's finalize may write.
_FINAL_MANIFEST = re.compile(r"/_versions/\d+\.manifest$")


def test_a_writer_lands_rows_with_only_what_its_vend_allows(estate: _Estate) -> None:
    """A stock append on the vended write tier stages everything in the table itself and leaves only the final manifest to
    the catalog, whose own records of the commit lie on its control root."""
    options, policy = estate.vend("write")
    stock = lance_namespace.connect("rest", {"uri": str(estate.client.base_url)})
    before = estate.keys()

    lance.write_dataset(_rows([1, 2], blob_bytes=200_000), namespace_client=stock, table_id=["db", "pages"], mode="append", storage_options=options)

    new = sorted(key for key in estate.keys() - before if key.startswith(f"{_within(estate.location, estate.bucket)}/"))
    finalized = [key for key in new if _FINAL_MANIFEST.search(key)]
    staged_by_the_writer = [key for key in new if key not in finalized]
    assert finalized and not any(_permits(policy, "s3:PutObject", estate.bucket, key) for key in finalized), finalized
    assert staged_by_the_writer and all(_permits(policy, "s3:PutObject", estate.bucket, key) for key in staged_by_the_writer), staged_by_the_writer
    landed = lance.dataset(estate.location, storage_options=options)
    assert sorted(landed.to_table(columns=["id"]).column("id").to_pylist()) == [0, 1, 2]
