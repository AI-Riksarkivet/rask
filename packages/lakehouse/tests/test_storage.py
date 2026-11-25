from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws
from pydantic import SecretStr

from lakehouse.problems.outage import reads_as_store_outage
from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.errors import StoreUnansweredError
from lakehouse.storage.options import StorageOptions
from lakehouse.storage.stored_objects import delete_objects, list_under
from lakehouse.storage.uri import s3_object, split_s3_uri


def test_split_s3_uri_splits_bucket_from_prefix() -> None:
    assert split_s3_uri("s3://bucket/foo/bar/") == ("bucket", "foo/bar")
    assert split_s3_uri("s3://bucket") == ("bucket", "")


def test_s3_object_joins_a_key_beneath_a_location() -> None:
    assert s3_object("s3://bucket/root/", "_k/r.json") == ("bucket", "root/_k/r.json")
    assert s3_object("s3://bucket", "_k/r.json") == ("bucket", "_k/r.json")


@pytest.mark.parametrize(
    ("location", "listed"),
    [
        pytest.param("s3://bkt", ["bkt/table/data/0.lance", "bkt/top"], id="a-buckets-root"),
        pytest.param("s3://bkt/table", ["bkt/table/data/0.lance"], id="a-key"),
    ],
)
@mock_aws
def test_a_listing_names_every_object_beneath_its_location(location: str, listed: list[str]) -> None:
    """A warehouse's root is its bucket: ``s3://bkt`` names every object in it, where a key names what lies beneath it."""
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="bkt")
    for key in ("top", "table/data/0.lance"):
        client.put_object(Bucket="bkt", Key=key, Body=b"x")
    options = StorageOptions(key_pair=S3KeyPair(access_key_id="k", secret_access_key=SecretStr("s")), options={})

    assert sorted(stored.path for stored in list_under(location, options)) == listed


#: The per-key error the failing store answers a batch delete that names the key.
_PER_KEY_ERRORS: Final = frozendict({b"busy": b"SlowDown", b"denied": b"AccessDenied"})


class _FailingStore(BaseHTTPRequestHandler):
    """A store that answers a listing 503, and a batch delete with a per-key error for the key ``busy`` (``SlowDown``)
    and for the key ``denied`` (``AccessDenied``)."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._answer(503, b"<Error><Code>ServiceUnavailable</Code><Message>try again</Message></Error>")

    def do_POST(self) -> None:
        asked = self.rfile.read(int(self.headers["Content-Length"]))
        failed = b"".join(
            b"<Error><Key>%b</Key><Code>%b</Code><Message>%b</Message></Error>" % (key, code, code)
            for key, code in _PER_KEY_ERRORS.items()
            if b"<Key>%b</Key>" % key in asked
        )
        self._answer(200, b"<DeleteResult><Deleted><Key>idle</Key></Deleted>%b</DeleteResult>" % failed)

    def _answer(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's signature
        return


@pytest.fixture
def failing_store() -> Iterator[StorageOptions]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FailingStore)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield StorageOptions(key_pair=S3KeyPair(access_key_id="k", secret_access_key=SecretStr("s")), options={}).with_endpoint(
        f"http://127.0.0.1:{server.server_address[1]}"
    )
    server.shutdown()
    server.server_close()


def test_a_listing_the_store_does_not_answer_raises_its_own_error_chained(failing_store: StorageOptions) -> None:
    with pytest.raises(StoreUnansweredError) as caught:
        list_under("s3://bucket/table", failing_store)

    assert isinstance(caught.value.__cause__, ClientError)


@pytest.mark.parametrize(
    ("key", "outage"),
    [
        pytest.param("busy", True, id="a-key-the-store-did-not-get-to"),
        pytest.param("denied", False, id="a-key-the-store-refused"),
    ],
)
def test_a_batch_delete_reads_as_an_outage_only_for_a_key_the_store_did_not_get_to(failing_store: StorageOptions, key: str, outage: bool) -> None:
    """A per-key error in a DeleteObjects answer is no client exception, so the delete's own error says which it is."""
    with pytest.raises(OSError) as caught:
        delete_objects("s3://bucket/table", failing_store, ["bucket/idle", f"bucket/{key}"])

    assert reads_as_store_outage(caught.value) is outage
