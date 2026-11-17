#!/usr/bin/env python
"""The real store the Rust outbox is tried against, and the Python half of the check that both outboxes agree (ADR 0234).

Two commands, run by ``scripts/port_outbox_check.sh``:

    uv run --no-sync scripts/port_outbox_fixture.py issuer <port> <state-dir>
    uv run --no-sync scripts/port_outbox_fixture.py cross <endpoint> <bucket> <issuer>

``issuer`` stands in for the cluster's ServiceAccount issuer, as ``scripts/rustfs_web_identity_measure.py`` does: it
publishes the discovery document of an issuer whose ``iss`` is the cluster's, its key set, and, at
``/token?sub=<subject>``, a token in the shape the kubelet projects for the store's audience, signed with an RSA key
made in <state-dir> on first run. RustFS reads the discovery and the keys; a test reads a token as a pod reads its
projected file. It serves 127.0.0.1 alone.

``cross`` shows the two outboxes reach the same objects: each exchanges its own projected token for a session at the
store's STS (the Python through ``lakehouse.store_session.exchange``), the Python stages events the Rust lists, reads
and drops, and the Rust stages events the Python lists, reads and drops. The Rust half is the ``outbox_cross`` example
of ``lakehouse-core``. Exits non-zero, naming what differs, when anything does.
"""

from __future__ import annotations

import base64
import http.server
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://kubernetes.default.svc.cluster.local"
AUDIENCE = "rask-object-store"
#: The subjects the check provisions: the producer stages, the relay lists, reads and drops.
PRODUCER = "system:serviceaccount:rask:rask-sa-catalog"
RELAY = "system:serviceaccount:rask:rask-sa-lineage"
OUTBOX = "_lineage_outbox"


def _key(state: Path) -> Any:  # noqa: ANN401 - cryptography's key type
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    path = state / "issuer.pem"
    if not path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def _token(key: Any, sub: str) -> str:  # noqa: ANN401 - cryptography's key type
    import jwt

    now = int(time.time())
    namespace, name = sub.split(":")[2:4]
    claims = {
        "iss": ISSUER,
        "sub": sub,
        "aud": [AUDIENCE],
        "exp": now + 3600,
        "iat": now - 5,
        "nbf": now - 5,
        "jti": str(uuid.uuid4()),
        "kubernetes.io": {"namespace": namespace, "serviceaccount": {"name": name, "uid": str(uuid.uuid4())}},
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "check"})


def issuer(port: int, state: Path) -> None:
    key = _key(state)
    numbers = key.public_key().public_numbers()
    jwks = {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "check", "n": _b64(numbers.n), "e": _b64(numbers.e)}]}
    public = f"http://127.0.0.1:{port}"
    discovery = {
        "issuer": ISSUER,
        "jwks_uri": f"{public}/openid/v1/jwks",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the stdlib's name
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/token":
                payload, kind = _token(key, urllib.parse.parse_qs(url.query)["sub"][0]).encode(), "text/plain"
            else:
                body = {"/.well-known/openid-configuration": discovery, "/openid/v1/jwks": jwks}.get(url.path)
                if body is None:
                    self.send_error(404)
                    return
                payload, kind = json.dumps(body).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 - the stdlib's signature
            pass

    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def _projected(issuer_url: str, sub: str, directory: Path) -> Path:
    """A token for ``sub`` written where a pod's projected token would be."""
    with urllib.request.urlopen(f"{issuer_url}/token?{urllib.parse.urlencode({'sub': sub})}", timeout=10) as answer:  # noqa: S310 - the fixture's own issuer
        token = answer.read().decode()
    path = directory / sub.rsplit(":", 1)[-1]
    path.write_text(token)
    return path


def _rust(*args: str, env: dict[str, str]) -> Any:  # noqa: ANN401 - JSON
    cargo = ["cargo", "run", "--quiet", "--locked", "-p", "lakehouse-core", "--example", "outbox_cross", "--", *args]
    answered = subprocess.run(cargo, cwd=_ROOT / "rust", check=True, capture_output=True, text=True, env=env)  # noqa: S603 - a fixed argv, no shell
    return json.loads(answered.stdout)


def cross(endpoint: str, bucket: str, issuer_url: str) -> None:
    import os
    import tempfile

    from lakehouse import outbox
    from lakehouse.cloud_events import lineage_envelope
    from lakehouse.storage_options import StorageOptions
    from lakehouse.store_session import WebIdentity, exchange

    failures: list[str] = []

    def check(ok: bool, what: str, seen: object) -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {what}  ->  {seen}", flush=True)
        if not ok:
            failures.append(what)

    uri = f"s3://{bucket}/{OUTBOX}"
    with tempfile.TemporaryDirectory() as tokens:
        directory = Path(tokens)

        def options(sub: str) -> StorageOptions:
            identity = WebIdentity(
                token_file=_projected(issuer_url, sub, directory), endpoint=endpoint, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/rask"
            )
            session = exchange(identity, session_name="port-outbox-check", duration_seconds=900)
            return StorageOptions(key_pair=session.key_pair, options={}).with_endpoint(endpoint)

        def rust_env(sub: str) -> dict[str, str]:
            return os.environ | {
                "LINEAGE_S3_ENDPOINT": endpoint,
                "LINEAGE_S3_STS_ENDPOINT": endpoint,
                "LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE": str(_projected(issuer_url, sub, directory)),
                "LINEAGE_OUTBOX_URI": uri,
            }

        producer, relay = options(PRODUCER), options(RELAY)
        events = [
            {"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "run": {"runId": f"r{n}"}, "job": {"namespace": "j", "name": "n"}} for n in range(3)
        ]
        envelopes = [lineage_envelope(event) for event in events]
        for envelope in envelopes:
            outbox.stage_envelope(uri, producer, envelope)
        python_listed = list(outbox.list_events(uri, relay))
        rust_listed = _rust("list", env=rust_env(RELAY))
        check(
            [[key, body.decode()] for key, body in python_listed] == rust_listed,
            "the Rust lists what the Python staged, as the Python lists it",
            [key for key, _ in rust_listed],
        )
        for key, _ in python_listed:
            _rust("drop", key, env=rust_env(RELAY))
        check(list(outbox.list_events(uri, relay)) == [], "what the Rust dropped is gone for the Python", "dropped")

        staged = {envelope.id: json.dumps(envelope.as_wire()) for envelope in envelopes}
        for key, text in staged.items():
            _rust("stage", key, text, env=rust_env(PRODUCER))
        python_listed = list(outbox.list_events(uri, relay))
        check(
            sorted((key, body.decode()) for key, body in python_listed) == sorted(staged.items()),
            "the Python lists what the Rust staged",
            [key for key, _ in python_listed],
        )
        for key in staged:
            check(outbox.read_event(uri, relay, key) == staged[key].encode(), f"the Python reads {key} as the Rust staged it", "read")
            outbox.drop_event(uri, relay, key)
        check(_rust("list", env=rust_env(RELAY)) == [], "what the Python dropped is gone for the Rust", "dropped")

    if failures:
        print(f"\n{len(failures)} check(s) failed", flush=True)
        sys.exit(1)
    print("\nthe Python and the Rust outboxes agree", flush=True)


if __name__ == "__main__":
    command, *rest = sys.argv[1:]
    if command == "issuer":
        state = Path(rest[1])
        state.mkdir(parents=True, exist_ok=True)
        issuer(int(rest[0]), state)
    else:
        cross(*rest)
