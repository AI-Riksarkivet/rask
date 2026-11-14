"""[[LH-494]] The worker connects to the bus with the credential the store holds for its own user (ADR 0198 decision 5):
``nats-user-maintenance-worker``'s ``jwt`` and ``seed``, the two keys the dev store's seed writes for every bus user and
the chart's other readers take (its ExternalSecret for the store's own user, and each Dapr component's ``seedKey``). A
store that holds no JWT for it stops the worker before it connects: it fails closed rather than dial the bus without its
identity. Read from the secret store under the process's own login, as the deployed service reads it; the connection itself is
the bus lane's.
"""

from __future__ import annotations

import pytest
from tests.secret_store_double import SecretStoreDouble

from lakehouse.control_emit import NoopControlEmitter
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import tasks
from maintenance.services.queue import TaskQueue, creds_document


#: The worker's bus user, as the chart names its secret.
_BUS_USER = "nats-user-maintenance-worker"
#: A bus user's secret as the store holds it.
_STORED = {"jwt": "eyJ.the-users-jwt", "seed": "SUATHEUSERSSEED"}


class _Connected(Exception):  # noqa: N818 — a signal that stops the worker at its connection, not an error
    """Raised by the bus's stand-in, so the worker stops at its connection with what it connected with."""

    def __init__(self, credentials: str) -> None:
        super().__init__(credentials)
        self.credentials = credentials


async def _connect(url: str, credentials: str, *, name: str) -> TaskQueue:
    raise _Connected(credentials)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble) -> MaintenanceSettings:
    """The deployed service's settings, with a stand-in bus that stops the worker at its connection."""
    monkeypatch.setattr(TaskQueue, "connect", _connect)
    return MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": "nats://bus:4222"})


def test_a_bus_is_refused_for_a_process_with_no_secret_store_to_read_its_credential_from() -> None:
    """The worker's bus credential is read from the store alone, so a bus named without one would be a worker with no
    credential to present, or one from its environment."""
    with pytest.raises(ValueError, match="MAINTENANCE_NATS_URL needs a secret store"):
        MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": "nats://bus:4222"})


@pytest.mark.asyncio
async def test_the_worker_connects_with_the_bus_credential_its_store_holds(settings: MaintenanceSettings, secret_store: SecretStoreDouble) -> None:
    secret_store.hold(_BUS_USER, _STORED)

    with pytest.raises(_Connected) as connected:
        async with tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter()):
            pass

    assert connected.value.credentials == creds_document(jwt=_STORED["jwt"], seed=_STORED["seed"])


@pytest.mark.asyncio
async def test_the_worker_does_not_start_when_its_store_holds_no_jwt(settings: MaintenanceSettings, secret_store: SecretStoreDouble) -> None:
    secret_store.hold(_BUS_USER, {"seed": _STORED["seed"]})

    with pytest.raises(RuntimeError, match="no 'jwt'"):
        async with tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter()):
            pass
