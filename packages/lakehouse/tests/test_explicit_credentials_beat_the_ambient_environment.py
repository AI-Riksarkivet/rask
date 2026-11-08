"""The storage options a service signs with must win over any process's ambient AWS_* environment, in every process ([[LH-238]]).

Lance reads ``AWS_*`` variables into a dataset's options under their canonical names, ``aws_endpoint``, ``aws_region``
and the rest, but only where the options do not already hold that name (`lance_docs/lance/guide/object_store.md` § S3
Configuration: each key "can be used as both environment variables or keys in the storage_options parameter"). A bare
spelling (``endpoint``, ``region``, ``virtual_hosted_style_request``) is a second name for the same setting, so both
land in the map and the one applied last wins, in hash order. Measured 2026-11-08 on pylance 13.0.0, ten fresh
processes per spelling under ``AWS_ENDPOINT_URL``, ``AWS_REGION`` and ``AWS_VIRTUAL_HOSTED_STYLE_REQUEST``: with the
bare spellings 5 of 10 reached the ambient endpoint and half the requests were signed for the ambient region or
addressed virtual-hosted; with the canonical ones 10 of 10 reached the store the options named, path-style, in its
region. One process cannot show the race, so this test runs ten.

The credential keys are canonical too: with the bare ones object_store blended an ambient ``AWS_ACCESS_KEY_ID`` with the
options' secret and signed as neither identity (``403 SignatureDoesNotMatch``, measured in-cluster 2026-09-03).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel


#: Enough fresh processes that a per-process race this wide cannot pass by chance (2**-10 for one even split).
PROCESSES = 10
CHILD = Path(__file__).with_name("open_under_ambient_env.py")
_SCOPE = re.compile(r"Credential=([^/]+)/\d{8}/([^/]+)/s3/aws4_request")


class Request(BaseModel):
    path: str
    access_key: str
    region: str


def _parsed(head: str) -> Request:
    """The request line's path and the SigV4 credential scope's access key and region, empty where the head names none."""
    scope = _SCOPE.search(head)
    path = head.split(" ", 2)[1] if " " in head else ""
    return Request(path=path, access_key=scope.group(1) if scope else "", region=scope.group(2) if scope else "")


class Store:
    """A loopback listener standing in for an S3 store: it records each request's path and SigV4 scope and refuses it."""

    def __init__(self) -> None:
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self.requests: list[Request] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            head = b""
            while b"\r\n\r\n" not in head and (chunk := conn.recv(65536)):
                head += chunk
            request = _parsed(head.decode("latin-1"))
            with self._lock:
                self.requests.append(request)
            body = b"<Error><Code>AccessDenied</Code></Error>"
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))

    def close(self) -> None:
        self._sock.close()


@pytest.fixture
def stores() -> Iterator[tuple[Store, Store]]:
    ambient, explicit = Store(), Store()
    yield ambient, explicit
    ambient.close()
    explicit.close()


def test_every_fresh_process_sends_the_explicit_credential_to_the_explicit_store_under_a_conflicting_environment(stores: tuple[Store, Store]) -> None:
    ambient, explicit = stores
    conflicting = {
        "AWS_ENDPOINT_URL": ambient.url,
        "AWS_REGION": "eu-north-1",
        "AWS_DEFAULT_REGION": "eu-north-1",
        "AWS_ACCESS_KEY_ID": "AMBIENT",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
        "AWS_VIRTUAL_HOSTED_STYLE_REQUEST": "true",
        "AWS_ALLOW_HTTP": "true",
    }
    env = {**{key: value for key, value in os.environ.items() if not key.startswith("AWS_")}, **conflicting}
    buckets = [f"probe-{n}" for n in range(PROCESSES)]

    children = [
        subprocess.Popen([sys.executable, "-I", str(CHILD), explicit.url, bucket, "EXPLICIT", "explicit-secret", "us-east-1"], env=env) for bucket in buckets
    ]
    for child in children:
        child.wait(timeout=120)

    assert ambient.requests == []
    # Path-style names the bucket as the first path segment; a virtual-hosted request names none there.
    assert {urlsplit(request.path).path.split("/")[1] for request in explicit.requests} == set(buckets)
    assert {(request.access_key, request.region) for request in explicit.requests} == {("EXPLICIT", "us-east-1")}
