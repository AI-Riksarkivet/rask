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

The token's spelling is pinned against object_store's ACTUAL behaviour rather than against a doc: it
silently ignores storage-option keys it does not recognise (verified 2026-09-03 — a deliberately
invented key produced no error and no signature change), so a mis-spelled option would drop the token
with nothing to notice. The wire test below is what makes that unmissable.
"""

from __future__ import annotations

import contextlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from service_kit.lakehouse.objectfs import lance_storage_options, s3_filesystem


def test_the_lance_builder_carries_a_session_token() -> None:
    options = lance_storage_options("http://s3:9000", "AKIA", "secret", "us-east-1", session_token="TOKEN")
    assert options["session_token"] == "TOKEN"


@pytest.mark.parametrize("absent", [None, ""])
def test_the_builder_emits_no_token_key_when_the_credential_is_static(absent: str | None) -> None:
    """A root-key caller must not grow an empty ``session_token``.

    object_store treats a present-but-empty token as a token, so an empty string signs a request with
    ``x-amz-security-token: `` and is refused — the key is absent or it is real. Both the unset and the
    empty case are covered because a config read is far likelier to yield ``""`` than ``None``: an
    env var that exists and is blank is exactly what a half-configured vending mode produces.
    """
    assert "session_token" not in lance_storage_options("http://s3:9000", "AKIA", "secret", "us-east-1", session_token=absent)


@pytest.mark.parametrize("token", ["TOKEN-ABC", "a/token+with/base64=chars"])
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
        options = lance_storage_options(f"http://127.0.0.1:{server.server_address[1]}", "AKIA", "secret", "us-east-1", session_token=token)
        # The listener answers 404, so the open always fails — what is under test is the SIGNED
        # REQUEST it made on the way, captured above, not the outcome.
        with contextlib.suppress(OSError, ValueError):
            lance.dataset("s3://bucket/t", storage_options=options)
    finally:
        server.shutdown()

    assert captured, "Lance never reached the endpoint, so nothing was signed"
    assert captured[0].get("x-amz-security-token") == token


def test_the_arrow_filesystem_carries_the_session_token() -> None:
    """``s3_filesystem`` is the other sink, and it fails OPEN when the token is dropped.

    pyarrow falls back to the default credential chain for anything it was not given, so a
    half-forwarded vended credential can end up signing with the pod's own role — more rights than the
    catalog vended, not fewer.
    """
    options = lance_storage_options("http://127.0.0.1:1", "AKIA", "secret", "us-east-1", session_token="TOKEN")
    filesystem = s3_filesystem(options)
    # `S3FileSystem` does not expose its credentials, so assert on what it was built with: pickling is
    # pyarrow's own round-trip of the constructor options.
    assert "TOKEN" in str(filesystem.__reduce__())
