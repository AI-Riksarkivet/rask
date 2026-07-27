"""Unit tests for the compliance audit trail (``common.audit``)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from common.audit import ALLOW, AUDIT_LOGGER, DENY, audit, configure_audit


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


def test_audit_emits_a_structured_who_what_outcome_event(captured: list[logging.LogRecord]) -> None:
    configure_audit(enabled=True)
    audit("can_write_data", ALLOW, subject="alice", resource="table:db$t", request_id="r1")

    assert len(captured) == 1
    fields = {k: v for k, v in captured[0].__dict__.items() if k.startswith("audit.")}
    assert fields == {
        "audit.action": "can_write_data",
        "audit.outcome": "allow",
        "audit.subject": "alice",
        "audit.resource": "table:db$t",
        "audit.request_id": "r1",  # extra fields ride under the audit. prefix
    }


def test_missing_subject_and_resource_default_to_empty(captured: list[logging.LogRecord]) -> None:
    configure_audit(enabled=True)
    audit("authn", DENY)
    fields = captured[0].__dict__
    assert fields["audit.subject"] == "" and fields["audit.resource"] == ""


def test_configure_audit_disabled_drops_every_event(captured: list[logging.LogRecord]) -> None:
    # The whole trail is gated by one flag: disabled → nothing is emitted at all (dropped at the logger).
    configure_audit(enabled=False)
    audit("can_read_data", DENY, subject="bob", resource="table:x")
    audit("authn", "success", subject="alice")
    assert captured == []
