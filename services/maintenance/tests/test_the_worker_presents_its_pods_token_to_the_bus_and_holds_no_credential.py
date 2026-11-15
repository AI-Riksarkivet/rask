"""[[XC-199]] The worker holds no bus credential (ADR 0198 decision 5): its queue connection presents its pod's own token
for the bus, as `maintenance-worker`, and a bus named without that token is refused when the settings are read, so no
worker dials the bus with nothing to show for itself. What the bus does with the token (the session it grants, its
scope, its end) is the bus lane's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lakehouse.bus import BusSettings
from lakehouse.control_emit import NoopControlEmitter
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import tasks
from maintenance.services.queue import TaskQueue


class _Connected(Exception):  # noqa: N818 — a signal that stops the worker at its connection, not an error
    """Raised by the bus's stand-in, so the worker stops at its connection with what it connected with."""

    def __init__(self, token: str, name: str) -> None:
        super().__init__(name)
        self.token = token
        self.name = name


async def _connect(url: str, presenting: BusSettings, *, name: str) -> TaskQueue:
    raise _Connected(presenting.token(), name)


def test_a_bus_is_refused_for_a_worker_with_no_token_to_present_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RASK_BUS_TOKEN_FILE")

    with pytest.raises(ValueError, match="MAINTENANCE_NATS_URL needs the pod's token"):
        MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": "nats://bus:4222"})


@pytest.mark.asyncio
async def test_the_worker_connects_as_its_own_user_with_the_token_its_pod_holds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "bus-token"
    token.write_text("the-workers-pods-token\n")
    monkeypatch.setenv("RASK_BUS_TOKEN_FILE", str(token))
    monkeypatch.setattr(TaskQueue, "connect", _connect)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": "nats://bus:4222"})

    with pytest.raises(_Connected) as connected:
        async with tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter()):
            pass

    assert (connected.value.token, connected.value.name) == ("the-workers-pods-token", "maintenance-worker")
