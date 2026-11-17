#!/usr/bin/env python
"""The Python half of ``scripts/port_lineage_check.sh``: lineage's Rust BINARY put to the checks ADR 0238 names, beside
the real Python lineage service, on real collaborators.

    uv run --no-sync scripts/port_lineage_fixture.py check <work>

The shell script starts the real Python catalog, the pinned OpenFGA, the identity provider the requests' tokens are
signed by, the pinned RustFS with the chart's lineage policy and the stand-in ServiceAccount issuer it trusts, and the
pinned nats-server, and exports where each is (``CHECK_*``). This process adds, as threads of its own, a proxy in front
of the catalog that can be told to fail or slow the feed's writes or to go away, a stand-in secret store answering the
store's API (login, ``auth/token/lookup-self``, KV v2 reads) and holding the signer's published keys, an OTLP/HTTP sink
that decodes every export, and a graph backend for the forwarder. It then starts the Rust binary (``lineage serve``)
and the Python service (``opentelemetry-instrument uvicorn lineage.main:app``, as the chart runs it) from one
environment shaped like the chart's (``environment``), and runs:

(a) the doors' side-by-side corpus (``scripts/port_doors_fixture.py``) with signing enforced and the bus present, plus
    signed staged events, a wrong signer and a tampered signature, and the bus republish on a replay;
(b) the lanes' nine black-box cases against the binary, OpenFGA deciding;
(c) the start refused for each bad setting, by both; ``/readyz`` before the start completes, after it, with the catalog
    away, and while draining;
(d) SIGTERM during an ingest and during a delivery;
(e) a handling that panics taking the process down non-zero;
(f) the same traffic through each service, and the sets of metric names, audit actions and span names each exported,
    every export presenting the pod's token;
(g) two replicas of the binary on one relay timer: the one that lost the window stops reporting the backlog the other
    drained.

Every check prints ``PASS``/``FAIL`` with its counts; the process exits non-zero when any fails.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import http.server
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "services"))

import port_doors_fixture as doors  # noqa: E402 - the scripts directory is put on the path first


SIGNER = "kubernetes~system:serviceaccount:rask:rask-sa-catalog"
ROGUE = "kubernetes~system:serviceaccount:rask:rask-sa-rogue"
#: A signer that is no delegator: it signs as itself, so what it may record is asked of OpenFGA, never attested.
MAINTENANCE = "kubernetes~system:serviceaccount:rask:rask-sa-maintenance"
STREAM, CONSUMER, TOPIC = "LINEAGE", "lineage-ingest", "lineage.events.v1"
OTLP_TOKEN = "otlp-pod-token"  # noqa: S105 - a fixture's token, protecting nothing
RESULTS: list[tuple[str, bool, str]] = []
#: Every service this process started, killed as it ends however it ends.
RUNNING: list[subprocess.Popen[bytes]] = []
#: What the Rust answers on purpose where the Python answers otherwise, beyond the doors' own (ADR 0236).
doors.KEPT["replay of a staged event with a signer nobody lists"] = "a refusal's message quotes nothing from the event (ADR 0223)"
#: How a stopped service may exit: uvicorn raises the SIGTERM it caught again once it has drained, so Python ends by it.
STOPPED = {"python": (0, -signal.SIGTERM), "rust": (0,)}


def verdict(name: str, passed: bool, detail: str = "") -> bool:
    RESULTS.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)
    return passed


def eventually(holds: Callable[[], bool], seconds: float = 30.0, every: float = 0.25) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if holds():
            return True
        time.sleep(every)
    return holds()


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


class _Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 - the stdlib's signature
        pass

    def answer(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))


class Proxy:
    """The catalog behind a switch: away (503 to everything), or failing or slowing the feed's writes."""

    FEED_WRITE = re.compile(r"(%24|\$)events/merge_insert")

    def __init__(self, upstream: str) -> None:
        import httpx

        self.away = False
        self.fail_feed = 0
        self.delay_feed = 0.0
        self.feed_writes = 0
        client = httpx.Client(base_url=upstream, timeout=120)
        proxy = self

        class Handler(_Quiet):
            def relay(self) -> None:
                if proxy.away:
                    self.answer(503, b'{"error": "the catalog is away", "code": 20}')
                    return
                body = self.body()
                if proxy.FEED_WRITE.search(self.path):
                    proxy.feed_writes += 1
                    if proxy.fail_feed > 0:
                        proxy.fail_feed -= 1
                        self.answer(503, b'{"error": "failed", "code": 21}')
                        return
                    time.sleep(proxy.delay_feed)
                headers = {name: value for name, value in self.headers.items() if name.lower() in ("content-type", "accept", "authorization")}
                answered = client.request(self.command, self.path, content=body, headers=headers)
                self.answer(answered.status_code, answered.content, answered.headers.get("content-type", "application/json"))

            do_GET = do_POST = do_PUT = do_DELETE = relay

        self.url = _serve(Handler)


class SecretStore:
    """The secret store's API as lineage speaks it, with the fields of each secret held here."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, str]] = {}
        self.away = False
        store = self
        issued: set[str] = set()

        class Handler(_Quiet):
            def do_POST(self) -> None:  # the stdlib's name
                self.body()
                if store.away or not self.path.endswith("/login"):
                    self.answer(503, b"{}")
                    return
                token = f"hvs.{uuid.uuid4().hex}"
                issued.add(token)
                self.answer(200, json.dumps({"auth": {"client_token": token, "lease_duration": 3600, "renewable": True}}).encode())

            def do_GET(self) -> None:  # the stdlib's name
                if store.away or self.headers.get("X-Vault-Token") not in issued:
                    self.answer(403, b'{"errors": ["permission denied"]}')
                elif self.path.endswith("/auth/token/lookup-self"):
                    self.answer(200, json.dumps({"data": {"ttl": 3600}}).encode())
                elif (name := self.path.rpartition("/data/")[2]) in store.secrets:
                    self.answer(200, json.dumps({"data": {"data": store.secrets[name]}}).encode())
                else:
                    self.answer(404, b'{"errors": []}')

        self.url = _serve(Handler)


class Sink:
    """An OTLP/HTTP receiver: every export decoded, with the service that sent it and whether it presented the token."""

    def __init__(self) -> None:
        self.exports: list[dict[str, Any]] = []
        sink = self

        class Handler(_Quiet):
            def do_POST(self) -> None:  # the stdlib's name
                body = self.body()
                presented = self.headers.get("Authorization") == f"Bearer {OTLP_TOKEN}"
                with contextlib.suppress(Exception):
                    sink.exports.append({"presented": presented, "at": time.monotonic(), **_decoded(self.path, body)})
                self.answer(200, b"", "application/x-protobuf")

        self.url = _serve(Handler)

    def names(self, service: str, kind: str) -> set[str]:
        return {name for export in self.exports if export.get("service") == service for name in export.get(kind, [])}

    def unpresented(self, service: str) -> int:
        return sum(1 for export in self.exports if export.get("service") == service and not export["presented"])


def _decoded(path: str, body: bytes) -> dict[str, Any]:
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    def service(resource: Any, key: str = "service.name") -> str:  # noqa: ANN401 - a protobuf message
        return next((kv.value.string_value for kv in resource.attributes if kv.key == key), "")

    if path.endswith("/v1/metrics"):
        metrics = ExportMetricsServiceRequest.FromString(body)
        found = [(service(rm.resource), m) for rm in metrics.resource_metrics for sm in rm.scope_metrics for m in sm.metrics]
        processed, depth = [], None
        for _, metric in found:
            if metric.name == "outbox.depth":
                lanes = {
                    kv.value.string_value: point.as_double for point in metric.gauge.data_points for kv in point.attributes if kv.key == "lance.outbox.lane"
                }
                depth = lanes.get("lineage", depth)
            if metric.name != "lineage.events.processed":
                continue
            for point in metric.sum.data_points:
                labels = {kv.key: kv.value.string_value for kv in point.attributes}
                if point.as_int > 0:
                    processed.append(f"{labels.get('lance.lineage.door')}:{labels.get('lance.lineage.outcome')}")
        instance = service(metrics.resource_metrics[0].resource, "service.instance.id") if metrics.resource_metrics else ""
        return {
            "service": found[0][0] if found else "",
            "instance": instance,
            "metrics": [metric.name for _, metric in found],
            "processed": processed,
            "depth": depth,
        }
    if path.endswith("/v1/traces"):
        traces = ExportTraceServiceRequest.FromString(body)
        found = [(service(rs.resource), s.name) for rs in traces.resource_spans for ss in rs.scope_spans for s in ss.spans]
        return {"service": found[0][0] if found else "", "spans": [name for _, name in found]}
    logs = ExportLogsServiceRequest.FromString(body)
    audits, services = [], set()
    for rl in logs.resource_logs:
        services.add(service(rl.resource))
        for sl in rl.scope_logs:
            if sl.scope.name != "lance.audit":
                continue
            for record in sl.log_records:
                attributes = {kv.key: kv.value.string_value for kv in record.attributes}
                audits.append(f"{attributes.get('audit.action')}:{attributes.get('audit.outcome')}")
    return {"service": next(iter(services), ""), "audits": audits}


class Backend:
    """The graph backend the forwarder POSTs to: every run it took, in order."""

    def __init__(self) -> None:
        self.runs: list[str] = []
        backend = self

        class Handler(_Quiet):
            def do_POST(self) -> None:  # the stdlib's name
                event = json.loads(self.body() or b"{}")
                backend.runs.append(((event.get("run") or {}).get("runId")) or "")
                self.answer(200, b"{}")

        self.url = _serve(Handler)


class Service:
    """One lineage process, Rust or Python, started from the environment and the server flags the chart gives it."""

    def __init__(self, kind: str, port: int, env: dict[str, str], work: Path, grace: int = 30) -> None:
        self.kind, self.port, self.url = kind, port, f"http://127.0.0.1:{port}"
        flags = ["--host=127.0.0.1", f"--port={port}", f"--timeout-graceful-shutdown={grace}"]
        if kind == "rust":
            argv = [os.environ["CHECK_RUST_BIN"], "serve", *flags]
        else:
            venv = ROOT / ".venv" / "bin"
            argv = [str(venv / "opentelemetry-instrument"), str(venv / "uvicorn"), "lineage.main:app", *flags]
        self.log = work / f"{kind}-{port}-{uuid.uuid4().hex[:6]}.log"
        environment = {"PATH": os.environ["PATH"], "HOME": str(work), **env, "OTEL_SERVICE_NAME": f"lineage-{kind}"}
        self.process = subprocess.Popen(argv, cwd=ROOT, env=environment, stdout=self.log.open("w"), stderr=subprocess.STDOUT)  # noqa: S603 - argv is the fixture's own
        RUNNING.append(self.process)

    def ready(self, seconds: float = 150.0) -> bool:
        import httpx

        def answers() -> bool:
            with contextlib.suppress(httpx.HTTPError):
                return httpx.get(f"{self.url}/readyz", timeout=2).status_code == 200
            return False

        return eventually(lambda: answers() or self.process.poll() is not None, seconds) and self.process.poll() is None

    def stop(self, seconds: float = 60.0) -> int | None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            return self.process.wait(seconds)
        self.process.kill()
        return None

    def said(self) -> str:
        return self.log.read_text(errors="replace")


def _port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def environment(work: Path, proxy: Proxy, secrets: SecretStore, sink: Sink, provenance: str, setup: dict[str, Any]) -> dict[str, str]:
    """The environment the chart renders for the lineage Deployment (`chart/templates/services.yaml`), pointed at this
    run's collaborators. Two departures, each because the fixture is not the estate: authorization is OpenFGA's own API
    at the pinned store and model, since the catalog fixture runs with authorization off and so serves no decision
    door; and no ServiceAccount verifier is configured, since no request here is signed by the cluster's issuer."""
    env = os.environ
    files = {"bus": env["CHECK_NATS_TOKEN"], "catalog": "catalog-token", "secret": "lineage-store-token", "otlp": OTLP_TOKEN, "fga": env["CHECK_FGA_KEY"]}
    for name, content in files.items():
        (work / f"{name}-token").write_text(content + "\n")
    store_token = work / "object-store-token"
    import httpx

    store_token.write_text(httpx.get(f"{env['CHECK_STORE_ISSUER']}/token", params={"sub": "system:serviceaccount:rask:rask-sa-lineage"}, timeout=10).text)
    return {
        "MALLOC_ARENA_MAX": "2",
        "ARROW_DEFAULT_MEMORY_POOL": "system",
        "OTEL_EXPORTER_OTLP_ENDPOINT": sink.url,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER": "rask_service_account",
        "RASK_OTLP_TOKEN_FILE": str(work / "otlp-token"),
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRIC_EXPORT_INTERVAL": "2000",
        "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED": "true",
        "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS": "/livez,/readyz,/healthz,/api/health,/metrics",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "http",
        "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=check,service.namespace=rask",
        "RASK_BUS_URL": env["CHECK_NATS_URL"],
        "RASK_BUS_USER": "lineage",
        "RASK_BUS_TOKEN_FILE": str(work / "bus-token"),
        "RASK_BUS_SESSION_SECONDS": "300",
        "LINEAGE_CATALOG_URL": proxy.url,
        "RASK_CATALOG_IDENTITY_TOKEN_FILE": str(work / "catalog-token"),
        "LINEAGE_PROVENANCE_NAMESPACE": provenance,
        "LINEAGE_CATALOG_DATASET_NAMESPACE": doors.CATALOG,
        "RASK_SECRET_STORE_ADDR": secrets.url,
        "RASK_SECRET_STORE_ROLE": "lineage",
        "RASK_SECRET_STORE_AUTH_MOUNT": "kubernetes",
        "RASK_SECRET_STORE_KV_MOUNT": "secret",
        "RASK_SECRET_STORE_TOKEN_FILE": str(work / "secret-token"),
        "LINEAGE_DOCS": "false",
        "LINEAGE_MAX_BODY_BYTES": "65536",
        "LINEAGE_REQUEST_DEADLINE_SECONDS": "30",
        "LINEAGE_READ_ONLY": "false",
        "LINEAGE_EVENTS_RETENTION_DAYS": "7",
        "LINEAGE_EVENTS_TOPIC": TOPIC,
        "LINEAGE_DELIVERIES_IN_FLIGHT": "8",
        "LINEAGE_DELIVERY_ATTEMPTS": "5",
        "LINEAGE_DELIVERY_RETRY_SECONDS": "1",
        "LINEAGE_DELIVERY_ACK_WAIT_SECONDS": "30",
        "LINEAGE_S3_ENDPOINT": env["CHECK_STORE_ENDPOINT"],
        "LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE": str(store_token),
        "LINEAGE_S3_STS_ENDPOINT": env["CHECK_STORE_ENDPOINT"],
        "LINEAGE_S3_ASSUME_ROLE_ARN": "arn:aws:iam::000000000000:role/rask-lineage",
        "LINEAGE_S3_SESSION_SECONDS": "900",
        "LINEAGE_OUTBOX_URI": f"s3://{env['CHECK_BUCKET']}/{doors.OUTBOX}",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": env["CHECK_ISSUER"],
        "RASK_OIDC_AUDIENCE": doors.AUDIENCE,
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_IDP_ID": "idp",
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": env["CHECK_FGA_URL"],
        "RASK_FGA_STORE_ID": setup["store"],
        "RASK_FGA_MODEL_ID": setup["model"],
        "RASK_FGA_TOKEN_FILE": str(work / "fga-token"),
        "RASK_FGA_ROOT_OBJECT": doors.ROOT,
        "LANCE_AUDIT_ENABLED": "true",
        "LINEAGE_SIGNERS": json.dumps([SIGNER, MAINTENANCE]),
        "LINEAGE_DELEGATORS": json.dumps([SIGNER]),
    }


def _key(name: str) -> Any:  # noqa: ANN401 - the kit's SigningKey
    from lakehouse.lineage.signing import _SEED_PREFIX, SigningKey, _encode_nkey

    return SigningKey.from_seed(_encode_nkey(_SEED_PREFIX, hashlib.sha256(f"rask-port-lineage-check:{name}".encode()).digest()))


def signed(event: dict[str, Any], identity: str = SIGNER, key: str = "catalog", on_behalf_of: str | None = None) -> dict[str, Any]:
    from lakehouse.lineage.signing import attach_signature

    return attach_signature(event, key=_key(key), identity=identity, on_behalf_of=on_behalf_of)


def self_signed(event: dict[str, Any]) -> dict[str, Any]:
    """``event`` stamped for, and signed by, the signer that is no delegator."""
    copy = json.loads(json.dumps(event))
    copy["run"]["facets"]["rask_author"] = {"sub": MAINTENANCE}
    return signed(copy, identity=MAINTENANCE, key="maintenance")


def tampered(event: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(event))
    holder = copy["run"] if "run" in copy else copy["dataset"]
    value = holder["facets"]["rask_signature"]["signature"]
    holder["facets"]["rask_signature"]["signature"] = ("A" if value[0] != "A" else "B") + value[1:]
    return copy


class Estate:
    """The collaborators this process holds, and what is staged and published on them."""

    def __init__(self, work: Path) -> None:
        self.work = work
        env = os.environ
        self.proxy = Proxy(env["CHECK_CATALOG_URL"])
        self.secrets = SecretStore()
        self.sink = Sink()
        self.backend = Backend()
        signer = _key("catalog")
        self.secrets.secrets["signing-public-rask-sa-catalog"] = {"keys": signer.public_nkey}
        self.secrets.secrets["signing-public-rask-sa-rogue"] = {"keys": _key("rogue").public_nkey}
        self.secrets.secrets["signing-public-rask-sa-maintenance"] = {"keys": _key("maintenance").public_nkey}
        self.setup = self._setup()
        self.producer = self._producer_options()

    def _setup(self) -> dict[str, Any]:
        env = os.environ
        doors.setup(self.work, env["CHECK_CATALOG_URL"], env["CHECK_FGA_URL"], env["CHECK_FGA_KEY"])
        setup = json.loads((self.work / "setup.json").read_text())
        doors.UUIDS.update(setup["uuids"])
        asyncio.run(self._declare_the_stream())
        return setup

    async def _declare_the_stream(self) -> None:
        import nats
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, StreamConfig

        client = await nats.connect(os.environ["CHECK_NATS_URL"], token=os.environ["CHECK_NATS_TOKEN"])
        stream = client.jetstream()
        with contextlib.suppress(Exception):
            await stream.delete_stream(STREAM)
        await stream.add_stream(StreamConfig(name=STREAM, subjects=["lineage.events.>"], duplicate_window=120, allow_direct=True))
        consumer = ConsumerConfig(
            durable_name=CONSUMER,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=30,
            max_deliver=-1,
            filter_subject=TOPIC,
            deliver_policy=DeliverPolicy.ALL,
            max_ack_pending=8,
        )
        await stream.add_consumer(STREAM, consumer)
        await client.close()

    def _producer_options(self) -> Any:  # noqa: ANN401 - the kit's StorageOptions
        import httpx

        from lakehouse.storage_options import StorageOptions
        from lakehouse.store_session import WebIdentity, exchange

        env = os.environ
        token = self.work / "producer-token"
        token.write_text(httpx.get(f"{env['CHECK_STORE_ISSUER']}/token", params={"sub": doors.PRODUCER}, timeout=10).text)
        identity = WebIdentity(token_file=token, endpoint=env["CHECK_STORE_ENDPOINT"], region="us-east-1", role_arn="arn:aws:iam::000000000000:role/rask")
        pair = exchange(identity, session_name="lineage-check", duration_seconds=900).key_pair
        return StorageOptions(key_pair=pair, options={}).with_endpoint(env["CHECK_STORE_ENDPOINT"])

    def environment(self, provenance: str, **overrides: str) -> dict[str, str]:
        return environment(self.work, self.proxy, self.secrets, self.sink, provenance, self.setup) | overrides

    def stage(self, event: dict[str, Any]) -> Any:  # noqa: ANN401 - the envelope
        from lakehouse import outbox

        envelope = doors._envelope(event)
        outbox.stage_envelope(f"s3://{os.environ['CHECK_BUCKET']}/{doors.OUTBOX}", self.producer, envelope)
        return envelope

    def publish(self, event: dict[str, Any], kind: str | None = None) -> str:
        """Publish ``event`` on the bus as the catalog's own publish does; its envelope id."""
        envelope = doors._envelope(event).as_wire()
        if kind is not None:
            envelope["type"] = kind
        asyncio.run(self._publish(envelope))
        return str(envelope["id"])

    async def _publish(self, envelope: dict[str, Any]) -> None:
        import nats

        client = await nats.connect(os.environ["CHECK_NATS_URL"], token=os.environ["CHECK_NATS_TOKEN"])
        await client.jetstream().publish(TOPIC, json.dumps(envelope).encode(), headers={"Nats-Msg-Id": envelope["id"]})
        await client.close()

    def published(self, since: int) -> list[dict[str, Any]]:
        """Every message the stream holds after sequence ``since``."""
        return asyncio.run(self._read(since))

    async def _read(self, since: int) -> list[dict[str, Any]]:
        import nats

        client = await nats.connect(os.environ["CHECK_NATS_URL"], token=os.environ["CHECK_NATS_TOKEN"])
        stream = client.jetstream()
        last = (await stream.stream_info(STREAM)).state.last_seq
        found = []
        for sequence in range(since + 1, last + 1):
            with contextlib.suppress(Exception):
                found.append(json.loads((await stream.get_msg(STREAM, sequence)).data or b"{}"))
        await client.close()
        return found

    def last_sequence(self) -> int:
        async def last() -> int:
            import nats

            client = await nats.connect(os.environ["CHECK_NATS_URL"], token=os.environ["CHECK_NATS_TOKEN"])
            sequence = (await client.jetstream().stream_info(STREAM)).state.last_seq
            await client.close()
            return int(sequence)

        return asyncio.run(last())

    def unsettled(self) -> int:
        async def pending() -> int:
            import nats

            client = await nats.connect(os.environ["CHECK_NATS_URL"], token=os.environ["CHECK_NATS_TOKEN"])
            info = await client.jetstream().consumer_info(STREAM, CONSUMER)
            await client.close()
            return int(info.num_ack_pending + info.num_pending)

        return asyncio.run(pending())

    def rows(self, provenance: str, table: str) -> list[dict[str, Any]]:
        from lakehouse.lancekit.reader import CatalogTableReader, RestCatalogTransport

        reader = CatalogTableReader(RestCatalogTransport(os.environ["CHECK_CATALOG_URL"], timeout=60), [*provenance.split("$"), table])
        return reader.to_table().to_pylist()

    def namespace(self, name: str) -> str:
        import httpx

        provenance = f"lance_catalog$port${name}{uuid.uuid4().hex[:6]}"
        created = httpx.post(f"{os.environ['CHECK_CATALOG_URL']}/v1/namespace/{provenance}/create", json={}, timeout=30)
        assert created.status_code in (200, 409), created.text
        return provenance


def _run(run_id: str, outputs: list[str], inputs: list[str] | None = None, author: str = "admin", **extra: Any) -> dict[str, Any]:  # noqa: ANN401 - facets
    return doors._run(run_id, outputs, inputs or [], author=author, stamped=True, **extra)


def check_doors(estate: Estate, provenance: str) -> None:
    """(a): the doors' corpus with signing enforced and the bus present, and the signed cases."""
    env = estate.environment(provenance)
    os.environ.update({name: value for name, value in env.items() if name.startswith("LINEAGE_")})
    python = Service("python", _port(), env, estate.work)
    rust = Service("rust", _port(), env, estate.work)
    if not (python.ready() and rust.ready()):
        verdict("(a) both services start", False, f"python: {python.said()[-800:]}\nrust: {rust.said()[-800:]}")
        return
    runs = doors._seed(os.environ["CHECK_STORE_ENDPOINT"], os.environ["CHECK_BUCKET"], os.environ["CHECK_STORE_ISSUER"], estate.work)
    corpus = doors.Corpus(python.url, rust.url, os.environ["CHECK_ISSUER"])
    doors.corpus(corpus, runs)
    signed_cases(estate, corpus)
    total = sum(corpus.by_operation.values())
    for line in corpus.differences:
        print(f"DIFF {line}")
    for line in corpus.kept:
        print(f"KEPT {line}")
    verdict("(a) doors side by side", not corpus.differences, f"{total} requests, {len(corpus.differences)} differences, {len(corpus.kept)} kept on purpose")
    codes = (python.stop(), rust.stop())
    clean = codes[0] in STOPPED["python"] and codes[1] in STOPPED["rust"]
    verdict("(a) both stop on SIGTERM, the Rust with 0", clean, f"python {codes[0]}, rust {codes[1]}")


def signed_cases(estate: Estate, corpus: Any) -> None:  # noqa: ANN401 - the doors' Corpus
    """Staged events signed by the listed signer, by a signer nobody lists, and with a tampered signature, replayed on each
    side; a replay of a signed event republished on the bus; a parked signed delivery re-presented; a signed HTTP ingest."""
    tag = uuid.uuid4().hex[:6]
    good = {side: f"sg-{side}-{tag}" for side in ("python", "rust")}
    for run in good.values():
        estate.stage(signed(_run(run, ["b"], ["a"]), on_behalf_of="idp~admin"))
    rogue = {side: f"sg-rogue-{side}-{tag}" for side in ("python", "rust")}
    for run in rogue.values():
        estate.stage(signed(_run(run, ["b"]), identity=ROGUE, key="rogue"))
    forged = {side: f"sg-forged-{side}-{tag}" for side in ("python", "rust")}
    for run in forged.values():
        estate.stage(tampered(signed(_run(run, ["b"]), on_behalf_of="idp~admin")))
    unsigned = {side: f"sg-unsigned-{side}-{tag}" for side in ("python", "rust")}
    for run in unsigned.values():
        estate.stage(_run(run, ["b"]))
    since = estate.last_sequence()
    corpus.paired(
        "replay of a signed staged event", "POST", (f"/admin/dlq/{good['python']}/replay", f"/admin/dlq/{good['rust']}/replay"), {good["python"]: good["rust"]}
    )
    for name, runs in (("a signer nobody lists", rogue), ("a tampered signature", forged), ("no signature", unsigned)):
        corpus.paired(
            f"replay of a staged event with {name}",
            "POST",
            (f"/admin/dlq/{runs['python']}/replay", f"/admin/dlq/{runs['rust']}/replay"),
            {runs["python"]: runs["rust"]},
        )
    republished = {((message.get("data") or {}).get("run") or {}).get("runId") for message in estate.published(since)}
    verdict("(a) a signed replay is republished on the bus by each", set(good.values()) <= republished, f"republished {sorted(r for r in republished if r)}")
    corpus.both("a signed ingest, its signature stripped", "POST", "/api/v1/lineage", "admin", signed(_run(f"sg-http-{tag}", ["a"]), on_behalf_of="idp~admin"))
    corpus.both("dlq after the signed replays", "GET", "/admin/dlq", "admin")


def check_lanes(estate: Estate) -> None:
    """(b): the lanes' nine cases against the binary, with the relay, the forwarder and OpenFGA deciding."""
    provenance = estate.namespace("lanes")
    env = estate.environment(
        provenance,
        LINEAGE_RELAY_PERIOD_SECONDS="2",
        LINEAGE_TIMER_ROOT=f"s3://{os.environ['CHECK_BUCKET']}",
        LINEAGE_FORWARD_URL=estate.backend.url,
        LINEAGE_FORWARD_ENDPOINT="api/v1/lineage",
    )
    rust = Service("rust", _port(), env, estate.work)
    if not verdict("(b) the binary starts with the relay and the forwarder", rust.ready(), rust.said()[-600:] if rust.process.poll() is not None else ""):
        return
    tag = uuid.uuid4().hex[:6]

    def feed(run: str) -> list[dict[str, Any]]:
        return [row for row in estate.rows(provenance, "events") if row["run_id"] == run]

    def whole(run: str) -> bool:
        bound = any(row["run_id"] == run for row in estate.rows(provenance, "runs"))
        edged = any(row["run_id"] == run for row in estate.rows(provenance, "edges"))
        return len(feed(run)) == 1 and bound and edged

    def parked(run: str) -> bool:
        return any(row["run_id"] == run for row in estate.rows(provenance, "refusals"))

    def staged_none() -> bool:
        return not [key for key in _listing(estate) if tag in key]

    # 1. Recorded by the lane, published by the producer, signed as the catalog signs.
    run = f"lane-{tag}"
    estate.publish(signed(_run(run, ["b"], ["a"]), on_behalf_of="idp~admin"))
    verdict("(b1) way: recorded by the lane", eventually(lambda: whole(run)), f"rows {len(feed(run))}")
    # 2. Refused and parked: a writer OpenFGA grants nothing on the output, signed for her.
    run = f"denied-{tag}"
    estate.publish(self_signed(_run(run, ["c"])))
    verdict("(b2) way: refused and parked", eventually(lambda: parked(run)) and not feed(run))
    # 3. The store failing mid-record: the feed row is not written, the delivery is asked for again, nothing doubles.
    run = f"midway-{tag}"
    estate.proxy.fail_feed = 2
    estate.publish(signed(_run(run, ["b"], ["a"]), on_behalf_of="idp~admin"))
    verdict("(b3) a store failing mid-record is retried whole", eventually(lambda: whole(run), 60) and estate.proxy.fail_feed == 0, f"rows {len(feed(run))}")
    # 4. Staged with the bus away: the relay records it, publishes it again, drops it.
    run = f"staged-{tag}"
    since = estate.last_sequence()
    estate.stage(signed(_run(run, ["b"], ["a"]), on_behalf_of="idp~admin"))
    drained = eventually(lambda: whole(run) and staged_none(), 60)
    again = any(((m.get("data") or {}).get("run") or {}).get("runId") == run for m in estate.published(since))
    verdict("(b4) a staged event is drained by the relay: recorded, published again, dropped", drained and again)
    # 5. The lane fails, then the relay records: one row, whole, nothing staged.
    run = f"lanefails-{tag}"
    estate.proxy.fail_feed = 1
    event = signed(_run(run, ["b"], ["a"]), on_behalf_of="idp~admin")
    estate.stage(event)
    estate.publish(event)
    verdict("(b5) way: the lane fails, then the relay records", eventually(lambda: whole(run) and staged_none(), 60), f"rows {len(feed(run))}")
    # 6. The relay refuses: kept as refused, then dropped.
    run = f"relayrefuses-{tag}"
    estate.stage(self_signed(_run(run, ["c"])))
    verdict("(b6) way: the relay refuses", eventually(lambda: parked(run) and staged_none(), 60) and not feed(run))
    # 7. Both race: the lane and the relay on one event.
    run = f"race-{tag}"
    event = signed(_run(run, ["b"], ["a"]), on_behalf_of="idp~admin")
    estate.stage(event)
    estate.publish(event)
    verdict("(b7) way: the lane and the relay race", eventually(lambda: whole(run) and staged_none(), 60), f"rows {len(feed(run))}")
    # 8. Two replicas' ticks in one window run once.
    second = Service("rust", _port(), env, estate.work)
    if second.ready():
        time.sleep(9)
        ticks = [(rust.said() + second.said()).count("lineage_relay_tick")]
        time.sleep(8)
        ticks.append((rust.said() + second.said()).count("lineage_relay_tick"))
        ran = ticks[1] - ticks[0]
        verdict("(b8) two replicas' ticks in one window run once", 3 <= ran <= 5, f"{ran} ticks in 8 s at a 2 s period, two replicas")
    else:
        verdict("(b8) two replicas' ticks in one window run once", False, "the second replica did not start")
    second.stop()
    # 9. The forwarder sends every recorded event once, and its cursor survives a restart.
    eventually(lambda: run in estate.backend.runs, 30)
    sent = Counter(estate.backend.runs)
    rust.stop()
    restarted = Service("rust", _port(), env, estate.work)
    restarted.ready()
    later = f"after-{tag}"
    estate.publish(signed(_run(later, ["b"], ["a"]), on_behalf_of="idp~admin"))
    eventually(lambda: later in estate.backend.runs, 60)
    resent = Counter(estate.backend.runs) - sent
    verdict("(b9) the forwarder sends each event once and its cursor survives a restart", resent == Counter([later]), f"after the restart: {dict(resent)}")
    restarted.stop()


def check_backlog_forgotten(estate: Estate) -> None:
    """(g): replica A reports a backlog it cannot record (the catalog fails every feed write); A is stopped, the catalog is
    let through and replica B drains the outbox and reads it empty; A resumes once B has claimed the window, early in it,
    and loses it. Until A next runs a window, A reports no backlog (the timer's ``elsewhere`` hook, `lakehouse.timers`),
    where one that kept its last reading would report the event B drained."""
    env = estate.environment(
        estate.namespace("backlog"),
        LINEAGE_RELAY_PERIOD_SECONDS="2",
        LINEAGE_TIMER_ROOT=f"s3://{os.environ['CHECK_BUCKET']}",
        OTEL_METRIC_EXPORT_INTERVAL="250",
    )
    name = "(g) a replica that lost the window stops reporting the backlog another drained"

    def depths(since: float, of: str | None = None) -> list[tuple[float, str, float | None]]:
        exported = [(e["at"], e["instance"], e["depth"]) for e in list(estate.sink.exports) if e.get("instance") and e["at"] > since and "metrics" in e]
        return [(at, i, d) for at, i, d in exported if of is None or i == of]

    a = Service("rust", _port(), env, estate.work)
    if not verdict("(g) replica A starts", a.ready()):
        return
    eventually(lambda: not _listing(estate), 60)
    estate.proxy.fail_feed = 10**6
    began = time.monotonic()
    estate.stage(signed(_run(f"backlog-{uuid.uuid4().hex[:6]}", ["b"], ["a"]), on_behalf_of="idp~admin"))
    reported = eventually(lambda: any(d is not None and d >= 1 for _, _, d in depths(began)), 60)
    a_instance = next((i for _, i, d in depths(began) if d is not None and d >= 1), "")
    b = Service("rust", _port(), env, estate.work)
    if not (reported and b.ready()):
        verdict(name, False, f"A reported the backlog: {reported}")
        a.stop()
        b.stop()
        return
    a.process.send_signal(signal.SIGSTOP)
    estate.proxy.fail_feed = 0
    stopped = time.monotonic()
    drained = eventually(lambda: not _listing(estate) and any(d == 0 for _, i, d in depths(stopped) if i != a_instance), 60)
    # Resume A once B has claimed the window it is early in, so A's first turn finds it held and A's next turn is a
    # second or more away: what A reports until then is what it knows of the outbox.
    claimed = eventually(lambda: abs(_claimed_window(estate) - (time.time() - time.time() % 2)) < 1e-3 and time.time() % 2 < 1, 30, 0.05)
    resumed = time.monotonic()
    a.process.send_signal(signal.SIGCONT)
    time.sleep(4)
    after = [(round(at - resumed, 2), d) for at, _, d in depths(resumed + 0.5, a_instance)]
    until_it_runs = after[: next((n for n, (_, d) in enumerate(after) if d == 0), len(after))]
    stale = [d for _, d in until_it_runs if d is not None]
    passed = drained and claimed and bool(until_it_runs) and not stale
    verdict(name, passed, f"B drained it: {drained}; A's exports after it resumed (s, depth), until it ran a window: {until_it_runs}")
    a.stop()
    b.stop()


def _claimed_window(estate: Estate) -> float:
    """The start of the relay's window the claim record names, in seconds since the epoch."""
    from datetime import datetime

    env = os.environ
    root = Path(env["CHECK_STORE_WORK"]) / "root"
    client = _root_client(root)
    record = json.loads(client.get_object(Bucket=env["CHECK_BUCKET"], Key="_timers/lineage-relay.json")["Body"].read())
    return datetime.fromisoformat(record["window"]).timestamp()


def _root_client(root: Path) -> Any:  # noqa: ANN401 - boto3's client
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["CHECK_STORE_ENDPOINT"],
        aws_access_key_id=(root / "access_key").read_text().strip(),
        aws_secret_access_key=(root / "secret_key").read_text().strip(),
        region_name="us-east-1",
    )


def _listing(estate: Estate) -> list[str]:
    env = os.environ
    client = _root_client(Path(env["CHECK_STORE_WORK"]) / "root")
    listed = client.list_objects_v2(Bucket=env["CHECK_BUCKET"], Prefix=f"{doors.OUTBOX}/")
    return [entry["Key"] for entry in listed.get("Contents", [])]


#: Each bad setting, and the variable the refusal names.
BAD_SETTINGS = [
    ("LINEAGE_DELIVERY_ATTEMPTS", "0"),
    ("LINEAGE_MAX_BODY_BYTES", "lots"),
    ("LINEAGE_CATALOG_TIMEOUT_SECONDS", "-1"),
    ("LINEAGE_FORWARD_BATCH", "0"),
    ("LINEAGE_CATALOG_DATASET_NAMESPACE", "http://catalog"),
    ("LINEAGE_RELAY_PERIOD_SECONDS", "5"),
    ("LINEAGE_SIGNERS", "not a list"),
    ("LINEAGE_READ_ONLY", "perhaps"),
    ("LINEAGE_DELIVERY_ACK_WAIT_SECONDS", "1"),
    ("RASK_OIDC_ENABLED", "false"),
]


def check_start(estate: Estate) -> None:
    """(c): every bad setting refuses the start, naming its variable; readiness before, after and while draining."""
    import httpx

    provenance = estate.namespace("start")
    env = estate.environment(provenance)
    refused, python_refused = 0, 0
    for name, value in BAD_SETTINGS:
        bad = env | {name: value}
        rust = Service("rust", _port(), bad, estate.work)
        try:
            code = rust.process.wait(30)
        except subprocess.TimeoutExpired:
            code = rust.stop()
        named = name in rust.said() or (name == "RASK_OIDC_ENABLED" and "RASK_OIDC_ENABLED" in rust.said())
        refused += int(code == 3 and named)
        if not (code == 3 and named):
            print(f"  rust started or named nothing for {name}={value!r}: exit {code}, {rust.said()[-300:]}")
        python = Service("python", _port(), bad, estate.work)
        try:
            python_code = python.process.wait(90)
        except subprocess.TimeoutExpired:
            python_code = None
            python.stop()
        python_refused += int(python_code not in (None, 0))
        if python_code in (None, 0):
            print(f"  python started with {name}={value!r}")
    verdict("(c) every bad setting refuses the Rust start, naming the variable", refused == len(BAD_SETTINGS), f"{refused} of {len(BAD_SETTINGS)}")
    verdict("(c) and the Python start", python_refused == len(BAD_SETTINGS), f"{python_refused} of {len(BAD_SETTINGS)}")

    def status(url: str) -> tuple[int, str]:
        with contextlib.suppress(httpx.HTTPError):
            answer = httpx.get(f"{url}/readyz", timeout=3)
            return answer.status_code, answer.json().get("status", "")
        return 0, "refused"

    estate.proxy.away = True
    rust = Service("rust", _port(), env, estate.work)
    eventually(lambda: status(rust.url)[0] != 0, 30)
    before = status(rust.url)
    estate.proxy.away = False
    started = rust.ready(150)
    after = status(rust.url)
    verdict(
        "(c) /readyz is 503 starting until the start completes, then 200", before == (503, "starting") and started and after[0] == 200, f"{before} then {after}"
    )
    estate.proxy.away = True
    away = eventually(lambda: status(rust.url) == (503, "unhealthy"), 40)
    estate.proxy.away = False
    back = eventually(lambda: status(rust.url)[0] == 200, 40)
    verdict("(c) /readyz is 503 while the catalog is away, and 200 once it answers", away and back)
    with httpx.Client(timeout=5) as held:
        held.get(f"{rust.url}/livez")
        rust.process.send_signal(signal.SIGTERM)
        draining = status_on(held, rust.url)
    code = rust.process.wait(60)
    verdict("(c) /readyz is 503 shutting down once SIGTERM lands", draining == (503, "shutting_down") and code == 0, f"{draining}, exit {code}")


def status_on(client: Any, url: str) -> tuple[int, str]:  # noqa: ANN401 - an httpx client
    import httpx

    with contextlib.suppress(httpx.HTTPError):
        answer = client.get(f"{url}/readyz")
        return answer.status_code, answer.json().get("status", "")
    return 0, "refused"


def check_drain(estate: Estate) -> None:
    """(d): SIGTERM during an ingest and during a delivery; (e): a handling that panics ends the process non-zero."""
    import httpx

    provenance = estate.namespace("drain")
    env = estate.environment(provenance)
    rust = Service("rust", _port(), env, estate.work, grace=20)
    if not verdict("(d) the binary starts", rust.ready()):
        return
    tag = uuid.uuid4().hex[:6]
    token = httpx.get(f"{os.environ['CHECK_ISSUER']}/token", params={"sub": "admin"}, timeout=10).text
    estate.proxy.delay_feed = 3.0
    answered: dict[str, Any] = {}
    run = f"slow-ingest-{tag}"

    def ingest() -> None:
        answer = httpx.post(f"{rust.url}/api/v1/lineage", json=_run(run, ["b"], ["a"]), headers={"authorization": f"Bearer {token}"}, timeout=60)
        answered["status"] = answer.status_code

    sending = threading.Thread(target=ingest)
    sending.start()
    time.sleep(1.0)
    began = time.monotonic()
    rust.process.send_signal(signal.SIGTERM)
    sending.join(60)
    code = rust.process.wait(60)
    took = time.monotonic() - began
    recorded = [row for row in estate.rows(provenance, "events") if row["run_id"] == run]
    verdict(
        "(d) SIGTERM during an ingest: answered, recorded once, exit 0 within the grace",
        answered.get("status") == 201 and len(recorded) == 1 and code == 0 and took < 20,
        f"status {answered.get('status')}, rows {len(recorded)}, exit {code} after {took:.1f} s",
    )
    rust = Service("rust", _port(), env, estate.work, grace=20)
    rust.ready()
    run = f"slow-delivery-{tag}"
    writes = estate.proxy.feed_writes
    estate.publish(signed(_run(run, ["b"], ["a"]), on_behalf_of="idp~admin"))
    eventually(lambda: estate.proxy.feed_writes > writes, 30, 0.05)
    began = time.monotonic()
    rust.process.send_signal(signal.SIGTERM)
    code = rust.process.wait(60)
    took = time.monotonic() - began
    estate.proxy.delay_feed = 0.0
    settled = estate.unsettled() == 0
    again = Service("rust", _port(), env, estate.work)
    again.ready()
    eventually(lambda: estate.unsettled() == 0, 30)
    recorded = [row for row in estate.rows(provenance, "events") if row["run_id"] == run]
    verdict(
        "(d) SIGTERM during a delivery: settled before the exit, recorded once, exit 0 within the grace",
        settled and len(recorded) == 1 and code == 0 and took < 20,
        f"settled at exit {settled}, rows {len(recorded)}, exit {code} after {took:.1f} s",
    )
    estate.publish(_run(f"panics-{tag}", ["b"]), kind="rask.check.handling-panics")
    with contextlib.suppress(subprocess.TimeoutExpired):
        again.process.wait(60)
    code = again.process.poll()
    verdict("(e) a handling that panics takes the process down non-zero", code == 70, f"exit {code}; said: {_line(again.said(), 'background_work_ended')}")
    if code is None:
        again.stop()
    # The panicking delivery is still on the consumer; a process that is not the debug check's takes it as unreadable.
    asyncio.run(_drop_the_panic())


async def _drop_the_panic() -> None:
    import nats

    client = await nats.connect(os.environ["CHECK_NATS_URL"], token=os.environ["CHECK_NATS_TOKEN"])
    stream = client.jetstream()
    await stream.delete_consumer(STREAM, CONSUMER)
    await stream.purge_stream(STREAM)
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

    consumer = ConsumerConfig(
        durable_name=CONSUMER,
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=30,
        max_deliver=-1,
        filter_subject=TOPIC,
        deliver_policy=DeliverPolicy.ALL,
        max_ack_pending=8,
    )
    await stream.add_consumer(STREAM, consumer)
    await client.close()


def _line(said: str, event: str) -> str:
    return next((line for line in said.splitlines() if event in line), "")[:300]


def traffic(estate: Estate, service: Service, tag: str) -> None:
    """The same traffic through either service: each door, a refusal, a replay, a delivery, a relay tick, a forward."""
    import httpx

    def bearer(who: str) -> dict[str, str]:
        return {"authorization": f"Bearer {httpx.get(f'{os.environ["CHECK_ISSUER"]}/token', params={'sub': who}, timeout=10).text}"}

    client = httpx.Client(base_url=service.url, timeout=60)
    client.post("/api/v1/lineage", json=_run(f"t-ok-{tag}", ["b"], ["a"]), headers=bearer("admin"))
    client.post("/api/v1/lineage", json=_run(f"t-no-{tag}", ["c"]), headers=bearer("ada"))
    client.post("/api/v1/lineage", json=_run(f"t-anon-{tag}", ["c"]))
    for path in ("/events?limit=5", "/events/projection?limit=5", f"/tables/{doors.table('a')}/derived", "/admin/dlq", "/admin/dlq/parked"):
        client.get(path, headers=bearer("admin"))
        client.get(path, headers=bearer("ada"))
    estate.stage(signed(_run(f"t-replay-{tag}", ["b"], ["a"]), on_behalf_of="idp~admin"))
    client.post(f"/admin/dlq/t-replay-{tag}/replay", headers=bearer("admin"))
    client.post(f"/admin/dlq/parked/t-none-{tag}/replay", headers=bearer("admin"))
    estate.publish(signed(_run(f"t-bus-{tag}", ["b"], ["a"]), on_behalf_of="idp~admin"))
    estate.publish(self_signed(_run(f"t-bus-no-{tag}", ["c"])))
    estate.publish(tampered(signed(_run(f"t-bus-forged-{tag}", ["b"]), on_behalf_of="idp~admin")))
    estate.stage(signed(_run(f"t-staged-{tag}", ["b"], ["a"]), on_behalf_of="idp~admin"))
    estate.stage(self_signed(_run(f"t-staged-no-{tag}", ["c"])))
    time.sleep(8)


#: What the Python alone exports, kept on purpose (ADR 0237, ADR 0238): the client instrumentations' metrics and spans
#: (httpx, botocore, the OpenFGA SDK), the ASGI layer's inner `http send` and `http receive` spans, and the thread pool a
#: process that runs its blocking work on its own threads measures, which the Rust has none of.
ONLY_PYTHON: dict[str, Callable[[str], bool]] = {
    "metrics": lambda name: name.startswith(("http.client.", "fga-client.", "threadpool.")),
    "spans": lambda name: name.endswith((" http send", " http receive")) or name in ("GET", "POST") or name.startswith(("S3.", "STS.")),
}


def check_telemetry(estate: Estate) -> None:
    """(f): the same traffic through each service alone, and what each exported: metric names, audit actions, spans."""
    provenance = estate.namespace("telemetry")
    env = estate.environment(
        provenance,
        LINEAGE_RELAY_PERIOD_SECONDS="2",
        LINEAGE_TIMER_ROOT=f"s3://{os.environ['CHECK_BUCKET']}",
        LINEAGE_FORWARD_URL=estate.backend.url,
        LINEAGE_FORWARD_ENDPOINT="api/v1/lineage",
    )
    estate.sink.exports.clear()
    for kind in ("python", "rust"):
        service = Service(kind, _port(), env, estate.work)
        if not service.ready():
            verdict(f"(f) {kind} starts", False, service.said()[-500:])
            return
        traffic(estate, service, uuid.uuid4().hex[:6])
        service.stop()
    for kind in ("python", "rust"):
        verdict(f"(f) every {kind} export presents the pod's token", estate.sink.unpresented(f"lineage-{kind}") == 0)
    sets = {kind: {what: estate.sink.names(f"lineage-{kind}", what) for what in ("metrics", "spans")} for kind in ("python", "rust")}
    audits = {kind: {name for e in estate.sink.exports if e.get("service") == f"lineage-{kind}" for name in e.get("audits", [])} for kind in ("python", "rust")}
    for what, allowed in ONLY_PYTHON.items():
        python, rust = sets["python"][what], sets["rust"][what]
        unexplained = {name for name in python - rust if not allowed(name)}
        print(f"  {what}: both {len(python & rust)}; python only {sorted(python - rust)}; rust only {sorted(rust - python)}")
        verdict(
            f"(f) the {what}: every name the Rust exports the Python exports, and the Python's others are kept differences",
            bool(rust) and not unexplained and rust <= python,
            f"{len(rust)} rust, {len(python)} python, unexplained {sorted(unexplained)}",
        )
    counted = {kind: estate.sink.names(f"lineage-{kind}", "processed") for kind in ("python", "rust")}
    print(f"  lineage.events.processed counted at: python {sorted(counted['python'])}; rust {sorted(counted['rust'])}")
    verdict(
        "(f) each event is counted at the same doors with the same outcomes",
        bool(counted["rust"]) and counted["python"] == counted["rust"],
        f"{len(counted['rust'])} door:outcome pairs",
    )
    print(f"  audit: python only {sorted(audits['python'] - audits['rust'])}; rust only {sorted(audits['rust'] - audits['python'])}")
    verdict("(f) the audit actions and outcomes are the same set", audits["python"] == audits["rust"], f"{sorted(audits['rust'])}")


def main() -> None:
    command, work = sys.argv[1], Path(sys.argv[2])
    if command != "check":
        raise SystemExit(f"unknown command {command}")
    atexit.register(lambda: [process.kill() for process in RUNNING if process.poll() is None])
    estate = Estate(work)
    only = set(filter(None, os.environ.get("CHECK_ONLY", "").split(",")))
    for name, run in (
        ("a", lambda: check_doors(estate, doors.PROVENANCE)),
        ("b", lambda: check_lanes(estate)),
        ("c", lambda: check_start(estate)),
        ("d", lambda: check_drain(estate)),
        ("f", lambda: check_telemetry(estate)),
        ("g", lambda: check_backlog_forgotten(estate)),
    ):
        if not only or name in only:
            try:
                run()
            except Exception as exc:  # a check that raised is a failed check, and the others still run
                verdict(f"({name}) ran to its end", False, f"{type(exc).__name__}: {exc}")
    failed = [name for name, passed, _ in RESULTS if not passed]
    print(f"{len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
