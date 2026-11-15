"""Unit tests for the compliance audit trail (``lakehouse.governed.audit``)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

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


def test_audit_emits_the_golden_record_of_its_format(captured: list[logging.LogRecord]) -> None:
    configure_audit(enabled=True)
    with continuing("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, subject="alice", resource="table:db$t", relation="can_write_data")

    assert [{k: v for k, v in r.__dict__.items() if k.startswith("audit.")} for r in captured] == [_GOLDEN_2_0]


def test_configure_audit_disabled_drops_every_event(captured: list[logging.LogRecord]) -> None:
    # The whole trail is gated by one flag: disabled → nothing is emitted at all (dropped at the logger).
    configure_audit(enabled=False)
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, subject="bob", resource="table:x", relation="can_read_data")
    audit(AuditAction.AUTHN, AuditOutcome.SUCCESS, subject="alice")
    assert captured == []
