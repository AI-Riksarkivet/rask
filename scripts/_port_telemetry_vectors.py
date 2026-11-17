"""The telemetry vectors of ``gen_port_vectors.py``: what the real Python instruments, audit trail, export identity,
settings and log lines answer, for the Rust port to answer the same (ADR 0237).

The real instruments (`lineage.core.metrics`, `lakehouse.bus_metrics`, `lakehouse.outbox_metrics`, `audit.records`)
are read through an in-memory reader on the process's meter provider, and a real app from the lance app factory under
the FastAPI instrumentation the chart turns on is read the same way, with its server spans. The real `audit` functions
emit through the SDK's logging handler into an in-memory exporter. The real `otlp_identity.session` sends through a
capturing adapter. The real exporters, reader and resource read a corpus of environments. Times and trace ids are
fixed, so the file is the same on every run.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch


#: The trace a traced case runs under, and the instant every line is stamped with (2026-11-17 10:00:00.123 UTC).
_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
_CREATED = 1794909600.123
#: The environment the chart renders for a lakehouse service (`lance.otelEnv`), less what each case changes.
_CHART = {
    "OTEL_SERVICE_NAME": "lineage",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector.observability:4318",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER": "rask_service_account",
    "RASK_OTLP_TOKEN_FILE": "/var/run/secrets/rask/identity/rask-otlp/token",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_METRIC_EXPORT_INTERVAL": "5000",
    "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=rask,deployment.environment.name=rask,service.version=0.1.0",
}


@contextmanager
def _environment(values: Mapping[str, str]) -> Generator[None]:
    kept = {name: value for name, value in os.environ.items() if not name.startswith(("OTEL_", "RASK_"))}
    with patch.dict(os.environ, kept | dict(values), clear=True):
        yield


def _kind(data: object) -> str:
    from opentelemetry.sdk.metrics.export import Gauge, Histogram, Sum

    match data:
        case Sum(is_monotonic=True):
            return "counter"
        case Sum():
            return "updowncounter"
        case Histogram():
            return "histogram"
        case Gauge():
            return "gauge"
    raise TypeError(type(data).__name__)


def _snapshot(reader: Any, scopes: set[str], *, values: bool = True) -> list[dict[str, Any]]:  # noqa: ANN401 - the SDK's reader
    """Every metric of ``scopes`` the reader holds: its name, unit, kind, buckets and points, sorted by name."""
    out = []
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            if scope.scope.name not in scopes:
                continue
            for metric in scope.metrics:
                kind = _kind(metric.data)
                entry: dict[str, Any] = {"scope": scope.scope.name, "name": metric.name, "unit": metric.unit, "kind": kind}
                points = []
                for point in metric.data.data_points:
                    recorded: dict[str, Any] = {"attributes": dict(sorted(point.attributes.items()))}
                    if kind == "histogram":
                        entry["bounds"] = list(point.explicit_bounds)
                        recorded |= {"count": point.count} | ({"sum": point.sum, "buckets": list(point.bucket_counts)} if values else {})
                    else:
                        recorded["value"] = point.value
                    points.append(recorded)
                entry["points"] = sorted(points, key=lambda point: json.dumps(point, sort_keys=True))
                out.append(entry)
    return sorted(out, key=lambda entry: entry["name"])


def _metric_steps(reader: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - the SDK's reader
    """The instruments of lineage, the bus and the outbox, driven through a corpus with a snapshot after each phase."""
    from lakehouse import bus_metrics, outbox_metrics
    from lakehouse.bus import _MESSAGING, _PROCESSING
    from lineage.core import metrics as lineage_metrics

    lineage = lineage_metrics
    steps: list[dict[str, Any]] = [
        {"op": "lineage.create_outcome_series", "args": []},
        {"op": "lineage.create_signature_refused_series", "args": []},
        {"op": "lineage.record_outcome", "args": ["ingested", "subscriber"]},
        {"op": "lineage.record_outcome", "args": ["ingested", "subscriber"]},
        {"op": "lineage.record_outcome", "args": ["refused", "http"]},
        {"op": "lineage.record_outcome", "args": ["dead_lettered", "dead_letter"]},
        {"op": "lineage.record_outcome", "args": ["signature_refused", "outbox"]},
        {"op": "lineage.record_ingest_duration", "args": [0.003]},
        {"op": "lineage.record_ingest_duration", "args": [0.2]},
        {"op": "lineage.record_ingest_duration", "args": [12.0]},
        {"op": "lineage.record_forwarded", "args": [3, 1, False]},
        {"op": "lineage.record_forwarded", "args": [0, 0, True]},
        {"op": "lineage.record_signature_verified", "args": ["rask-sa-catalog"]},
        {"op": "lineage.record_signature_refused", "args": ["kid"]},
        {"op": "collect"},
        {"op": "bus.create_delivery_series", "args": ["lineage-events", ["taken", "again", "parked"]]},
        {"op": "bus.record_delivery", "args": ["lineage-events", "taken"]},
        {"op": "bus.record_delivery", "args": ["lineage-events", "parked"]},
        {"op": "bus.record_pull_failed", "args": ["lineage-events"]},
        {"op": "bus.record_processing", "args": ["lineage.events.v1", "lineage-events", 0.04]},
        {"op": "bus.record_processing", "args": ["catalog.control.v1", None, 0.3]},
        {"op": "bus.record_publish", "args": ["lineage.events.v1", 0.02, None]},
        {"op": "bus.record_publish", "args": ["lineage.events.v1", 6.0, "TimeoutError"]},
        {"op": "bus.record_refusal", "args": ["lineage.events.v1", "oversize"]},
        {"op": "collect"},
        {"op": "outbox.create_series", "args": ["lineage"]},
        {"op": "outbox.record_staged", "args": ["lineage"]},
        {"op": "outbox.record_published", "args": ["lineage"]},
        {"op": "outbox.record_publish_failed", "args": ["control"]},
        {"op": "outbox.record_stage_failed", "args": ["control"]},
        {"op": "outbox.record_drop_failed", "args": ["lineage"]},
        {"op": "outbox.record_poison_dropped", "args": ["lineage"]},
        {"op": "outbox.record_drained", "args": ["lineage", 4]},
        {"op": "outbox.record_stranded", "args": ["lineage", 0]},
        {"op": "outbox.record_refused", "args": ["control", 2]},
        {"op": "collect"},
        {"op": "outbox.observe_backlog", "args": ["lineage", 3, 12.5]},
        {"op": "collect"},
        {"op": "outbox.forget_backlog", "args": ["lineage"]},
        {"op": "outbox.observe_backlog", "args": ["control", 0, 0.0]},
        {"op": "collect"},
    ]
    calls = {
        "lineage.create_outcome_series": lineage.create_outcome_series,
        "lineage.create_signature_refused_series": lineage.create_signature_refused_series,
        "lineage.record_outcome": lambda outcome, door: lineage.record_outcome(lineage.Outcome(outcome), door=lineage.Door(door)),
        "lineage.record_ingest_duration": lineage.record_ingest_duration,
        "lineage.record_forwarded": lambda forwarded, refused, stalled: lineage.record_forwarded(forwarded=forwarded, refused=refused, stalled=stalled),
        "lineage.record_signature_verified": lineage.record_signature_verified,
        "lineage.record_signature_refused": lineage.record_signature_refused,
        "bus.create_delivery_series": bus_metrics.create_delivery_series,
        "bus.record_delivery": bus_metrics.record_delivery,
        "bus.record_pull_failed": bus_metrics.record_pull_failed,
        "bus.record_processing": lambda subject, consumer, seconds: bus_metrics.record_processing(
            {**_PROCESSING, "messaging.destination.name": subject} | ({"messaging.consumer.group.name": consumer} if consumer else {}), seconds
        ),
        "bus.record_publish": lambda topic, seconds, failure: bus_metrics.record_publish(
            {**_MESSAGING, "messaging.destination.name": topic}, seconds, failure=failure
        ),
        "bus.record_refusal": bus_metrics.record_refusal,
    }
    for name in ("create_series", "record_staged", "record_published", "record_publish_failed", "record_stage_failed", "record_drop_failed"):
        calls[f"outbox.{name}"] = getattr(outbox_metrics, name)
    for name in ("record_poison_dropped", "record_drained", "record_stranded", "record_refused", "observe_backlog", "forget_backlog"):
        calls[f"outbox.{name}"] = getattr(outbox_metrics, name)
    for step in steps:
        if step["op"] == "collect":
            step["snapshot"] = _snapshot(reader, {"lance.lineage", "lance.bus", "lance.outbox"})
        else:
            calls[step["op"]](*step["args"])
    return steps


def _http_vectors(reader: Any) -> dict[str, list[Any]]:  # noqa: ANN401 - the SDK's reader
    """A real app from the factory under the FastAPI instrumentation as the chart sets it, put a corpus of requests."""
    from fastapi import APIRouter, FastAPI, Request
    from fastapi.testclient import TestClient
    from opentelemetry import trace
    from opentelemetry.instrumentation import fastapi as fastapi_instrumentation
    from opentelemetry.instrumentation._semconv import _OpenTelemetrySemanticConventionStability
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.util.http import get_excluded_urls

    from lakehouse.lance_app import build_lance_service_app
    from lakehouse.schemas.health import Readiness, ReadinessStatus

    doors = APIRouter()

    @doors.get("/v1/things/{name}")
    async def thing(name: str) -> dict[str, str]:
        if name == "boom":
            raise RuntimeError("boom")
        return {"name": name}

    @doors.post("/v1/things/{name}")
    async def put_thing(name: str) -> dict[str, str]:
        return {"name": name}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield

    async def ready(_: Request) -> Readiness:
        return Readiness(status=ReadinessStatus.ready)

    chart = {"OTEL_SEMCONV_STABILITY_OPT_IN": "http", "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS": "/livez,/readyz,/healthz,/api/health,/metrics"}
    requests: list[dict[str, Any]] = [
        {"method": "GET", "path": "/v1/things/a", "headers": {}},
        {"method": "GET", "path": "/v1/things/b?x=1", "headers": {"traceparent": _TRACEPARENT, "user-agent": "probe/1"}},
        {"method": "GET", "path": "/v1/things/boom", "headers": {}},
        {"method": "POST", "path": "/v1/things/a", "headers": {"content-type": "application/json"}, "body": "{}"},
        {"method": "DELETE", "path": "/v1/things/a", "headers": {}},
        {"method": "GET", "path": "/nowhere", "headers": {}},
        {"method": "GET", "path": "/livez", "headers": {}},
        {"method": "GET", "path": "/readyz", "headers": {}},
    ]
    spans = InMemorySpanExporter()
    with _environment(chart), patch.object(fastapi_instrumentation, "_excluded_urls_from_env", get_excluded_urls("FASTAPI")):
        # Both are read once per process, at import and at the first instrumentation; the chart sets them before either.
        _OpenTelemetrySemanticConventionStability._initialized = False
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        provider.add_span_processor(SimpleSpanProcessor(spans))
        app: FastAPI = build_lance_service_app(
            title="telemetry",
            docs_enabled=False,
            audit_enabled=False,
            lifespan=lifespan,
            log=logging.getLogger("telemetry"),
            ready_check=ready,
            max_body_bytes=1024,
            request_deadline_seconds=5.0,
            routers=[doors],
        )
        with TestClient(app, raise_server_exceptions=False, base_url="http://lineage.test") as client:
            for sent in requests:
                client.request(sent["method"], sent["path"], headers=sent["headers"], content=sent.get("body"))
    _OpenTelemetrySemanticConventionStability._initialized = False
    finished = [span for span in spans.get_finished_spans() if span.kind is trace.SpanKind.SERVER]
    recorded = [
        {
            "name": span.name,
            "kind": "server",
            "attributes": sorted(span.attributes or {}),
            "status": span.status.status_code.name.lower(),
            "trace_id": format(span.context.trace_id, "032x") if span.parent and span.parent.is_remote else None,
        }
        for span in finished
    ]
    return {"requests": requests, "spans": recorded, "metrics": _snapshot(reader, {"opentelemetry.instrumentation.fastapi"}, values=False)}


class _Fixed(logging.Filter):
    """Stamp every record with one instant, so a line is the same on every run."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.created = _CREATED
        record.msecs = 123.0
        return True


def _line_handler() -> tuple[logging.Handler, io.StringIO]:
    from lakehouse.context import CorrelationFilter, DiagnosticFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(_Fixed())
    handler.addFilter(CorrelationFilter())
    formatter = DiagnosticFormatter("%(asctime)s %(levelname)-7s %(name)s [%(trace_id)s] — %(message)s")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    return handler, stream


def _audit_vectors(reader: Any) -> dict[str, list[Any]]:  # noqa: ANN401 - the SDK's reader
    """The real trail through the SDK's logging handler: each case's records and lines, and what `audit.records` counted."""
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

    from lakehouse.context import continuing
    from lakehouse.governed import audit

    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = logging.getLogger(audit.AUDIT_LOGGER)
    lines, stream = _line_handler()
    exporting = LoggingHandler(logger_provider=provider)
    logger.addHandler(exporting)
    logger.addHandler(lines)
    logger.propagate = False
    cases: list[dict[str, Any]] = [
        {
            "enabled": True,
            "call": "audit",
            "action": "authz",
            "outcome": "allow",
            "subject": "user:ada",
            "resource": "table:wh$a$b",
            "fields": {"relation": "can_read"},
        },
        {"enabled": True, "call": "audit", "action": "authn", "outcome": "deny", "subject": None, "resource": None, "fields": {"reason": "expired"}},
        {
            "enabled": True,
            "call": "audit",
            "action": "dlq_replay",
            "outcome": "success",
            "subject": "user:bo",
            "resource": "run:1",
            "fields": {"count": 3, "dry": False},
        },
        {"enabled": True, "call": "audit", "action": "authz", "outcome": "failure", "subject": "s", "resource": "r", "fields": {}, "traceparent": _TRACEPARENT},
        {"enabled": True, "call": "audit_read", "subject": "user:ada", "resource": "table:wh$a$b", "version": 7, "columns": ["text", "id"], "fields": {}},
        {"enabled": True, "call": "audit_read", "subject": "user:ada", "resource": "table:wh$a$b", "version": None, "columns": None, "fields": {"rows": 10}},
        {"enabled": False, "call": "audit", "action": "authz", "outcome": "deny", "subject": "user:eve", "resource": "t", "fields": {}},
    ]
    for case in cases:
        audit.configure_audit(enabled=case["enabled"])
        exporter.clear()
        stream.truncate(0)
        stream.seek(0)
        with continuing(case.get("traceparent")):
            if case["call"] == "audit":
                audit.audit(
                    audit.AuditAction(case["action"]), audit.AuditOutcome(case["outcome"]), subject=case["subject"], resource=case["resource"], **case["fields"]
                )
            else:
                audit.audit_read(subject=case["subject"], resource=case["resource"], version=case["version"], columns=case["columns"], **case["fields"])
        case["records"] = [
            {
                "scope": record.instrumentation_scope.name if record.instrumentation_scope else None,
                "body": record.log_record.body,
                "severity_number": record.log_record.severity_number.value if record.log_record.severity_number else None,
                "severity_text": record.log_record.severity_text,
                "attributes": {key: value for key, value in (record.log_record.attributes or {}).items() if not key.startswith("code.")},
            }
            for record in exporter.get_finished_logs()
        ]
        case["lines"] = stream.getvalue().splitlines()
    logger.removeHandler(exporting)
    logger.removeHandler(lines)
    logger.propagate = True
    audit.configure_audit(enabled=False)
    return {
        "cases": cases,
        "counted": _snapshot(reader, {"lance.audit"}),
        "actions": [action.value for action in audit.AuditAction],
        "outcomes": [outcome.value for outcome in audit.AuditOutcome],
        "constants": [{"format": audit.AUDIT_FORMAT, "scope": audit.AUDIT_LOGGER, "all_columns": audit.ALL_COLUMNS}],
    }


def _identity_vectors() -> list[dict[str, Any]]:
    """What the real `session` presents for a token file's contents, a rotated file, a missing one, and no file named."""
    import requests
    from requests.adapters import BaseAdapter

    from lakehouse import otlp_identity

    class Capture(BaseAdapter):
        def __init__(self, seen: list[str | None]) -> None:
            super().__init__()
            self._seen = seen

        def send(self, request: requests.PreparedRequest, *_args: object, **_kwargs: object) -> requests.Response:
            presented = request.headers.get("Authorization")
            self._seen.append(presented if isinstance(presented, str) else None)
            answer = requests.Response()
            answer.status_code = 200
            answer._content = b""
            return answer

        def close(self) -> None:
            pass

    cases: list[dict[str, Any]] = [
        {"name": "a token", "files": ["fixture-token-1"]},
        {"name": "a token with the kubelet's whitespace", "files": ["  fixture-token-1\n"]},
        {"name": "a rotated token is presented on the next export", "files": ["fixture-token-1", "fixture-token-2"]},
        {"name": "an empty file", "files": [""]},
        {"name": "a file of whitespace", "files": [" \n\t"]},
        {"name": "a missing file", "files": [None]},
        {"name": "a token that is read again after a missing file", "files": [None, "fixture-token-3"]},
        {"name": "no token file named", "files": ["fixture-token-1"], "unnamed": True},
    ]
    with TemporaryDirectory() as directory:
        path = Path(directory) / "token"
        for case in cases:
            env = {} if case.get("unnamed") else {"RASK_OTLP_TOKEN_FILE": str(path)}
            with _environment(env):
                try:
                    sender = otlp_identity.session()
                except RuntimeError:
                    case["exports"] = "no session"
                    continue
            seen: list[str | None] = []
            sender.mount("http://", Capture(seen))
            exports = []
            for contents in case["files"]:
                path.unlink(missing_ok=True)
                if contents is not None:
                    path.write_text(contents)
                try:
                    sender.post("http://gateway.test/v1/traces", data=b"x")
                    exports.append({"sent": True, "authorization": seen[-1]})
                except Exception:
                    exports.append({"sent": False})
            case["exports"] = exports
    return cases


def _settings_vectors() -> list[dict[str, Any]]:
    """What the real exporters, metric reader and resource read from a corpus of environments."""
    from opentelemetry.distro import OpenTelemetryDistro
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._configuration import _get_exporter_names
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    envs: list[dict[str, Any]] = [
        {"name": "the chart's", "env": _CHART},
        {"name": "an endpoint with a trailing slash", "env": _CHART | {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://gateway.platform:4318/"}},
        {"name": "an endpoint under a path", "env": _CHART | {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://gateway.platform/otlp"}},
        {"name": "a signal's own endpoint", "env": _CHART | {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://logs.platform/ingest"}},
        {"name": "logs turned off", "env": _CHART | {"OTEL_LOGS_EXPORTER": "none"}},
        {"name": "traces turned off in capitals", "env": _CHART | {"OTEL_TRACES_EXPORTER": " NONE "}},
        {"name": "exporters left to the default", "env": {k: v for k, v in _CHART.items() if not k.endswith("_EXPORTER")}},
        {"name": "a timeout", "env": _CHART | {"OTEL_EXPORTER_OTLP_TIMEOUT": "2.5"}},
        {"name": "a timeout that is no number", "env": _CHART | {"OTEL_EXPORTER_OTLP_TIMEOUT": "soon"}},
        {"name": "an interval that is no number", "env": _CHART | {"OTEL_METRIC_EXPORT_INTERVAL": "often"}},
        {"name": "the default interval", "env": {k: v for k, v in _CHART.items() if k != "OTEL_METRIC_EXPORT_INTERVAL"}},
        {
            "name": "resource attributes with spaces, escapes, a pair with no value and service.name",
            "env": _CHART | {"OTEL_RESOURCE_ATTRIBUTES": " a = b ,c=d%20e,broken,x=1=2,service.name=other", "OTEL_SERVICE_NAME": "lineage"},
        },
        {
            "name": "service.name from the attributes alone",
            "env": {k: v for k, v in _CHART.items() if k != "OTEL_SERVICE_NAME"} | {"OTEL_RESOURCE_ATTRIBUTES": "service.name=lineage"},
        },
        {"name": "the gRPC protocol", "env": _CHART | {"OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}},
        {"name": "no token file named", "env": {k: v for k, v in _CHART.items() if k != "RASK_OTLP_TOKEN_FILE"}},
        {"name": "no endpoint", "env": {k: v for k, v in _CHART.items() if k != "OTEL_EXPORTER_OTLP_ENDPOINT"}},
    ]
    for case in envs:
        with _environment(case["env"]):
            OpenTelemetryDistro()._configure()
            read: dict[str, Any] = {
                "exporters": {signal: _get_exporter_names(signal) for signal in ("traces", "metrics", "logs")},  # type: ignore[arg-type]
                "protocol": os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"],
            }
            resource = Resource.create().attributes
            read["resource"] = {key: value for key, value in resource.items() if not key.startswith("telemetry.sdk.") and key != "service.instance.id"}
            try:
                spans, metrics, logs = OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()
            except RuntimeError:
                read["exporting"] = "refused"
            else:
                read["exporting"] = {"traces": spans._endpoint, "metrics": metrics._endpoint, "logs": logs._endpoint}
                read["timeout"] = spans._client._timeout
                read["interval_ms"] = PeriodicExportingMetricReader(metrics)._export_interval_millis
                for exporter in (spans, metrics, logs):
                    exporter.shutdown()
        case["read"] = read
    return envs


def _level_vectors() -> list[dict[str, Any]]:
    """What the real `setup_logging` sets the root logger to for each `RASK_LOG_LEVEL`."""
    from lakehouse.app import setup_logging

    root = logging.getLogger()
    kept = root.level
    cases: list[dict[str, Any]] = []
    for raw in (None, "INFO", "debug", "Warning", "WARN", "ERROR", "CRITICAL", "FATAL", "NOTSET", "10", "verbose", ""):
        with _environment({} if raw is None else {"RASK_LOG_LEVEL": raw}):
            try:
                setup_logging()
                cases.append({"raw": raw, "level": logging.getLevelName(root.level)})
            except ValueError:
                cases.append({"raw": raw, "level": None})
    root.handlers = [handler for handler in root.handlers if handler.get_name() != "rask-stdout"]
    root.setLevel(kept)
    return cases


def _line_vectors() -> list[dict[str, Any]]:
    """The real handler's line for a corpus of records: logger, level, message, diagnostics and trace."""
    from lakehouse.context import continuing

    handler, stream = _line_handler()
    cases: list[dict[str, Any]] = [
        {"name": "lineage.relay", "level": "INFO", "message": "lineage_relay_tick", "fields": {}},
        {"name": "lineage.relay", "level": "WARNING", "message": "lineage_outbox_event_stranded", "fields": {"key": "a/b.json", "error": "timed out"}},
        {
            "name": "lakehouse.bus",
            "level": "ERROR",
            "message": "bus_pull_failed",
            "fields": {"consumer": "lineage-events", "attempt": 3},
            "traceparent": _TRACEPARENT,
        },
        {"name": "lineage.store", "level": "DEBUG", "message": "store_read", "fields": {"seconds": 0.25, "whole": 2.0, "ok": True, "missing": False}},
        {
            "name": "lineage.doors",
            "level": "INFO",
            "message": "quoted",
            "fields": {"single": "it's", "double": 'say "hi"', "both": 'it\'s "x"', "line": "a\nb\tc"},
        },
        {"name": "lineage.doors", "level": "INFO", "message": "unicode", "fields": {"text": "åäö ✓", "small": 1e-05, "large": 1e17, "negative": -3}},
    ]
    for case in cases:
        logger = logging.getLogger(case["name"])
        logger.addHandler(handler)
        logger.propagate = False
        kept = logger.level
        logger.setLevel(logging.DEBUG)
        stream.truncate(0)
        stream.seek(0)
        with continuing(case.get("traceparent")):
            logger.log(logging.getLevelNamesMapping()[case["level"]], case["message"], extra=case["fields"])
        case["line"] = stream.getvalue().rstrip("\n")
        logger.removeHandler(handler)
        logger.propagate = True
        logger.setLevel(kept)
    return cases


def telemetry_vectors() -> dict[str, list[Any]]:
    """Every telemetry group, read through one in-memory reader on the process's meter provider."""
    from opentelemetry import metrics
    from opentelemetry.metrics._internal import _ProxyMeterProvider
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    if not isinstance(metrics.get_meter_provider(), _ProxyMeterProvider):
        raise RuntimeError("the process's meter provider was set before the telemetry vectors could read it")
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    # Another generator may have muted logging process-wide; the trail and the lines are read with every level on.
    muted = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        return {
            "metric_steps": _metric_steps(reader),
            "http": [_http_vectors(reader)],
            "audit": [_audit_vectors(reader)],
            "identity": _identity_vectors(),
            "settings": _settings_vectors(),
            "levels": _level_vectors(),
            "lines": _line_vectors(),
        }
    finally:
        logging.disable(muted)
