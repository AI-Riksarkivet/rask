"""A vended, table-scoped credential must survive the shared storage seam intact.

An STS credential is a TRIPLE — key, secret, and a session token — and the token is the half that
carries the scoping. Drop it and the request signs as a different identity: either the key pair alone
(which STS refuses, a 403 with nothing naming the cause) or, when the caller had no static key either,
whatever ambient credential the pod's default chain finds. The second case is the one that matters for
zero trust, because it FAILS OPEN: the request succeeds with broader rights than the credential the
catalog deliberately scoped.

`records._s3_client` already forwards the token and says why. These pin the other two sinks, so a
credential does not survive one hop and die at the next:

* ``lance_storage_options`` — the builder that exists precisely so a hand-rolled copy cannot omit a key.
* ``s3_filesystem`` — the pyarrow half, used by ``fs_and_base`` and by the catalog's own commit-time
  file verification.

A filesystem from the resolver carries the credential's rights and no more: it may not create a bucket. Buckets are the
platform's, made by the chart's provisioning hook, and a lakehouse process given a write on a missing one has been pointed
at the wrong store, which a created bucket would hide while widening what the process can do to the store.

The token's spelling is pinned against object_store's ACTUAL behaviour rather than against a doc: it
silently ignores storage-option keys it does not recognise (verified 2026-09-03 — a deliberately
invented key produced no error and no signature change), so a mis-spelled option would drop the token
with nothing to notice. The wire test below is what makes that unmissable.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import boto3
import pytest
from moto.server import ThreadedMotoServer
from pydantic import SecretStr

from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.objectfs import fs_and_base, lance_storage_options


@pytest.fixture
def moto_url() -> Iterator[str]:
    """An S3 store of the test's own, holding no bucket, that pyarrow's native client reaches over HTTP."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.mark.parametrize("absent", [""])
def test_the_builder_emits_no_token_key_when_the_credential_is_static(absent: str) -> None:
    """A root-key caller must not grow an empty ``session_token``.

    object_store treats a present-but-empty token as a token, so an empty string signs a request with
    ``x-amz-security-token: `` and is refused — the key is absent or it is real. The empty case is the
    one driven because a config read is far likelier to yield ``""`` than ``None``: an env var that
    exists and is blank is exactly what a half-configured vending mode produces.
    """
    built = lance_storage_options(
        "http://s3:9000", S3KeyPair(access_key_id="AKIA", secret_access_key=SecretStr("secret"), session_token=SecretStr(absent)), "us-east-1"
    ).in_clear()
    assert "aws_session_token" not in built
    assert "session_token" not in built


@pytest.mark.parametrize("token", ["TOKEN-ABC"])
def test_a_built_option_set_actually_signs_with_the_token(token: str) -> None:
    """The property that matters: what the builder emits reaches the wire as ``x-amz-security-token``.

    Driven through real Lance against a capturing HTTP listener, so it pins the builder's spelling
    against object_store itself. A test asserting only the dict shape would pass against a key
    object_store ignores.
    """
    import lance

    captured: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            captured.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib's own parameter name
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        options = lance_storage_options(
            f"http://127.0.0.1:{server.server_address[1]}",
            S3KeyPair(access_key_id="AKIA", secret_access_key=SecretStr("secret"), session_token=SecretStr(token)),
            "us-east-1",
        )
        # The listener answers 404, so the open always fails — what is under test is the SIGNED
        # REQUEST it made on the way, captured above, not the outcome.
        with contextlib.suppress(OSError, ValueError):
            lance.dataset("s3://bucket/t", storage_options=options.in_clear())
    finally:
        server.shutdown()

    assert captured, "Lance never reached the endpoint, so nothing was signed"
    assert captured[0].get("x-amz-security-token") == token


def test_a_filesystem_from_the_resolver_is_refused_a_bucket(moto_url: str) -> None:
    """A bucket-level create through the resolver's filesystem fails, and the store holds no bucket afterwards."""
    options = lance_storage_options(moto_url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
    fs, base = fs_and_base("s3://unprovisioned/control", options)

    with pytest.raises(OSError, match="allow_bucket_creation"):
        fs.create_dir(base.split("/", 1)[0])

    store = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    assert store.list_buckets()["Buckets"] == []
