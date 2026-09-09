"""Every lance-plane app arms the compliance trail, because the factory does it and not the service.

`governed/audit.py` gates the `lance.audit` stream by the dedicated logger's LEVEL, so a service that
never calls `configure_audit` leaves it at NOTSET and inherits whatever `RASK_LOG_LEVEL` set. § Q17-27
closed that for the `make_service_app` fleet; `build_lance_service_app` is the OTHER factory and it
serves the lakehouse — catalog, lineage, the medallion stage runners and maintenance.

Three of those four called `configure_audit` in their own lifespans and two did not, which is the same
convention-nobody-holds-up shape twice over. It matters most where it was missing: `services/maintenance`
is the component that REWRITES BYTES, and § Q17-36 records that its reclamation trail is telemetry
rather than a governed artifact — a gap that cannot even begin to close while the logger it would write
to is unarmed.

`audit_enabled` is REQUIRED here, exactly like `docs_enabled` beside it. A keyword with no default is
enforced by the type checker at every call site, which is a stronger gate than any test: a new
lakehouse service cannot forget it, it has to answer.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI

from service_kit.governed.audit import AUDIT_LOGGER
from service_kit.lance_app import build_lance_service_app


@pytest.fixture(autouse=True)
def _restore_loggers():
    log, root = logging.getLogger(AUDIT_LOGGER), logging.getLogger()
    before, root_before = log.level, root.level
    yield
    log.setLevel(before)
    root.setLevel(root_before)


def _build(*, audit_enabled: bool) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield

    return build_lance_service_app(
        title="t",
        docs_enabled=False,
        audit_enabled=audit_enabled,
        lifespan=_lifespan,
        log=logging.getLogger("t"),
    )


def test_the_flag_turns_the_trail_ON_regardless_of_the_app_log_level() -> None:
    """The volume lever must not be a compliance lever — this is the direction that loses evidence."""
    _build(audit_enabled=True)
    logging.getLogger().setLevel(logging.WARNING)
    assert logging.getLogger(AUDIT_LOGGER).isEnabledFor(logging.INFO), (
        "RASK_LOG_LEVEL=WARNING silenced the lakehouse audit stream, so raising the log level deletes the trail"
    )


def test_the_flag_turns_it_OFF_even_at_INFO() -> None:
    """A deployment that says no must get no trail — the flag decides, not the root logger."""
    logging.getLogger().setLevel(logging.INFO)
    _build(audit_enabled=False)
    assert not logging.getLogger(AUDIT_LOGGER).isEnabledFor(logging.INFO), "audit_enabled=False left the trail emitting, so the flag is decorative"
