"""The shared-bearer fallback resolves the token the way the doors that verify it do.

`service_headers` sends maintenance's own credential when the store holds one and falls back to the
SHARED bearer otherwise — "which is every estate that has not provisioned this identity", per its own
docstring. That fallback read `DaprDoorSettings().app_api_token`: the ENV branch alone.

`expected_app_token` exists precisely to stop that. Its docstring: "ONE resolver for all three
consumers ... because a deployment that moves its token to the store must move every door with it: a
service door still reading env while the Dapr door reads the store is a pod where half the credentials
are configured and nothing says which half."

MEASURED ON THE DEPLOYED MAINTENANCE POD 2026-09-19 — `app_token_from_store: True`,
`DaprDoorSettings().app_api_token: None`, `expected_app_token(): 'lance-dev-dapr-app-token-…'`. The env
branch is empty on this estate and the store branch is not.

NOT LIVE-BROKEN HERE, and saying so is part of the finding: `service-maintenance` IS provisioned, so
the dedicated resolver answers and the fallback never runs — the header goes out. The defect bites the
combination the docstring names as the common one: token in the store, dedicated identity absent. There
the fallback reads empty and the call goes out with NO service bearer at all, which is the silent
outbound failure `medallion/core/config.py::outbound_app_token` was written for after it cost "2,700
[401s] in twenty-five minutes with zero successes".
"""

from __future__ import annotations

import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import catalog_identity


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings()


def test_the_fallback_uses_the_store_when_env_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT: a token the estate has, that this fallback could not see."""
    monkeypatch.setattr(catalog_identity, "dedicated_token_for", lambda _s: None)
    monkeypatch.setattr(catalog_identity, "expected_app_token", lambda: "from-the-store")

    headers = catalog_identity.service_headers(_settings())

    assert headers.get("dapr-api-token") == "from-the-store"


def test_a_provisioned_identity_still_WINS(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control. The fallback must not start overriding the dedicated credential."""
    monkeypatch.setattr(catalog_identity, "dedicated_token_for", lambda _s: lambda _identity: "its-own-token")
    monkeypatch.setattr(catalog_identity, "expected_app_token", lambda: "the-shared-one")

    assert catalog_identity.service_headers(_settings()).get("dapr-api-token") == "its-own-token"


def test_neither_source_sends_NO_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty header is worse than none: it presents a credential the door must then reject."""
    monkeypatch.setattr(catalog_identity, "dedicated_token_for", lambda _s: None)
    monkeypatch.setattr(catalog_identity, "expected_app_token", lambda: "")

    assert "dapr-api-token" not in catalog_identity.service_headers(_settings())


def test_the_identity_is_always_named() -> None:
    """Whatever the credential, the door needs to know who is claiming it."""
    assert "x-lance-service-identity" in catalog_identity.service_headers(_settings())
