"""The storage builders derive `allow_http` and the pyarrow scheme from the endpoint by one case-insensitive rule.

Two builders hand options to object_store: `lakehouse.objectfs.lance_storage_options`
(every vend and most service opens) and the catalog's `namespace_properties` (its own `dir` connection).
object_store lowercases the scheme — measured on pylance 12.0.0 against a closed
port, `HTTP://127.0.0.1:1` with `allow_http=true` requests `http://127.0.0.1:1/…` and with
`allow_http=false` dies at `builder error`. So a case-sensitive `startswith("http://")` hands an
upper-case plaintext endpoint `false`, and the client is never built.

The pyarrow half reads the same endpoint through `s3_filesystem`.
pyarrow accepts only a lowercase `http`/`https` scheme — measured on pyarrow 25.0.0, `scheme="HTTP"`
raises `ArrowInvalid: Invalid S3 connection scheme 'HTTP'` — so an endpoint Lance opens must not be
one every pyarrow consumer refuses. An empty endpoint is AWS proper on both halves, and its regional
endpoint is TLS.

An endpoint's PATH is part of the root both halves address. Measured against a local listener for
`http://<host>/tenant`: Lance lists `GET /tenant/bucket?…` and boto3 heads `/tenant/bucket/key`, so a
pyarrow override that keeps only the host reaches `/bucket/key`, a different root of the same store.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import lance
import pyarrow.fs as pafs
import pytest
from pydantic import SecretStr

from catalog.core.config import Settings as CatalogSettings
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage_options import s3_filesystem


def _lance(endpoint: str) -> str:
    return lance_storage_options(endpoint, S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK")), "us-east-1").options["allow_http"]


def _catalog(endpoint: str) -> str:
    return CatalogSettings(LANCE_S3_ENDPOINT=endpoint, s3_access_key_id="k").namespace_properties()["storage.allow_http"]


_BUILDERS: dict[str, Callable[[str], str]] = {
    "lance_storage_options": _lance,
    "namespace_properties": _catalog,
}


@pytest.mark.parametrize("builder", _BUILDERS)
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("HTTP://rustfs:9000", "true"),
        ("HTTPS://s3.example.com", "false"),
    ],
)
def test_allow_http_follows_the_scheme_in_any_case(builder: str, endpoint: str, expected: str) -> None:
    assert _BUILDERS[builder](endpoint) == expected


_PYARROW_CASES = [
    ("http://rustfs:9000/", "http", "rustfs:9000"),
    ("http://rustfs:9000/tenant/", "http", "rustfs:9000/tenant"),
    ("HTTP://rustfs:9000", "http", "rustfs:9000"),
    ("HTTPS://s3.example.com", "https", "s3.example.com"),
    ("", "https", ""),
]


@pytest.mark.parametrize(("endpoint", "scheme", "host"), _PYARROW_CASES)
def test_the_pyarrow_filesystem_reads_the_scheme_by_the_same_rule(endpoint: str, scheme: str, host: str) -> None:
    built = s3_filesystem(lance_storage_options(endpoint, S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK")), "us-east-1"))

    assert built == pafs.S3FileSystem(access_key="AK", secret_key="SK", endpoint_override=host, scheme=scheme, region="us-east-1")
    assert (scheme == "http") == (_lance(endpoint) == "true"), "pyarrow and Lance disagree on whether this endpoint is plaintext"


_EMPTY_LISTING = (
    b'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Name>bucket</Name><KeyCount>0</KeyCount><IsTruncated>false</IsTruncated></ListBucketResult>"
)
_UPLOAD_STARTED = (
    b'<?xml version="1.0" encoding="UTF-8"?><InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Bucket>bucket</Bucket><Key>key</Key><UploadId>upload</UploadId></InitiateMultipartUploadResult>"
)
_UPLOAD_COMPLETED = (
    b'<?xml version="1.0" encoding="UTF-8"?><CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b'<Bucket>bucket</Bucket><Key>key</Key><ETag>"etag"</ETag></CompleteMultipartUploadResult>'
)


@pytest.fixture
def s3_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[str]]]:
    """A plaintext S3 stand-in on a free port: ``(its base URL, the request lines it received)``.

    It answers an empty listing, a 404 for anything else read, and a multipart upload that completes,
    so both clients get far enough to show which path they address and neither retries.
    """
    monkeypatch.delenv("AWS_ALLOW_HTTP", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # answers the SDK's `Expect: 100-continue`, which otherwise waits 1 s

        def _answer(self) -> None:
            received.append(f"{self.command} {self.path}")
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            query = urlsplit(self.path).query
            status, body = 404, b""
            if self.command == "GET" and "list-type=2" in query:
                status, body = 200, _EMPTY_LISTING
            elif self.command == "POST":
                status, body = 200, _UPLOAD_COMPLETED if "uploadId=" in query else _UPLOAD_STARTED
            elif self.command == "PUT":
                status = 200
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"etag"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _answer

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own parameter name
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", received
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("path", ["", "/tenant/"])
def test_pyarrow_addresses_the_root_lance_addresses(s3_stub: tuple[str, list[str]], path: str) -> None:
    """The request that reaches the store, not the override string: both halves see one root."""
    url, received = s3_stub
    options = lance_storage_options(f"{url}{path}", S3KeyPair(access_key_id="AK", secret_access_key=SecretStr("SK")), "us-east-1")
    root = path.rstrip("/")

    with pytest.raises(ValueError, match="not found"):
        lance.dataset("s3://bucket/key", storage_options=options.in_clear())
    assert received[0].startswith(f"GET {root}/bucket?"), received

    received.clear()
    s3_filesystem(options).get_file_info("bucket/key")
    assert received[0] == f"HEAD {root}/bucket/key", received
