"""The registry's conditional-write client is `storage.s3_client`, never a hand-rolled boto3 one.

`packages/storage` is the estate's canonical S3 seam: path-style addressing, s3v4 signing, adaptive
retries, connect/read timeouts and the endpoint/CA/insecure env resolution live there ONCE. A raw
`boto3.client("s3", ...)` here inherits botocore's defaults instead, which include NO timeouts — a
control-root PUT against an unreachable store then hangs the request rather than failing it, and the
conditional-create door is on the synchronous path of every warehouse/project mint.

The registry's own `storage_options` carry a per-warehouse `region` and, when the caller was handed
vended credentials, a `session_token`. Both must survive the swap: a dropped region silently pins the
warehouse to `AWS_REGION`, and a dropped token makes a scoped credential sign as an unknown identity.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

import pytest

from service_kit.lakehouse import records


_RECORDS_PY = pathlib.Path(records.__file__)


def test_records_never_imports_boto3() -> None:
    """No `import boto3` — top-level or inline. `botocore.exceptions` stays allowed: catching
    `ClientError` is reading the wire protocol's error shape, not building a client."""
    offences: list[str] = []
    for node in ast.walk(ast.parse(_RECORDS_PY.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "boto3" for alias in node.names):
            offences.append(f"records.py:{node.lineno} imports boto3")
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "boto3":
            offences.append(f"records.py:{node.lineno} imports from boto3")
    assert not offences, "the registry hand-rolls its S3 client instead of storage.s3_client:\n  " + "\n  ".join(offences)


class _RecordingClient:
    """A stand-in S3 client that remembers the one call each test makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(("put_object", kwargs))

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))

        class _Body:
            def read(self) -> bytes:
                return json.dumps({"id": "w", "status": "active"}).encode()

        return {"Body": _Body(), "ETag": '"abc123"'}


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> tuple[_RecordingClient, list[tuple[Any, dict[str, Any]]]]:
    """Replace `storage.s3_client` itself, so the assertion is on the SEAM the registry must use."""
    built: list[tuple[Any, dict[str, Any]]] = []
    client = _RecordingClient()

    def fake_s3_client(endpoint: Any = None, **kwargs: Any) -> _RecordingClient:
        built.append((endpoint, kwargs))
        return client

    import storage

    monkeypatch.setattr(storage, "s3_client", fake_s3_client)
    return client, built


_SO = {
    "endpoint": "http://rf:9000",
    "access_key_id": "ak",
    "secret_access_key": "sk",
    "region": "eu-north-1",
    "session_token": "tok",
}


def test_create_json_builds_its_client_through_the_storage_seam(seam: tuple[_RecordingClient, list[tuple[Any, dict[str, Any]]]]) -> None:
    client, built = seam

    records.create_json("s3://ctrl/prefix", _SO, "_warehouses/wh-a.json", {"id": "wh-a"})

    assert built, "create_json did not build its client via storage.s3_client"
    endpoint, kwargs = built[0]
    assert endpoint == "http://rf:9000"
    assert kwargs == {"access_key": "ak", "secret_key": "sk", "session_token": "tok", "region": "eu-north-1"}
    assert client.calls[0][1]["IfNoneMatch"] == "*"


def test_mutate_json_builds_its_client_through_the_storage_seam(seam: tuple[_RecordingClient, list[tuple[Any, dict[str, Any]]]]) -> None:
    client, built = seam

    records.mutate_json("s3://ctrl/prefix", _SO, "_warehouses/wh-a.json", lambda r: {**r, "status": "deactivated"}, attempts=1)

    assert built, "mutate_json did not build its client via storage.s3_client"
    assert all(kwargs["region"] == "eu-north-1" and kwargs["session_token"] == "tok" for _endpoint, kwargs in built)
    assert client.calls[-1][1]["IfMatch"] == "abc123"


def test_a_storage_options_without_a_region_still_reaches_the_seam(seam: tuple[_RecordingClient, list[tuple[Any, dict[str, Any]]]]) -> None:
    """`us-east-1` is the registry's own default for a root that names no region — the seam must be
    handed it explicitly rather than left to resolve `AWS_REGION`, which is a different value."""
    _client, built = seam

    records.create_json("s3://ctrl", {"endpoint": "http://rf:9000"}, "_projects/acme.json", {"id": "acme"})

    assert built[0][1]["region"] == "us-east-1"
    assert built[0][1]["session_token"] is None
