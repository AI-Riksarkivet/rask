"""service-kit Dapr wiring — config gating + client factory (no sidecar needed)."""

import pytest

from service_kit.config import Settings


def _settings(**env: str) -> Settings:
    return Settings.model_validate({"RASK_VIEWER_INPUT": "/dev/null", "RASK_VIEWER_OUTPUT": "/dev/null", **env})


def test_dapr_disabled_by_default() -> None:
    s = _settings()
    assert s.dapr_enabled is False
    assert s.dapr_http_port == "3500"


def test_dapr_enabled_from_env() -> None:
    s = _settings(RASK_DAPR_ENABLED="true", DAPR_HTTP_PORT="3555")
    assert s.dapr_enabled is True
    assert s.dapr_http_port == "3555"


def test_build_dapr_client_none_when_disabled() -> None:
    from service_kit import build_dapr_client

    assert build_dapr_client(_settings()) is None


def test_build_dapr_client_builds_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import service_kit

    captured: dict[str, str] = {}

    class FakeDaprClient:
        def __init__(self, address: str) -> None:
            captured["address"] = address

        def close(self) -> None:
            captured["closed"] = "yes"

    # Patch the lazy import target so no real dapr package / sidecar is needed.
    monkeypatch.setattr(service_kit, "_import_dapr_client", lambda: FakeDaprClient, raising=True)

    client = service_kit.build_dapr_client(_settings(RASK_DAPR_ENABLED="true", DAPR_HTTP_PORT="3500"))
    assert isinstance(client, FakeDaprClient)
    assert captured["address"] == "http://127.0.0.1:3500"


# ── the public front door must never take a service-token path ────────────────


def test_the_public_caller_list_is_ONE_list_for_the_estate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configurable, case-insensitive, and shared.

    Deployments name their own front doors, an HTTP header's case is not something to bet on, and a
    PER-SERVICE copy of this list is the drift the shared definition exists to prevent — a newly added
    edge would be refused by one door and trusted by another.
    """
    from service_kit.governed.dapr_auth import is_public_caller

    assert is_public_caller("gateway")
    assert is_public_caller("GATEWAY")
    assert is_public_caller("  Gateway  ")
    assert not is_public_caller("medallion")
    assert not is_public_caller(None), "an ABSENT header is pub/sub or Service-DNS delivery — the legitimate path"
    assert not is_public_caller("")

    monkeypatch.setenv("RASK_PUBLIC_CALLERS", "gateway, Edge-Proxy")
    assert is_public_caller("edge-proxy")
    assert is_public_caller("gateway")
    assert not is_public_caller("medallion")


def test_require_dapr_token_REFUSES_a_public_caller_even_with_a_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured bypass, at the shared primitive that guards twelve sidecar-only routes.

    daprd stamps a valid `dapr-api-token` on everything it hands the app, and the gateway forwards
    through Dapr service invocation — so an anonymous public request arrives holding the estate's
    service credential. These routes are sidecar-DELIVERY-only by construction; a front-door
    invocation of one is never legitimate, which is why the refusal comes before the token is even
    compared.
    """
    from fastapi import HTTPException

    from service_kit.governed.dapr_auth import require_dapr_token

    monkeypatch.setenv("APP_API_TOKEN", "shared-secret")

    with pytest.raises(HTTPException) as caught:
        require_dapr_token(dapr_api_token="shared-secret", dapr_caller_app_id="gateway")
    assert caught.value.status_code == 403
    assert "public front door" in str(caught.value.detail)


def test_require_dapr_token_REFUSES_a_public_caller_even_in_DEV(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset APP_API_TOKEN is the open dev default for the TOKEN check — not for this one.

    With no token configured the token comparison is a no-op, so if the public-caller refusal were
    conditional on it, dev would have no guard at all on routes that are sidecar-only by design.
    """
    from fastapi import HTTPException

    from service_kit.governed.dapr_auth import require_dapr_token

    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(HTTPException) as caught:
        require_dapr_token(dapr_caller_app_id="gateway")
    assert caught.value.status_code == 403


def test_the_LEGITIMATE_delivery_paths_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pub/sub delivery, binding delivery and a direct Service-DNS call carry NO caller app-id.

    Those are every legitimate way onto the routes this dependency guards. If absence were treated as
    public, the entire cascade would stop — a fix that breaks the system it protects is not a fix.
    """
    from service_kit.governed.dapr_auth import require_dapr_token

    monkeypatch.setenv("APP_API_TOKEN", "shared-secret")

    require_dapr_token(dapr_api_token="shared-secret")  # no caller id — must not raise
    require_dapr_token(dapr_api_token="shared-secret", dapr_caller_app_id="medallion")
