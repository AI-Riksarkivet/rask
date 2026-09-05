"""Conditional-create on the control-root registries.

The registry's id-minting writes (`_projects/<id>.json`, `_warehouses/<id>.json`, bindings) were
plain overwrites, so every guard in front of them was check-then-act: two concurrent creates of one
warehouse id under different projects both read "absent" and the last writer won — on exactly the
tenant-isolation guards (docs/DECISIONS.md "The Python estate audit" CAT-CORE-05, same defect class). The store
demonstrably honors put-if-not-exists (`tests/e2e-py/test_object_store_cas_e2e.py` proves
``If-None-Match: *`` against RustFS), so the fix is to USE it: ``service_kit.lakehouse.records``
is the one conditional-create seam, and the id-minting doors go through it.

Local-FS branch is exercised directly (``open(..., "xb")`` exclusivity — the same arbitration, by
the OS); the S3 branch is driven through a fake client at the ``storage.s3_client`` seam, pinning the ``IfNoneMatch`` parameter
and the 412 → ``RecordExistsError`` mapping, mirroring how ``test_vending.py`` pins the STS call.
The live contended-writer proof against RustFS lives in ``tests/e2e-py/test_registry_cas_e2e.py``
(env-gated, ``cas`` marker) — this file is the always-running half.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from lance_namespace import NamespaceAlreadyExistsError

import storage
from catalog.services import warehouses
from service_kit.lakehouse import records


def _root(tmp_path: Any) -> str:
    return f"file://{tmp_path}"


# --------------------------------------------------------------------------- #
# the seam: create_json (local-FS branch — OS-arbitrated exclusivity)
# --------------------------------------------------------------------------- #


def test_create_json_local_first_writer_wins(tmp_path: Any) -> None:
    records.create_json(_root(tmp_path), {}, "_warehouses/wh-a.json", {"id": "wh-a", "project": "acme"})
    assert json.loads((tmp_path / "_warehouses" / "wh-a.json").read_text()) == {"id": "wh-a", "project": "acme"}


def test_create_json_local_second_create_refused_and_content_preserved(tmp_path: Any) -> None:
    root = _root(tmp_path)
    records.create_json(root, {}, "_warehouses/wh-a.json", {"id": "wh-a", "project": "acme"})
    with pytest.raises(records.RecordExistsError):
        records.create_json(root, {}, "_warehouses/wh-a.json", {"id": "wh-a", "project": "evil"})
    # the loser's payload never touched the store — first writer's record is intact
    assert json.loads((tmp_path / "_warehouses" / "wh-a.json").read_text())["project"] == "acme"


def test_create_json_local_bare_path_root(tmp_path: Any) -> None:
    # control_root may be a bare local path (no file:// scheme) in dev — same exclusivity.
    records.create_json(str(tmp_path), {}, "_projects/acme.json", {"id": "acme"})
    with pytest.raises(records.RecordExistsError):
        records.create_json(str(tmp_path), {}, "_projects/acme.json", {"id": "acme"})


# --------------------------------------------------------------------------- #
# the seam: create_json (S3 branch — store-arbitrated If-None-Match)
# --------------------------------------------------------------------------- #

_SO = {"endpoint": "http://rf:9000", "access_key_id": "k", "secret_access_key": "s", "region": "us-east-1"}


def _client_error(code: str, status: int) -> ClientError:
    """A ClientError shaped like botocore's own — every `ResponseMetadata` key, not just the one read.

    `_ResponseMetadataTypeDef` is a total TypedDict: botocore always populates all five. Supplying
    only `HTTPStatusCode` built a fake no real call can produce, and `create_json` reads the block
    through `.get()` chains that swallow the difference — so a consumer that grew a `RetryAttempts`
    read would pass here and `KeyError` in production against the very error this fixture models.
    """
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {
                "RequestId": "req-test",
                "HostId": "host-test",
                "HTTPStatusCode": status,
                "HTTPHeaders": {},
                "RetryAttempts": 0,
            },
        },
        "PutObject",
    )


def test_create_json_s3_sends_if_none_match(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(storage, "s3_client", lambda *_a, **_kw: client)
    records.create_json("s3://ctrl/prefix", _SO, "_warehouses/wh-a.json", {"id": "wh-a"})
    kwargs = client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "ctrl"
    assert kwargs["Key"] == "prefix/_warehouses/wh-a.json"
    assert kwargs["IfNoneMatch"] == "*"  # the whole point: the STORE arbitrates, not a prior read
    assert json.loads(kwargs["Body"]) == {"id": "wh-a"}


def test_create_json_s3_root_without_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(storage, "s3_client", lambda *_a, **_kw: client)
    records.create_json("s3://ctrl", _SO, "_projects/acme.json", {"id": "acme"})
    kwargs = client.put_object.call_args.kwargs
    assert (kwargs["Bucket"], kwargs["Key"]) == ("ctrl", "_projects/acme.json")


def test_create_json_s3_412_maps_to_record_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    client.put_object.side_effect = _client_error("PreconditionFailed", 412)
    monkeypatch.setattr(storage, "s3_client", lambda *_a, **_kw: client)
    with pytest.raises(records.RecordExistsError):
        records.create_json("s3://ctrl", _SO, "_warehouses/wh-a.json", {"id": "wh-a"})


def test_create_json_s3_other_client_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # A refused conditional PUT is the one expected signal; anything else (auth, throttling, outage)
    # must surface as itself — laundering it into "already exists" would hide a real failure.
    client = MagicMock()
    client.put_object.side_effect = _client_error("AccessDenied", 403)
    monkeypatch.setattr(storage, "s3_client", lambda *_a, **_kw: client)
    with pytest.raises(ClientError):
        records.create_json("s3://ctrl", _SO, "_warehouses/wh-a.json", {"id": "wh-a"})


# --------------------------------------------------------------------------- #
# the doors: warehouse-id mint + binding write-once through the seam
# --------------------------------------------------------------------------- #


def test_create_warehouse_record_second_mint_refused(tmp_path: Any) -> None:
    root, so = _root(tmp_path), {}
    warehouses.create_warehouse_record(root, so, {"id": "wh-a", "bucket": "b", "root_uri": "s3://b", "project": "acme", "created_at": "t"})
    with pytest.raises(records.RecordExistsError):
        warehouses.create_warehouse_record(root, so, {"id": "wh-a", "bucket": "b2", "root_uri": "s3://b2", "project": "evil", "created_at": "t"})
    winner = warehouses.get_warehouse(root, so, "wh-a")
    assert winner is not None, "the refused second mint must leave the FIRST record standing, not delete it"
    assert winner["project"] == "acme"


def test_bind_namespace_is_write_once(tmp_path: Any) -> None:
    # The binding write-once guard used to be check-then-act (endpoint reads, then service
    # overwrites): a concurrent bind of one top_ns to two warehouses re-routed tenant A's tables to
    # tenant B's bucket. The service itself now refuses at the store, whatever the caller read.
    root, so = _root(tmp_path), {}
    warehouses.bind_namespace(root, so, "team_ns", "wh-a", "s3://bkt-a")
    with pytest.raises(NamespaceAlreadyExistsError):
        warehouses.bind_namespace(root, so, "team_ns", "wh-b", "s3://bkt-b")
    assert warehouses.warehouse_for_namespace(root, so, "team_ns") == "s3://bkt-a"  # winner intact


def test_bind_namespace_same_binding_is_idempotent(tmp_path: Any) -> None:
    # The partial-failure retry path (binding written, a later step failed, caller retries) must
    # converge — same (warehouse_id, root_uri) re-bind is a no-op, not a 409.
    root, so = _root(tmp_path), {}
    warehouses.bind_namespace(root, so, "team_ns", "wh-a", "s3://bkt-a")
    warehouses.bind_namespace(root, so, "team_ns", "wh-a", "s3://bkt-a")  # no raise
    assert warehouses.warehouse_for_namespace(root, so, "team_ns") == "s3://bkt-a"
