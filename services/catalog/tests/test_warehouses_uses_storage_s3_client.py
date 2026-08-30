"""CAT-CORE-11 — the warehouse registry's bucket client is ``storage.s3_client``, never a hand-rolled boto3 one.

``packages/storage`` is the estate's canonical S3 seam: path-style addressing, s3v4 signing, retries,
timeouts, and the endpoint/CA/insecure env resolution live there ONCE. A raw ``boto3.client("s3", ...)``
in the warehouse registry silently forfeits all of it — most concretely path-style addressing, without
which a bucket create against RustFS/MinIO resolves a virtual-hosted DNS name that does not exist.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from catalog.services import warehouses


_WAREHOUSES_PY = pathlib.Path(warehouses.__file__)


def test_warehouses_never_imports_boto3() -> None:
    """No ``import boto3`` — top-level or inline — in the registry module; the client comes from storage.

    ``botocore.exceptions`` stays allowed: catching ``ClientError`` is not building a client.
    """
    offences: list[str] = []
    for node in ast.walk(ast.parse(_WAREHOUSES_PY.read_text())):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "boto3" for alias in node.names):
            offences.append(f"warehouses.py:{node.lineno} imports boto3")
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "boto3":
            offences.append(f"warehouses.py:{node.lineno} imports from boto3")
    assert not offences, "the warehouse registry hand-rolls its S3 client instead of storage.s3_client:\n  " + "\n  ".join(offences)


def test_the_bucket_client_is_built_by_storage_s3_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring, not just the import: provisioning routes through ``storage.s3_client`` with the
    registry's own endpoint + credentials, so every seam guarantee (path-style, s3v4, retries) applies."""
    calls: list[tuple[Any, ...]] = []

    class _Client:
        def create_bucket(self, **kwargs: Any) -> None:
            calls.append(("create_bucket", kwargs))

    def fake_s3_client(endpoint: Any = None, **kwargs: Any) -> _Client:
        calls.append(("s3_client", endpoint, kwargs))
        return _Client()

    import storage

    monkeypatch.setattr(storage, "s3_client", fake_s3_client)
    warehouses.provision_bucket(
        "wh-bucket",
        {"endpoint": "http://rustfs:9000", "access_key_id": "ak", "secret_access_key": "sk"},
    )
    assert calls and calls[0][0] == "s3_client", "provision_bucket did not build its client via storage.s3_client"
    assert calls[0][1] == "http://rustfs:9000"
    assert calls[0][2] == {"access_key": "ak", "secret_key": "sk"}
    assert ("create_bucket", {"Bucket": "wh-bucket"}) in calls
