"""No deployment gets the RustFS tenant root by forgetting to configure a credential.

`s3_access_key_id` defaulted to `minioadmin` — the tenant root. The chart always sets it, so no
shipped deployment relied on that default, which is exactly what made it dangerous: nothing would have
caught its removal and nothing announces its use. A deployment that configures a scoped SECRET and
forgets the key silently pairs a scoped secret with the root key id; one that configures neither runs
the whole sweep as tenant root.

The secret half already fails closed at boot (`service.py`: "MAINTENANCE_S3_SECRET_ACCESS_KEY is
required"). The key half did not, and a credential is a PAIR — the Ray plane's measured lesson is that
half a pair gives `SignatureDoesNotMatch` on every operation, which is the *better* outcome here.
The worse one is a complete, working, root-privileged pair nobody chose.

Fail-closed lives at BOOT, not in the model, so tests and tools may still build a settings object
without credentials — the same split the secret half already uses.
"""

from __future__ import annotations

import pytest

from maintenance.core.config import MaintenanceSettings


def test_an_explicit_key_is_still_honoured() -> None:
    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="b", MAINTENANCE_S3_ACCESS_KEY_ID="rask-maintenance")
    assert settings.s3_access_key_id == "rask-maintenance"


@pytest.mark.asyncio
async def test_boot_refuses_a_missing_key_the_way_it_refuses_a_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves or neither, checked where the secret half is already checked."""
    from fastapi import FastAPI

    from maintenance import service

    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="b", MAINTENANCE_S3_SECRET_ACCESS_KEY="scoped-secret")
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    # The refusal raises before the lifespan's teardown, so a drain armed here would stay armed for the
    # rest of the process and refuse the next lifespan that arms it.
    monkeypatch.setattr(service, "arm_drain_on_sigterm", lambda _app: lambda: None)

    with pytest.raises(RuntimeError, match="MAINTENANCE_S3_ACCESS_KEY_ID is required"):
        async with service.lifespan(FastAPI()):
            pass
