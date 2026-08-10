"""The settings contract: named knobs, and one invariant that refuses to boot rather than leak."""

import pytest
from pydantic import ValidationError

from notifications.config import NotificationsSettings, get_notifications_settings


def test_the_retention_knobs_are_settings_not_literals() -> None:
    """open_notifications §11 q3: the VALUES stay UNVERIFIED — only real volume can set them — so what
    is settled is that measuring them later is a config change rather than a code change."""
    settings = get_notifications_settings()
    assert (settings.inbox_ttl_seconds, settings.compaction_interval_seconds, settings.inbox_max_rows) == (86400, 3600, 5)


def test_the_defaults_are_production_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("INBOX_TTL_SECONDS", "COMPACTION_INTERVAL_SECONDS", "INBOX_MAX_ROWS", "INBOX_PAGE_LIMIT"):
        monkeypatch.delenv(f"RASK_NOTIFICATIONS_{name}", raising=False)
    settings = NotificationsSettings.model_validate({})
    assert settings.inbox_ttl_seconds == 30 * 24 * 3600
    assert settings.compaction_interval_seconds == 6 * 3600
    assert (settings.inbox_max_rows, settings.inbox_page_limit) == (200, 20)


def test_compaction_slower_than_retention_refuses_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compaction is the ONLY authoritative bound on an inbox — `ActorStateTTL` is off on this estate,
    so the actor sends no `ttlInSeconds` at all (daprd refuses the write). A compaction interval at
    or past the retention window therefore means rows outliving the window with nothing noticing, which
    is a fail-at-boot rather than a fail-quietly."""
    monkeypatch.setenv("RASK_NOTIFICATIONS_INBOX_TTL_SECONDS", "3600")
    monkeypatch.setenv("RASK_NOTIFICATIONS_COMPACTION_INTERVAL_SECONDS", "3600")
    with pytest.raises(ValidationError, match="only thing that bounds an inbox"):
        NotificationsSettings.model_validate({})


def test_authorization_still_requires_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited from `GovernedAuthSettings` and asserted here because this service is the one whose
    whole point is that a badge counts YOUR work: checking a subject nobody verified would be worse
    than no check at all."""
    monkeypatch.setenv("LANCE_FGA_ENABLED", "true")
    with pytest.raises(ValidationError, match="LANCE_OIDC_ENABLED"):
        NotificationsSettings.model_validate({})
