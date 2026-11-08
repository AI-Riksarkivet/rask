"""The committed-file identity reads keep every store connection they open, so a sweep reconnects for none of them.

`lakehouse.stored_objects.identities` asks the store about sixteen objects at once through one client, and a client
returns each connection to its pool when its request ends; a connection the pool has no room for is closed, and the
next batch connects again. Measured on the dev estate 2026-11-08 (helm revision 24): urllib3 logged `Connection pool is
full, discarding connection: rask-rustfs. Connection pool size: 10` on every sweep tick. The store here answers each
HEAD after a pause, so all sixteen requests are in flight together, as they are against a store across the network.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import SecretStr

from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import StorageOptions
from lakehouse.stored_objects import identities


class _SlowStore(BaseHTTPRequestHandler):
    """Answers every HEAD with one object's identity after a pause, over a connection it keeps open."""

    protocol_version = "HTTP/1.1"

    def do_HEAD(self) -> None:
        time.sleep(0.3)
        self.send_response(200)
        self.send_header("ETag", '"e"')
        self.send_header("Content-Length", "1")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's signature
        return


@pytest.fixture
def store() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowStore)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_sixteen_identity_reads_through_one_client_keep_every_connection(store: str, caplog: pytest.LogCaptureFixture) -> None:
    options = StorageOptions(key_pair=S3KeyPair(access_key_id="k", secret_access_key=SecretStr("s")), options={}).with_endpoint(store)
    keys = [f"data/f{n}.lance" for n in range(16)]

    with caplog.at_level(logging.WARNING, logger="urllib3.connectionpool"):
        found = identities("s3://bucket/table", options, keys)

    discarded = [record.getMessage() for record in caplog.records if "pool is full" in record.getMessage()]
    assert (sorted(found), discarded) == (sorted(keys), [])
