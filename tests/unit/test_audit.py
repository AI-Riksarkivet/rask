"""Unit tests for the compliance audit trail (``lakehouse.governed.audit``)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import pytest

from lakehouse.context import continuing
from lakehouse.governed.audit import AUDIT_LOGGER, AuditAction, AuditOutcome, audit, configure_audit


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured() -> Iterator[list[logging.LogRecord]]:
    """Capture records emitted on the dedicated audit logger (independent of the OTLP root handler)."""
    handler = _Capture()
    logger = logging.getLogger(AUDIT_LOGGER)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        configure_audit(enabled=True)  # leave the stream on for the rest of the suite


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[tuple[dict[str, object], int]]]:
    """Reads the trail's own count of what it emitted: each series' attributes and its value."""
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

    from lakehouse.governed import audit as trail

    reader = InMemoryMetricReader()
    monkeypatch.setattr(trail, "_records", MeterProvider(metric_readers=[reader]).get_meter("lance.audit").create_counter("audit.records"))

    def read() -> list[tuple[dict[str, object], int]]:
        data = reader.get_metrics_data()
        scopes = [scope for resource in (data.resource_metrics if data is not None else []) for scope in resource.scope_metrics]
        points = [point for scope in scopes for metric in scope.metrics for point in metric.data.data_points]
        return [({**(point.attributes or {})}, int(point.value)) for point in points if isinstance(point, NumberDataPoint)]

    return read


#: The golden record of format 2.0: every field a compliance query may name, as one authorization verdict made under a
#: request carries it. A renamed, retyped or dropped field changes this dict, and so is a new ``audit.format`` (2.0 named
#: the request's trace, ``audit.trace_id``, where 1.0 named a request id the caller chose, ADR 0209).
_GOLDEN_2_0 = {
    "audit.format": "2.0",
    "audit.action": "authz",
    "audit.outcome": "allow",
    "audit.trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "audit.subject": "alice",
    "audit.resource": "table:db$t",
    "audit.relation": "can_write_data",
}


def test_audit_emits_the_golden_record_of_its_format(captured: list[logging.LogRecord], counted: Callable[[], list[tuple[dict[str, object], int]]]) -> None:
    """The record, and the trail's own count of it: the records ride the log path, which can lose one, so each is also
    counted on the metric path, and a store holding fewer than were counted has lost some ([[LH-585]])."""
    configure_audit(enabled=True)
    with continuing("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, subject="alice", resource="table:db$t", relation="can_write_data")

    assert [{k: v for k, v in r.__dict__.items() if k.startswith("audit.")} for r in captured] == [_GOLDEN_2_0]
    assert counted() == [({"action": "authz", "outcome": "allow"}, 1)]


def test_configure_audit_disabled_drops_every_event(captured: list[logging.LogRecord], counted: Callable[[], list[tuple[dict[str, object], int]]]) -> None:
    # The whole trail is gated by one flag: disabled → nothing is emitted at all (dropped at the logger), and nothing is
    # counted as emitted.
    configure_audit(enabled=False)
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, subject="bob", resource="table:x", relation="can_read_data")
    audit(AuditAction.AUTHN, AuditOutcome.SUCCESS, subject="alice")
    assert captured == []
    assert counted() == []
