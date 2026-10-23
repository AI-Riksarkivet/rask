"""What reaches an S3 store: a proxy in front of one that forwards every request unchanged and keeps what each asked.

Lance's and pyarrow's native S3 clients go through no Python hook, so a claim about how many requests a door sends is
counted on the wire. The Host header is forwarded as sent, so a SigV4 signature made for the proxy's address is the one
the store reads.
"""

from __future__ import annotations

import http.client
import http.server
import threading
import urllib.parse
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

from pydantic import BaseModel


class StoreRequest(BaseModel):
    """One request the store answered: its method, its key path and its query parameters."""

    method: str
    path: str
    query: dict[str, list[str]]

    def lists(self, prefix: str) -> bool:
        """Whether this is a listing of the keys under ``prefix``."""
        return self.method == "GET" and "list-type" in self.query and self.query.get("prefix", [""])[0] == prefix


class _Forwarding(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: tuple[str, int]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.upstream = upstream
        self.lock = threading.Lock()
        self.seen: list[StoreRequest] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the stdlib's signature
        return

    def _body(self) -> bytes:
        """The request's body, whole: pyarrow streams a multipart upload's part as a chunked body."""
        if self.headers.get("Transfer-Encoding", "").lower() != "chunked":
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""
        body = bytearray()
        while size := int(self.rfile.readline().split(b";")[0].strip(), 16):
            body += self.rfile.read(size)
            self.rfile.readline()
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        return bytes(body)

    def _forward(self) -> None:
        forwarding = cast(_Forwarding, self.server)
        body = self._body()
        headers = {key: value for key, value in self.headers.items() if key.lower() not in ("connection", "content-length", "transfer-encoding", "expect")}
        upstream = http.client.HTTPConnection(*forwarding.upstream, timeout=60)
        try:
            upstream.request(self.command, self.path, body=body if body or self.command in ("PUT", "POST") else None, headers=headers)
            answer = upstream.getresponse()
            data = answer.read()
        finally:
            upstream.close()
        parsed = urllib.parse.urlsplit(self.path)
        with forwarding.lock:
            forwarding.seen.append(StoreRequest(method=self.command, path=urllib.parse.unquote(parsed.path), query=urllib.parse.parse_qs(parsed.query)))
        self.send_response_only(answer.status, answer.reason)
        for key, value in answer.getheaders():
            if key.lower() not in ("connection", "content-length", "transfer-encoding"):
                self.send_header(key, value)
        self.send_header("Content-Length", answer.getheader("Content-Length", "0") if self.command == "HEAD" else str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_PUT = do_POST = do_HEAD = do_DELETE = _forward


class CountedStore:
    """The proxy's address, and the requests it forwarded."""

    def __init__(self, server: _Forwarding) -> None:
        self._server = server

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def taken(self) -> list[StoreRequest]:
        """Every request forwarded since the last call, oldest first."""
        with self._server.lock:
            seen, self._server.seen = self._server.seen, []
        return seen


@contextmanager
def counted(upstream_url: str) -> Generator[CountedStore]:
    """A counting proxy in front of the store at ``upstream_url`` (``http://host:port``), for the life of the block."""
    parsed = urllib.parse.urlsplit(upstream_url)
    server = _Forwarding((parsed.hostname or "127.0.0.1", parsed.port or 80))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield CountedStore(server)
    finally:
        server.shutdown()
        server.server_close()
