"""A service's telemetry reaches the platform's OTLP endpoint as the service itself.

The chart names the endpoint and the credential provider in the environment, and `opentelemetry-instrument` builds the
exporters from it. Each export then carries the pod's projected ServiceAccount token as a bearer, read when the export
is sent: the kubelet replaces the file at 80% of the token's lifetime, so a token read once would be refused within
minutes. No secret is configured; the platform's gateway verifies the token against the cluster's issuer.

The exporter is the SDK's own, built the way auto-configuration builds it, against a server that records what arrives.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


class _Gateway(BaseHTTPRequestHandler):
    seen: ClassVar[list[str | None]] = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@pytest.fixture
def gateway() -> Iterator[str]:
    _Gateway.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def test_every_export_carries_the_token_the_pod_holds_when_it_is_sent(gateway: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "otlp-token"
    token.write_text("first-projected-token\n")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", gateway)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "5")
    monkeypatch.setenv("OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER", "rask_service_account")
    monkeypatch.setenv("RASK_OTLP_TOKEN_FILE", str(token))
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
    tracer = provider.get_tracer(__name__)

    with tracer.start_as_current_span("before-rotation"):
        pass
    token.write_text("rotated-projected-token\n")
    with tracer.start_as_current_span("after-rotation"):
        pass
    provider.shutdown()

    assert _Gateway.seen == ["Bearer first-projected-token", "Bearer rotated-projected-token"]
