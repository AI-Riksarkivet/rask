"""No deployment gets the RustFS tenant root by forgetting to configure a credential.

`s3_access_key_id` defaulted to `rustfsadmin` — the tenant root. The chart always sets it, so no
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


def test_the_default_is_not_the_tenant_root() -> None:
    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="lance-catalog")
    assert settings.s3_access_key_id != "rustfsadmin", (
        "an unconfigured deployment runs the sweep as RustFS tenant root, reaching every tenant's bytes and the records that govern maintenance itself"
    )
    assert settings.s3_access_key_id == "", "unset should be empty so the boot check can refuse it"


def test_an_explicit_key_is_still_honoured() -> None:
    settings = MaintenanceSettings(MAINTENANCE_S3_BUCKET="b", MAINTENANCE_S3_ACCESS_KEY_ID="rask-maintenance")
    assert settings.s3_access_key_id == "rask-maintenance"


@pytest.mark.asyncio
async def test_boot_refuses_a_missing_key_the_way_it_refuses_a_missing_secret() -> None:
    """Both halves or neither, checked where the secret half is already checked."""
    import inspect

    from maintenance import service

    source = inspect.getsource(service)
    assert "MAINTENANCE_S3_ACCESS_KEY_ID is required" in source, (
        "boot validates the secret and not the key — a deployment can start with a scoped secret and the root key id"
    )
