"""The storage builders derive `allow_http` and the pyarrow scheme from the endpoint by one case-insensitive rule.

Three builders hand options to object_store: `service_kit.lakehouse.objectfs.lance_storage_options`
(every vend and most service opens), the catalog's `namespace_properties` (its own `dir` connection)
and `service_kit.media.config`'s `storage_options`. Two scripts build a set from their own arguments
through the first, the baked Ray training job and the model-artifact janitor, and are checked by what
they hand Lance. object_store lowercases the scheme — measured on pylance 12.0.0 against a closed
port, `HTTP://127.0.0.1:1` with `allow_http=true` requests `http://127.0.0.1:1/…` and with
`allow_http=false` dies at `builder error`. So a case-sensitive `startswith("http://")` hands an
upper-case plaintext endpoint `false`, and the client is never built.

The pyarrow half reads the same endpoint: `s3_filesystem`, and the builders that go through it —
ingest's estate-default source, the training job's artifact writer and the janitor's filesystem.
pyarrow accepts only a lowercase `http`/`https` scheme — measured on pyarrow 25.0.0, `scheme="HTTP"`
raises `ArrowInvalid: Invalid S3 connection scheme 'HTTP'` — so an endpoint Lance opens must not be
one every pyarrow consumer refuses. An empty endpoint is AWS proper on both halves, and its regional
endpoint is TLS.

An endpoint's PATH is part of the root both halves address. Measured against a local listener for
`http://<host>/tenant`: Lance lists `GET /tenant/bucket?…` and boto3 heads `/tenant/bucket/key`, so a
pyarrow override that keeps only the host reaches `/bucket/key`, a different root of the same store.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from unittest import mock
from urllib.parse import urlsplit

import lance
import pyarrow.fs as pafs
import pytest

from catalog.core.config import Settings as CatalogSettings
from ingest.objectstore import SourceConnection, source_filesystem
from service_kit.lakehouse.objectfs import StorageOptions, lance_storage_options, s3_filesystem
from service_kit.media.config import Settings as MediaSettings


_SCRIPTS = Path(__file__).parents[2] / "scripts"


def _load_script(stem: str) -> ModuleType:
    """A script loaded by path under its own module name, registered first so its dataclasses resolve."""
    spec = importlib.util.spec_from_file_location(f"{stem}_under_scheme_test", _SCRIPTS / f"{stem}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


train_job = _load_script("ray_train_job")
janitor = _load_script("model_artifact_janitor")


def _lance(endpoint: str) -> str:
    return lance_storage_options(endpoint, "AK", "SK", "us-east-1")["allow_http"]


def _catalog(endpoint: str) -> str:
    return CatalogSettings(LANCE_S3_ENDPOINT=endpoint, LANCE_S3_ACCESS_KEY_ID="k").namespace_properties()["storage.allow_http"]


def _media(endpoint: str) -> str:
    # Both static fields set, so the builder takes its no-secret-store branch.
    options = MediaSettings.model_validate(
        {"MEDIA_S3_ENDPOINT": endpoint, "MEDIA_S3_ACCESS_KEY_ID": "AK", "MEDIA_S3_SECRET_ACCESS_KEY": "SK"}
    ).storage_options()
    assert options is not None
    return options["allow_http"]


def _train_job_options(endpoint: str) -> StorageOptions:
    env = {"S3_ENDPOINT": endpoint, "S3_KEY": "AK", "S3_SECRET": "SK", "S3_REGION": "us-east-1"}
    with mock.patch.dict(os.environ, env):
        return train_job._storage_options()


def _janitor_options(endpoint: str) -> StorageOptions:
    """What the janitor's CLI hands `sweep`, read by standing in for the sweep itself."""
    handed: list[StorageOptions | None] = []

    def recording_sweep(**kwargs: object) -> object:
        options = kwargs["storage_options"]
        assert options is None or isinstance(options, dict)
        handed.append(options)
        return janitor.JanitorReport()

    argv = ["janitor", "--registry-uri", "s3://lake/registry", "--artifact-base", "s3://lake/artifacts"]
    argv += ["--s3-endpoint", endpoint, "--s3-key", "AK", "--s3-secret", "SK"]
    with mock.patch.object(janitor, "sweep", recording_sweep), mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
        janitor.main()
    [options] = handed
    assert options is not None
    return options


_BUILDERS: dict[str, Callable[[str], str]] = {
    "lance_storage_options": _lance,
    "namespace_properties": _catalog,
    "media": _media,
    "ray_train_job": lambda endpoint: _train_job_options(endpoint)["allow_http"],
    "model_artifact_janitor": lambda endpoint: _janitor_options(endpoint)["allow_http"],
}


@pytest.mark.parametrize("builder", _BUILDERS)
@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://rustfs:9000", "true"),
        ("HTTP://rustfs:9000", "true"),
        ("Http://rustfs:9000", "true"),
        ("https://s3.example.com", "false"),
        ("HTTPS://s3.example.com", "false"),
    ],
)
def test_allow_http_follows_the_scheme_in_any_case(builder: str, endpoint: str, expected: str) -> None:
    assert _BUILDERS[builder](endpoint) == expected


_PYARROW_CASES = [
    ("http://rustfs:9000", "http", "rustfs:9000"),
    ("http://rustfs:9000/", "http", "rustfs:9000"),
    ("http://rustfs:9000/tenant", "http", "rustfs:9000/tenant"),
    ("http://rustfs:9000/tenant/", "http", "rustfs:9000/tenant"),
    ("HTTP://rustfs:9000", "http", "rustfs:9000"),
    ("Http://rustfs:9000", "http", "rustfs:9000"),
    ("https://s3.example.com", "https", "s3.example.com"),
    ("HTTPS://s3.example.com", "https", "s3.example.com"),
    ("", "https", ""),
]


@pytest.mark.parametrize(("endpoint", "scheme", "host"), _PYARROW_CASES)
def test_the_pyarrow_filesystem_reads_the_scheme_by_the_same_rule(endpoint: str, scheme: str, host: str) -> None:
    built = s3_filesystem(lance_storage_options(endpoint, "AK", "SK", "us-east-1"))

    assert built == pafs.S3FileSystem(access_key="AK", secret_key="SK", endpoint_override=host, scheme=scheme, region="us-east-1")
    assert (scheme == "http") == (_lance(endpoint) == "true"), "pyarrow and Lance disagree on whether this endpoint is plaintext"


@pytest.mark.parametrize(("endpoint", "scheme", "host"), _PYARROW_CASES)
def test_ingest_estate_default_source_reads_the_scheme_by_the_same_rule(monkeypatch: pytest.MonkeyPatch, endpoint: str, scheme: str, host: str) -> None:
    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", endpoint)  # an empty value reads as unset
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    # An unkeyed, region-less filesystem asks the AWS SDK's instance-metadata probe, which off EC2
    # measured 14 s per construction; disabling it keeps the test local.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    assert source_filesystem(SourceConnection()) == pafs.S3FileSystem(endpoint_override=host, scheme=scheme)


@pytest.mark.parametrize(("endpoint", "scheme", "host"), _PYARROW_CASES)
def test_the_janitor_filesystem_reads_the_scheme_by_the_same_rule(endpoint: str, scheme: str, host: str) -> None:
    built, root = janitor._filesystem("s3://bucket/artifacts/", _janitor_options(endpoint))

    assert built == pafs.S3FileSystem(access_key="AK", secret_key="SK", endpoint_override=host, scheme=scheme, region="us-east-1")
    assert root == "bucket/artifacts"


@pytest.mark.parametrize("script", ["ray_train_job", "model_artifact_janitor"])
def test_a_script_opens_lance_with_the_estate_builders_options(script: str) -> None:
    """The builder's whole option set, not only `allow_http`: its `aws_`-prefixed credential keys are
    the ones that displace an ambient AWS_* environment (see `lance_storage_options`)."""
    read = {"ray_train_job": _train_job_options, "model_artifact_janitor": _janitor_options}[script]

    assert read("https://s3.example.com") == lance_storage_options("https://s3.example.com", "AK", "SK", "us-east-1")


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


@pytest.mark.parametrize("path", ["", "/", "/tenant", "/tenant/"])
def test_pyarrow_addresses_the_root_lance_addresses(s3_stub: tuple[str, list[str]], path: str) -> None:
    """The request that reaches the store, not the override string: both halves see one root."""
    url, received = s3_stub
    options = lance_storage_options(f"{url}{path}", "AK", "SK", "us-east-1")
    root = path.rstrip("/")

    with pytest.raises(ValueError, match="not found"):
        lance.dataset("s3://bucket/key", storage_options=options)
    assert received[0].startswith(f"GET {root}/bucket?"), received

    received.clear()
    s3_filesystem(options).get_file_info("bucket/key")
    assert received[0] == f"HEAD {root}/bucket/key", received


@pytest.mark.parametrize("path", ["", "/tenant"])
@pytest.mark.parametrize("scheme", ["http", "HTTP"])
def test_the_train_job_writes_its_artifacts_under_the_root_lance_addresses(
    s3_stub: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch, scheme: str, path: str
) -> None:
    url, received = s3_stub
    monkeypatch.setenv("S3_ENDPOINT", f"{scheme}{url.removeprefix('http')}{path}")
    monkeypatch.setenv("S3_KEY", "AK")
    monkeypatch.setenv("S3_SECRET", "SK")

    uris = train_job.write_artifacts("s3://bucket/models/m", "tok", {"weights.json": b"w"})

    assert uris == {"weights.json": "s3://bucket/models/m/tok/weights.json"}
    assert received, "the write reached no store"
    assert all(line.split(" ", 1)[1].startswith(f"{path}/bucket/models/m/tok/weights.json") for line in received), received
