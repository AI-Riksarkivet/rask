"""Conditional-create on the control-root registries.

The registry's id-minting writes (`_projects/<id>.json`, `_warehouses/<id>.json`) were
plain overwrites, so every guard in front of them was check-then-act: two concurrent creates of one
warehouse id under different projects both read "absent" and the last writer won — on exactly the
tenant-isolation guards (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" CAT-CORE-05, same defect class). The store
demonstrably honors put-if-not-exists (`tests/e2e-py/test_object_store_cas_e2e.py` proves
``If-None-Match: *`` against RustFS), so the fix is to USE it: ``lakehouse.storage.conditional_records``
is the one conditional-create seam, and the id-minting doors go through it.

Local-FS branch is exercised directly (``open(..., "xb")`` exclusivity — the same arbitration, by
the OS); the S3 branch is driven through a fake client at the ``storage.client.s3_client`` seam, pinning the ``IfNoneMatch`` parameter
and the 412 → ``RecordExistsError`` mapping, mirroring how ``test_vending.py`` pins the STS call.
The live contended-writer proof against RustFS lives in ``tests/e2e-py/test_registry_cas_e2e.py``
(env-gated, ``cas`` marker) — this file is the always-running half.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import lakehouse.storage.client as storage_client
from lakehouse.storage import conditional_records
from lakehouse.storage.errors import PreconditionFailedError, StorageError, StoreRefusedError, WriteConflictError
from lakehouse.storage.options import StorageOptions


# --------------------------------------------------------------------------- #
# the seam: create_json (local-FS branch — OS-arbitrated exclusivity)
# --------------------------------------------------------------------------- #


def test_create_json_local_bare_path_root(tmp_path: Any) -> None:
    # control_root may be a bare local path (no file:// scheme) in dev — same exclusivity.
    conditional_records.create_json(str(tmp_path), StorageOptions(), "_projects/acme.json", record={"id": "acme"})
    with pytest.raises(conditional_records.RecordExistsError):
        conditional_records.create_json(str(tmp_path), StorageOptions(), "_projects/acme.json", record={"id": "acme"})


# --------------------------------------------------------------------------- #
# the seam: create_json (S3 branch — store-arbitrated If-None-Match)
# --------------------------------------------------------------------------- #

_SO = StorageOptions.from_flat({"endpoint": "http://rf:9000", "access_key_id": "k", "secret_access_key": "s", "region": "us-east-1"})


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


def test_create_json_s3_412_maps_to_record_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost race is no ``OSError``: every ``except OSError`` around a store call would answer it as an outage (503)
    where its caller answers 409."""
    client = MagicMock()
    client.put_object.side_effect = _client_error("PreconditionFailed", 412)
    monkeypatch.setattr(storage_client, "s3_client", lambda *_a, **_kw: client)
    with pytest.raises(conditional_records.RecordExistsError) as caught:
        conditional_records.create_json("s3://ctrl", _SO, "_warehouses/wh-a.json", record={"id": "wh-a"})
    assert not isinstance(caught.value, OSError)
    assert isinstance(caught.value.__cause__, PreconditionFailedError)


@pytest.mark.parametrize(
    ("code", "status", "raised"),
    [
        pytest.param("AccessDenied", 403, StoreRefusedError, id="a-refusal"),
        pytest.param("ConditionalRequestConflict", 409, WriteConflictError, id="another-write-in-flight"),
    ],
)
def test_create_json_s3_other_client_error_propagates(monkeypatch: pytest.MonkeyPatch, code: str, status: int, raised: type[StorageError]) -> None:
    # A refused conditional PUT is the one expected signal; anything else (auth, throttling, outage, a write of the
    # key still in flight, which may yet fail) must surface as itself: laundering it into "already exists" would hide
    # a real failure.
    client = MagicMock()
    client.put_object.side_effect = _client_error(code, status)
    monkeypatch.setattr(storage_client, "s3_client", lambda *_a, **_kw: client)
    with pytest.raises(StorageError) as caught:
        conditional_records.create_json("s3://ctrl", _SO, "_warehouses/wh-a.json", record={"id": "wh-a"})
    assert type(caught.value) is raised
    assert isinstance(caught.value.__cause__, ClientError)
