"""The stage runner's FGA bootstrap events carry a service label a log query can match.

`build_fga_client(service=...)` stamps the label onto the structured `openfga_*` events, which
`service_kit.obs` raises to OTLP as the audit tier, so an operator filters boot diagnostics on it. It
is a value, not prose: every governed service passes a space-free name (`medallion-producer`,
`cascade-backfill`, `maintenance`, ...), and the stage runner's is `medallion-stage-runner`.

Driven through the real lifespan, because the label that matters is the one on the emitted record.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI

from medallion import stage_runner
from medallion.core.config import MedallionSettings
from service_kit.governed import fga as fga_mod


class _Sidecar:
    """The Dapr client the lifespan builds; the boot path only closes it."""

    async def close(self) -> None:
        return None


class _FgaClient:
    async def close(self) -> None:
        return None


async def _resolve(api_url: str, **_: object) -> tuple[str, str]:
    return "01STORE", "01MODEL"


def _boot() -> None:
    async def _drive() -> None:
        async with stage_runner.lifespan(FastAPI()):
            pass

    asyncio.run(_drive())


def test_the_stage_runners_fga_events_name_the_service(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # Unpinned, so the bootstrap resolves by name and emits `openfga_resolved_by_name`. Explicit values beat
    # the environment: a set RASK_FGA_STORE_ID/RASK_FGA_MODEL_ID takes the pinned path, which emits no such
    # event, and MEDALLION_RAY_ENABLED starts a real workflow runtime.
    settings = MedallionSettings.model_validate({"fga_enabled": True, "fga_store_id": None, "fga_model_id": None, "ray_enabled": False})
    monkeypatch.setattr(stage_runner, "get_settings", lambda: settings)
    monkeypatch.setattr(stage_runner, "DaprClient", _Sidecar)
    monkeypatch.setattr(stage_runner, "instrument_lance_if_available", lambda: None)
    monkeypatch.setattr(fga_mod, "resolve", _resolve)
    monkeypatch.setattr(fga_mod, "make_client", lambda *_a, **_k: _FgaClient())

    with caplog.at_level(logging.INFO, logger="service_kit.governed.auth_lifespan"):
        _boot()

    record = next((r for r in caplog.records if r.getMessage() == "openfga_resolved_by_name"), None)
    assert record is not None, "the boot emitted no openfga_resolved_by_name event, so this test never reached the FGA bootstrap"
    assert getattr(record, "service", None) == "medallion-stage-runner", (
        f"the stage runner labels its FGA events {getattr(record, 'service', None)!r}; a query for the service by its hyphenated name misses them"
    )
