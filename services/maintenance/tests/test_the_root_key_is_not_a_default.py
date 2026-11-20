"""No deployment gets the RustFS tenant root by forgetting to configure a credential.

A default `s3_access_key_id` naming the store's root key id (`rustfsadmin` on the development store) is the
tenant root. The chart always sets it, so no shipped deployment would rely on such a default, which is exactly
what makes it dangerous: nothing would catch its removal and nothing announces its use. A deployment that configures a scoped SECRET and
forgets the key silently pairs a scoped secret with the root key id; one that configures neither runs
the whole sweep as tenant root.

The secret half fails closed at the worker's start (`lakehouse.storage.store_session.require_env_pair`: "MAINTENANCE_S3_SECRET_ACCESS_KEY is
required"), and so does the key half: a credential is a PAIR — half a pair was measured giving
`SignatureDoesNotMatch` on every operation, which is the *better* outcome here.
The worse one is a complete, working, root-privileged pair nobody chose.

Fail-closed lives at BOOT, not in the model, so tests and tools may still build a settings object
without credentials — the same split the secret half already uses.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.secret_store_double import SecretStoreDouble

from maintenance import worker
from maintenance.core.config import MaintenanceSettings


def test_an_explicit_key_is_still_honoured() -> None:
    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="b", s3_access_key_id="rask-maintenance")
    assert settings.s3_access_key_id == "rask-maintenance"


async def _start(settings: MaintenanceSettings) -> bool:
    """The worker's start under ``settings``, bounded: a start that got past the store's identity would wait on the bus."""
    return await asyncio.wait_for(worker.run(settings, asyncio.Event()), timeout=20)


@pytest.mark.asyncio
async def test_boot_refuses_a_missing_key_the_way_it_refuses_a_missing_secret() -> None:
    """Both halves or neither, checked where the secret half is already checked."""
    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="b", s3_secret_access_key="scoped-secret")

    with pytest.raises(ValueError, match="MAINTENANCE_S3_ACCESS_KEY_ID is required"):
        await _start(settings)


@pytest.mark.asyncio
async def test_boot_refuses_a_whole_static_pair_nobody_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment's store identity is its ServiceAccount token; a whole key pair in the environment is a static key that
    never rotates, and a service signs with one only where an operator acknowledged it, as for authentication off."""
    monkeypatch.delenv("RASK_INSECURE_ALLOW_STATIC_STORE_KEY", raising=False)
    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="b", s3_access_key_id="rask-maintenance", s3_secret_access_key="scoped-secret")

    with pytest.raises(ValueError, match="RASK_INSECURE_ALLOW_STATIC_STORE_KEY"):
        await _start(settings)


@pytest.mark.asyncio
async def test_boot_refuses_an_acknowledged_pair_where_the_chart_named_a_secret_store(secret_store: SecretStoreDouble) -> None:
    """A service with a secret store is a deployment, and a deployment signs with its ServiceAccount token alone: the
    acknowledgement that lets the offline lanes sign with a pair does not lift that (ADR 0170)."""
    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="b", s3_access_key_id="rask-maintenance", s3_secret_access_key="scoped-secret")

    with pytest.raises(ValueError, match="signs with its ServiceAccount token alone: set MAINTENANCE_S3_WEB_IDENTITY_TOKEN_FILE"):
        await _start(settings)

    assert secret_store.login.call_count == 1
