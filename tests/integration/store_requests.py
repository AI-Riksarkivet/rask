"""What reaches an S3 store: a proxy in front of one that forwards every request unchanged and keeps what each asked, and
that answers the requests a test names 503 ServiceUnavailable, as a store that cannot serve them does.

Lance's and pyarrow's native S3 clients go through no Python hook, so a claim about how many requests a door sends is
counted on the wire, and a claim about one read the store cannot serve is made there too. The Host header is
forwarded as sent, so a SigV4 signature made for the proxy's address is the one the store reads.

A client that names no endpoint addresses AWS proper, which no test reaches: :func:`at_aws_proper` stands a TLS front
for the regional endpoint before the counting proxy. pyarrow's ``S3FileSystem`` built with no endpoint ignores
``AWS_ENDPOINT_URL`` and ``AWS_ENDPOINT_URL_S3``, and its ``scheme`` too (measured on pyarrow 25.0.0: under either variable,
with ``endpoint_override`` empty or unset and ``scheme="http"``, it opened TLS to ``<bucket>.s3.us-east-1.amazonaws.com:443``),
so its own ``proxy_options`` and ``tls_ca_file_path`` are the only route to a local store, and the front hands a test both.
"""

from __future__ import annotations

import datetime
import http.client
import http.server
import re
import ssl
import tempfile
import threading
import urllib.parse
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import BaseModel


#: The access key id a SigV4 ``Authorization`` header's credential scope names.
_CREDENTIAL = re.compile(r"Credential=([^/]+)/")
#: Headers that describe one hop's connection, never forwarded across it.
_HOP_HEADERS = frozenset({"connection", "content-length", "transfer-encoding", "expect"})


class StoreRequest(BaseModel):
    """One request the store answered: its method, its key path, its query parameters and the access key that signed it."""

    method: str
    path: str
    query: dict[str, list[str]]
    #: The access key id the request's SigV4 credential names; empty for an unsigned request.
    access_key: str = ""

    def lists(self, prefix: str) -> bool:
        """Whether this is a listing of the keys under ``prefix``."""
        return self.method == "GET" and "list-type" in self.query and self.query.get("prefix", [""])[0] == prefix


#: The body S3 answers a request with while it cannot serve it (its ``ServiceUnavailable`` error).
_UNAVAILABLE = b"<?xml version='1.0' encoding='UTF-8'?><Error><Code>ServiceUnavailable</Code><Message>Service is unable to handle request.</Message></Error>"


class _Forwarding(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, upstream: tuple[str, int]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.upstream = upstream
        self.lock = threading.Lock()
        self.seen: list[StoreRequest] = []
        #: The requests answered 503 rather than forwarded: none unless a test names them (:meth:`CountedStore.answering_unavailable`).
        self.answered_unavailable: Callable[[StoreRequest], bool] | None = None


class _Relaying(http.server.BaseHTTPRequestHandler):
    """A hop that reads a request whole and relays it to an upstream store, answering with what the store answered."""

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

    def _relay(self, upstream: tuple[str, int], path: str, *, body: bytes, headers: dict[str, str]) -> None:
        """Send this request to ``upstream`` at ``path`` and answer with the store's answer."""
        connection = http.client.HTTPConnection(*upstream, timeout=60)
        try:
            connection.request(self.command, path, body=body if body or self.command in ("PUT", "POST") else None, headers=headers)
            answer = connection.getresponse()
            data = answer.read()
        finally:
            connection.close()
        self.send_response_only(answer.status, answer.reason)
        for key, value in answer.getheaders():
            if key.lower() not in _HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Content-Length", answer.getheader("Content-Length", "0") if self.command == "HEAD" else str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class _Handler(_Relaying):
    def _forward(self) -> None:
        forwarding = cast(_Forwarding, self.server)
        body = self._body()
        parsed = urllib.parse.urlsplit(self.path)
        signed = _CREDENTIAL.search(self.headers.get("Authorization", ""))
        request = StoreRequest(
            method=self.command,
            path=urllib.parse.unquote(parsed.path),
            query=urllib.parse.parse_qs(parsed.query),
            access_key=signed.group(1) if signed else "",
        )
        unavailable = forwarding.answered_unavailable
        if unavailable is not None and unavailable(request):
            self._answer_unavailable()
            return
        headers = {key: value for key, value in self.headers.items() if key.lower() not in _HOP_HEADERS}
        self._relay(forwarding.upstream, self.path, body=body, headers=headers)
        with forwarding.lock:
            forwarding.seen.append(request)

    def _answer_unavailable(self) -> None:
        self.send_response_only(503, "Service Unavailable")
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(_UNAVAILABLE)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(_UNAVAILABLE)

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
    def answering_unavailable(self, requests: Callable[[StoreRequest], bool]) -> Generator[None]:
        """For the life of the block, answer each of ``requests`` 503 ``ServiceUnavailable`` instead of forwarding it, as a
        store that cannot serve it answers; every other request is forwarded."""
        self._server.answered_unavailable = requests
        try:
            yield
        finally:
            self._server.answered_unavailable = None


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


class AwsProper(BaseModel):
    """How a client naming no endpoint reaches :func:`at_aws_proper`'s front: the proxy it sends through and the CA it trusts,
    pyarrow's ``proxy_options`` and ``tls_ca_file_path``."""

    model_config = {"frozen": True}

    proxy: str
    ca_file: str


class _Front(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store: str, tls: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), _Tunnel)
        parsed = urllib.parse.urlsplit(store)
        self.store = (parsed.hostname or "127.0.0.1", parsed.port or 80)
        self.tls = tls


class _Tunnel(_Relaying):
    def do_CONNECT(self) -> None:
        """Accept the client's tunnel to the regional endpoint and answer inside it as that endpoint, over TLS."""
        self.send_response_only(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        tunnel = cast(_Front, self.server).tls.wrap_socket(self.connection, server_side=True)
        self.connection, self.rfile, self.wfile = tunnel, tunnel.makefile("rb"), tunnel.makefile("wb")
        self.close_connection = False

    def _path_style(self) -> None:
        """Relay a virtual-hosted request, ``<bucket>.s3.<region>.amazonaws.com/<key>``, to the store as ``/<bucket>/<key>``."""
        front = cast(_Front, self.server)
        bucket = self.headers.get("Host", "").split(".s3.", 1)[0]
        headers = {key: value for key, value in self.headers.items() if key.lower() not in {*_HOP_HEADERS, "host"}}
        headers["Host"] = f"{front.store[0]}:{front.store[1]}"
        self._relay(front.store, f"/{bucket}{self.path}", body=self._body(), headers=headers)

    do_GET = do_PUT = do_POST = do_HEAD = do_DELETE = _path_style


def _regional_tls(directory: Path, region: str) -> tuple[ssl.SSLContext, Path]:
    """A server context for ``*.s3.<region>.amazonaws.com``, signed by a CA minted for this front alone, and that CA's file."""
    now = datetime.datetime.now(datetime.UTC)
    start, end = now - datetime.timedelta(hours=1), now + datetime.timedelta(hours=1)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "store front test CA")])
    ca = (
        x509.CertificateBuilder(
            issuer_name=ca_name,
            subject_name=ca_name,
            public_key=ca_key.public_key(),
            serial_number=x509.random_serial_number(),
            not_valid_before=start,
            not_valid_after=end,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    hosts = x509.SubjectAlternativeName([x509.DNSName(f"*.s3.{region}.amazonaws.com"), x509.DNSName(f"s3.{region}.amazonaws.com")])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"s3.{region}.amazonaws.com")])
    leaf = (
        x509.CertificateBuilder(
            issuer_name=ca_name,
            subject_name=leaf_name,
            public_key=key.public_key(),
            serial_number=x509.random_serial_number(),
            not_valid_before=start,
            not_valid_after=end,
        )
        .add_extension(hosts, critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file, chain, key_file = directory / "ca.pem", directory / "leaf.pem", directory / "leaf.key"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls.load_cert_chain(chain, key_file)
    return tls, ca_file


@contextmanager
def at_aws_proper(store: CountedStore, *, region: str = "us-east-1") -> Generator[AwsProper]:
    """A TLS front answering as ``region``'s S3 endpoint, relaying each request to ``store`` path-style, for the life of the
    block: what a client naming no endpoint is routed to through the proxy and CA it is handed."""
    with tempfile.TemporaryDirectory() as directory:
        tls, ca_file = _regional_tls(Path(directory), region)
        server = _Front(store.url, tls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield AwsProper(proxy=f"http://127.0.0.1:{server.server_address[1]}", ca_file=str(ca_file))
        finally:
            server.shutdown()
            server.server_close()
