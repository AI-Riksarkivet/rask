"""service-kit Dapr wiring — the config gating and the shared front-door guard (no sidecar needed).

The CLIENT FACTORY that used to be tested here is gone. It built
`DaprClient("http://127.0.0.1:3500")` — the sidecar's HTTP port handed to a gRPC client — and the
test below it asserted that wrong constant as though it were the contract, which is how a defect
acquires a green gate. Two re-verifications found no caller at all: every service that publishes
builds its own client. The settings survive because other code reads the same environment variables;
only the factory and its dependency are gone.
"""

import pytest


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
    # `PermissionDeniedError`, not `HTTPException`: the guard raises a DOMAIN error now so its 403
    # wears the estate's problem+json envelope (open_fastapi-audit).
    from lance_namespace import PermissionDeniedError

    from service_kit.governed.dapr_auth import require_dapr_token

    monkeypatch.setenv("APP_API_TOKEN", "shared-secret")

    with pytest.raises(PermissionDeniedError) as caught:
        require_dapr_token(dapr_api_token="shared-secret", dapr_caller_app_id="gateway")
    assert "public front door" in str(caught.value)


def test_require_dapr_token_REFUSES_a_public_caller_even_in_DEV(monkeypatch: pytest.MonkeyPatch) -> None:
    """The front-door refusal comes FIRST, so it holds whatever the token check would have answered.

    Both branches refuse here, which is why the ORDER is what this pins: a deployment that has taken
    the `RASK_ALLOW_UNAUTHENTICATED_DAPR` hatch still refuses a front door, and it can only do that if
    the caller check runs before the token check rather than after it.
    """
    # `PermissionDeniedError`, not `HTTPException`: the guard raises a DOMAIN error now so its 403
    # wears the estate's problem+json envelope (open_fastapi-audit).
    from lance_namespace import PermissionDeniedError

    from service_kit.governed.dapr_auth import require_dapr_token

    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(PermissionDeniedError):
        require_dapr_token(dapr_caller_app_id="gateway")


def test_the_LEGITIMATE_delivery_paths_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pub/sub delivery, binding delivery and a direct Service-DNS call carry NO caller app-id.

    Those are every legitimate way onto the routes this dependency guards. If absence were treated as
    public, the entire cascade would stop — a fix that breaks the system it protects is not a fix.
    """
    from service_kit.governed.dapr_auth import require_dapr_token

    monkeypatch.setenv("APP_API_TOKEN", "shared-secret")

    require_dapr_token(dapr_api_token="shared-secret")  # no caller id — must not raise
    require_dapr_token(dapr_api_token="shared-secret", dapr_caller_app_id="medallion")
