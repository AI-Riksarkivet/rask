"""[[LH-494]] The worker connects to the bus with the credential the store holds for its own user (ADR 0198 decision 5):
``nats-user-maintenance-worker``'s ``jwt`` and ``seed``, the two keys the dev store's seed writes for every bus user and
the chart's other readers take (its ExternalSecret for the store's own user, and each Dapr component's ``seedKey``). A
store that holds no JWT for it stops the worker before it connects: it fails closed rather than dial the bus without its
identity. Read through the sidecar's secrets API, as the deployed service reads it; the connection itself is the bus lane's.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from lakehouse.control_emit import NoopControlEmitter
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import tasks
from maintenance.services.queue import TaskQueue, creds_document


SIDECAR = "http://localhost:3500/v1.0/secrets/lance-secrets"
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
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MaintenanceSettings:
    """The deployed service's settings, with a stand-in bus that stops the worker at its connection."""
    monkeypatch.setattr(TaskQueue, "connect", _connect)
    # A service reading its secrets from the store presents its ServiceAccount token at the store, as the pod does.
    (token := tmp_path / "token").write_text("a projected token")
    monkeypatch.setenv("MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE", str(token))
    monkeypatch.setenv("MAINTENANCE_S3_STS_ENDPOINT", "http://store.example:9000")
    return MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": "nats://bus:4222", "secrets_from_dapr": True})


@pytest.mark.asyncio
@respx.mock
async def test_the_worker_connects_with_the_bus_credential_its_store_holds(settings: MaintenanceSettings) -> None:
    respx.get(f"{SIDECAR}/nats-user-maintenance-worker").mock(return_value=httpx.Response(200, json=_STORED))

    with pytest.raises(_Connected) as connected:
        async with tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter()):
            pass

    assert connected.value.credentials == creds_document(jwt=_STORED["jwt"], seed=_STORED["seed"])


@pytest.mark.asyncio
@respx.mock
async def test_the_worker_does_not_start_when_its_store_holds_no_jwt(settings: MaintenanceSettings) -> None:
    respx.get(f"{SIDECAR}/nats-user-maintenance-worker").mock(return_value=httpx.Response(200, json={"seed": _STORED["seed"]}))

    with pytest.raises(RuntimeError, match="secret 'jwt' unavailable"):
        async with tasks.running_worker(settings, ready=lambda: True, emitter=NoopEmitter(), control=NoopControlEmitter()):
            pass
