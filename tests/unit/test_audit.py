"""Unit tests for the compliance audit trail (``service_kit.governed.audit``)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from service_kit.context import request_id_ctx
from service_kit.governed.audit import AUDIT_LOGGER, AuditAction, AuditOutcome, audit, configure_audit


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


#: The golden record of format 1.0: every field a compliance query may name, as one authorization verdict made under a
#: request carries it. A renamed, retyped or dropped field changes this dict, and so is a new ``audit.format``.
_GOLDEN_1_0 = {
    "audit.format": "1.0",
    "audit.action": "authz",
    "audit.outcome": "allow",
    "audit.request_id": "r1",
    "audit.subject": "alice",
    "audit.resource": "table:db$t",
    "audit.relation": "can_write_data",
}


def test_audit_emits_the_golden_record_of_its_format(captured: list[logging.LogRecord]) -> None:
    configure_audit(enabled=True)
    token = request_id_ctx.set("r1")
    try:
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, subject="alice", resource="table:db$t", relation="can_write_data")
    finally:
        request_id_ctx.reset(token)

    assert [{k: v for k, v in r.__dict__.items() if k.startswith("audit.")} for r in captured] == [_GOLDEN_1_0]


def test_configure_audit_disabled_drops_every_event(captured: list[logging.LogRecord]) -> None:
    # The whole trail is gated by one flag: disabled → nothing is emitted at all (dropped at the logger).
    configure_audit(enabled=False)
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, subject="bob", resource="table:x", relation="can_read_data")
    audit(AuditAction.AUTHN, AuditOutcome.SUCCESS, subject="alice")
    assert captured == []
