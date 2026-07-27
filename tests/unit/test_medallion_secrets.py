"""Medallion secret consumption from the Dapr secret store (Batch 7, 2026-07-11).

The movers/producer were the LAST real S3 consumers still shipping the key in plaintext pod env —
worse, the chart ALREADY omitted the env when the store was on, but nothing told the service to
consume the store, so a store-on deployment left medallion credential-less. This is the service
half, symmetric with catalog/lineage/compaction: flag off → env exactly as today (no fetch, no
Dapr dependency); flag on → the store is the STRICT sole source (store value wins over any env
residue; a miss FAILS CLOSED — an env fallback would contradict 'OpenBao is the sole source').
"""

from __future__ import annotations

from typing import Any

import pytest
from medallion.core.config import MedallionSettings, apply_dapr_secrets


def _settings(**overrides: Any) -> MedallionSettings:
    values: dict[str, Any] = {"MEDALLION_S3_SECRET_ACCESS_KEY": "env-value"}
    values.update(overrides)
    return MedallionSettings.model_validate(values)


def test_flag_off_is_a_no_op_and_never_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASSERTS: with MEDALLION_SECRETS_FROM_DAPR unset (the default), apply_dapr_secrets touches
    # NOTHING — the env value survives byte-identical and no store fetch is even attempted
    # (behavior-preserving default: every dev/demo boot is exactly as before this batch).
    import common.secrets as secrets_mod

    def boom(*_a: Any, **_kw: Any) -> dict[str, str]:
        raise AssertionError("must not fetch when the flag is off")

    monkeypatch.setattr(secrets_mod, "fetch_required_secrets", boom)
    settings = _settings()
    apply_dapr_secrets(settings)
    assert settings.s3_secret_access_key.get_secret_value() == "env-value"


def test_flag_on_store_is_the_sole_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASSERTS: flag on → the store bundle's field REPLACES whatever env held (store wins — the
    # chart ships no env in this mode, and any residue must never shadow the store).
    import common.secrets as secrets_mod

    def fake_fetch(store: str, key: str, *, require: str) -> dict[str, str]:
        assert (store, key, require) == ("lance-secrets", "lance", "rustfs-secret-key")
        return {"rustfs-secret-key": "from-store"}

    monkeypatch.setattr(secrets_mod, "fetch_required_secrets", fake_fetch)
    settings = _settings(MEDALLION_SECRETS_FROM_DAPR="true")
    apply_dapr_secrets(settings)
    assert settings.s3_secret_access_key.get_secret_value() == "from-store"
    # SecretStr contract: the value never leaks through repr/model_dump
    assert "from-store" not in repr(settings)


def test_flag_on_store_miss_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASSERTS: an empty/miss bundle raises (through the REAL fetch_required_secrets fail-closed
    # path) — the service must never boot on an empty key or silently fall back to env.
    import common.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "fetch_dapr_secret", lambda *_a, **_kw: {})
    settings = _settings(MEDALLION_SECRETS_FROM_DAPR="true")
    with pytest.raises(RuntimeError, match="failing closed"):
        apply_dapr_secrets(settings)
    assert settings.s3_secret_access_key.get_secret_value() == "env-value"  # untouched on failure
