"""The issuer mirror serves the cluster issuer's discovery and keys to a verifier that sends no credential ([[XC-077]]).

OpenFGA fetches `<issuer>/.well-known/openid-configuration` and then its `jwks_uri` with no bearer, and the k3s
issuer answers both 401 without one. RUN: the real app (`with TestClient`, so its lifespan builds the upstream
client) in front of a stub issuer that, like the API server, answers 401 unless the request carries the token
the mirror's file holds at that moment, and advertises a `jwks_uri` on another host, as k3s advertises its node
IP. A verifier must be pointed back at the mirror for the keys, and the file is rotated between the two GETs.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from service_kit.governed.issuer_mirror import IssuerMirrorSettings, create_app


_KEYS: dict[str, Any] = {"keys": [{"kty": "RSA", "kid": "k3s-1", "alg": "RS256", "use": "sig", "n": "AQAB", "e": "AQAB"}]}


@pytest.fixture
def issuer(tmp_path: Path) -> Iterator[tuple[str, Path, list[str]]]:
    """A stub issuer on a free port: its URL, the token file it admits, and every bearer it was sent."""
    token = tmp_path / "token"
    token.write_text("mirror-token-1\n")
    seen: list[str] = []
    port: list[int] = []

    class Issuer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            bearer = self.headers.get("authorization", "")
            seen.append(bearer)
            base = f"http://127.0.0.1:{port[0]}"
            if bearer != f"Bearer {token.read_text().strip()}":
                status, body = 401, {"kind": "Status", "code": 401}
            elif self.path == "/.well-known/openid-configuration":
                status, body = 200, {"issuer": "https://kubernetes.default.svc.cluster.local", "jwks_uri": f"{base}/node-ip/openid/v1/jwks"}
            elif self.path == "/node-ip/openid/v1/jwks":
                status, body = 200, _KEYS
            else:
                status, body = 404, {}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's own parameter name
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
    port.append(server.server_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", token, seen
    finally:
        server.shutdown()
        server.server_close()


def test_a_verifier_with_no_credential_reads_the_discovery_and_keys_through_the_mirror(issuer: tuple[str, Path, list[str]]) -> None:
    url, token, seen = issuer
    settings = IssuerMirrorSettings(issuer=url, public_url="http://rask-issuer-mirror:8080", token_file=str(token))

    with TestClient(create_app(settings)) as verifier:
        discovery = verifier.get("/.well-known/openid-configuration")
        token.write_text("mirror-token-2\n")
        keys = verifier.get("/openid/v1/jwks")

    assert (discovery.status_code, keys.status_code) == (200, 200), f"{discovery.text} {keys.text}"
    assert discovery.json() == {"issuer": "https://kubernetes.default.svc.cluster.local", "jwks_uri": "http://rask-issuer-mirror:8080/openid/v1/jwks"}
    assert keys.json() == _KEYS
    assert seen == ["Bearer mirror-token-1", "Bearer mirror-token-2", "Bearer mirror-token-2"]
