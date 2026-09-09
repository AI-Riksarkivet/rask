"""The compliance trail must be DECIDED, not inherited from `RASK_LOG_LEVEL`.

`governed/audit.py` promises "audit is OFF unless explicitly on" — true only for a service that calls
`configure_audit`. Measured on the live estate 2026-09-09: of the ten services holding `audit()` call
sites, THREE call it (catalog, lineage, medallion) and seven do not, so their `lance.audit` logger sits
at NOTSET and inherits the root level `setup_logging` sets from `RASK_LOG_LEVEL` (default INFO). Both
directions of that are wrong and neither is visible: `RASK_AUDIT_ENABLED=false` cannot silence them, and
`RASK_LOG_LEVEL=WARNING` — documented one file away as "the volume lever" — silently deletes the
compliance trail of every one of them while the three explicit services keep theirs.

So the factory arms it, the way it already arms the readiness flags and the problem handlers for the
same reason: a convention five apps did not hold up is not a convention.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter

from service_kit import make_service_app
from service_kit.config import Settings
from service_kit.governed.audit import AUDIT_LOGGER


@pytest.fixture(autouse=True)
def _restore_audit_logger():
    """`configure_audit` mutates a process-global logger; leaving it set would leak across tests."""
    log = logging.getLogger(AUDIT_LOGGER)
    root = logging.getLogger()
    before, root_before = log.level, root.level
    yield
    log.setLevel(before)
    root.setLevel(root_before)


def _app(*, audit_enabled: bool):
    return make_service_app(title="t", routers=[APIRouter()], settings=Settings(audit_enabled=audit_enabled))


def test_the_flag_can_turn_the_trail_OFF_even_at_INFO() -> None:
    """A deployment that says no must get no trail — the flag, not the log level, decides."""
    logging.getLogger().setLevel(logging.INFO)
    _app(audit_enabled=False)
    assert not logging.getLogger(AUDIT_LOGGER).isEnabledFor(logging.INFO), (
        "audit_enabled=False left the trail emitting, so the flag is decorative and the log level is the real switch"
    )


def test_the_trail_SURVIVES_a_quieter_app_log_level() -> None:
    """The volume lever must not be a compliance lever — this is the direction that loses evidence."""
    _app(audit_enabled=True)
    logging.getLogger().setLevel(logging.WARNING)
    assert logging.getLogger(AUDIT_LOGGER).isEnabledFor(logging.INFO), (
        "RASK_LOG_LEVEL=WARNING silenced the audit stream, so raising the app's log level deletes the compliance trail"
    )
