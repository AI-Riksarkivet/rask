"""Each lakehouse process logs in to its secret store when it starts, before it reads a secret, serves or takes a task.

[[XC-199]] step D2. A pod with a wrong role, a token its role's audience refuses, or a store that never comes up must
not serve, and the process logs in to the store itself, so its own start is what fails closed and says why: the
catalog's, lineage's and maintenance's lifespans and the maintenance worker's start each ask the store for a login
before anything of theirs reads a secret. Each is started here as the chart starts it where a sidecar delivers to it,
its app token read from the store, against a store that never admits it: the start ends on the login, naming the role
the store refused, never on the first secret the process went on to read, and leaves no session behind.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine

import pytest
from fastapi import FastAPI

from catalog import main as catalog_main
from catalog.core.config import Settings
from lakehouse.governed import secrets
from lineage import main as lineage_main
from lineage.core.config import LineageSettings
from maintenance import service, worker
from maintenance.core.config import MaintenanceSettings
from tests.secret_store_double import SecretStoreDouble


async def _catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_main, "get_settings", lambda: Settings.model_validate({"control_relay_binding_name": "catalog-control-relay-cron"}))
    await catalog_main.lifespan(FastAPI()).__aenter__()


async def _lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lineage_main, "get_settings", lambda: LineageSettings.model_validate({"dapr_enabled": True}))
    await lineage_main.lifespan(FastAPI()).__aenter__()


async def _maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "get_settings", lambda: MaintenanceSettings.model_validate({"s3_bucket": "b", "dapr_enabled": True}))
    # The start fails before the lifespan's teardown, so a drain armed here would stay armed for the rest of the process.
    monkeypatch.setattr(service, "arm_drain_on_sigterm", lambda _app: lambda: None)
    await service.lifespan(FastAPI()).__aenter__()


async def _worker(_monkeypatch: pytest.MonkeyPatch) -> None:
    await worker.run(MaintenanceSettings.model_validate({"s3_bucket": "b", "nats_url": "nats://bus:4222"}), asyncio.Event())


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(_catalog, id="catalog"),
        pytest.param(_lineage, id="lineage"),
        pytest.param(_maintenance, id="maintenance"),
        pytest.param(_worker, id="maintenance-worker"),
    ],
)
def test_a_process_the_store_never_admits_ends_its_start_on_the_login_naming_its_role(
    monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble, start: Callable[[pytest.MonkeyPatch], Coroutine[None, None, None]]
) -> None:
    monkeypatch.setenv("RASK_APP_TOKEN_FROM_STORE", "true")
    # The start asks again inside its budget, as it does of a store still being seeded: here with no wait between.
    monkeypatch.setattr(secrets, "_LONGEST_BACKOFF_SECONDS", 0)
    secret_store.login.mock(return_value=secret_store.refused(400, "role not found"))

    with pytest.raises(RuntimeError, match=f"gave this process no usable login as its role '{secret_store.ROLE}'"):
        asyncio.run(asyncio.wait_for(start(monkeypatch), timeout=60))

    monkeypatch.delenv("RASK_SECRET_STORE_ADDR")
    assert (secret_store.login.call_count > 1, secrets.secret_store_configured()) == (True, False)
