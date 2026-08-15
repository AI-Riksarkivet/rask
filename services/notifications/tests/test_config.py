"""The settings contract: named knobs, and one invariant that refuses to boot rather than leak."""

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from notifications.api.settings import get_ingress_settings
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


@pytest.fixture(autouse=True)
def _settings_cache_is_not_shared_between_tests() -> Iterator[None]:
    """Clear the settings caches AFTER each test as well as before.

    `monkeypatch` restores the environment, but it cannot restore an `lru_cache` that was populated
    while the environment was patched — so a test that builds settings under `RASK_DAPR_ENABLED=true`
    leaves that object cached for whoever runs next. That is exactly what happened: adding the two
    tests below turned `test_inbox_door_contract.py` red while it still passed in isolation.
    """
    yield
    get_ingress_settings.cache_clear()
    get_notifications_settings.cache_clear()


# --- the feed read goes through the sidecar, so a Dapr policy can cover it ------------------------
#
# `LineageFeedClient.page` carried its own tenacity retry, and the module said why: "This is the
# service's OWN egress: no sidecar policy covers it, so the retry has to." True of a DIRECT httpx call
# — and the reason it was direct is not recorded anywhere.
#
# Routed through Dapr service invocation instead, the estate's EXISTING `invokeRetry` policy covers it,
# and that policy already encodes the same rule the hand-written `_is_transient` did:
# `matching: httpStatusCodes: 408,429,500-599` — retry what a retry can fix, never a 4xx. Its comment
# gives the same reasoning, measured on the ingest door: "Retrying a 403 is also pointless on its own
# terms: the answer will not change." `lineage` is already a target of that policy
# (chart/templates/dapr-resiliency.yaml builds $invoked from services.lineage.daprAppId).
#
# The gateway established the shape (`_target_base`): sidecar when Dapr is on, direct URL otherwise, so
# dev and the unit tests keep a plain base URL.


def test_the_feed_is_read_direct_when_dapr_is_off(monkeypatch) -> None:
    monkeypatch.delenv("RASK_DAPR_ENABLED", raising=False)
    monkeypatch.setenv("RASK_NOTIFICATIONS_LINEAGE_URL", "http://rask-lineage:8000")
    get_ingress_settings.cache_clear()

    assert get_ingress_settings().feed_base_url == "http://rask-lineage:8000"


def test_the_feed_is_read_through_the_sidecar_when_dapr_is_on(monkeypatch) -> None:
    monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    monkeypatch.setenv("RASK_NOTIFICATIONS_LINEAGE_URL", "http://rask-lineage:8000")
    get_ingress_settings.cache_clear()

    assert get_ingress_settings().feed_base_url == "http://127.0.0.1:3500/v1.0/invoke/lineage/method", (
        "the feed read bypasses the sidecar, so no Dapr resiliency policy can cover it and the retry has to be hand-written"
    )
