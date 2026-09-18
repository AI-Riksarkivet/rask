"""The credential this service PRESENTS resolves the app token the same way its DOORS verify it.

THE REGRESSION THIS PINS, and it was self-inflicted. [[LH-160]] moved nine services off `APP_API_TOKEN`
as a `secretKeyRef` onto the Dapr secret store — the estate's rule is that a secret never travels through
the environment. The INBOUND doors moved with it, because they all go through
`service_kit.governed.dapr_auth.expected_app_token()`, which reads the store when
`RASK_APP_TOKEN_FROM_STORE` is set. The OUTBOUND credential did not: every medallion call to the catalog
built its headers from `settings.app_api_token`, the raw env field, which is now empty by design.

`catalog_register.credential` needs BOTH halves — "the door requires the token AND the identity … sending
one is refused for a reason invisible from this side" — so an empty token makes it return `{}` and the
call goes out UNAUTHENTICATED. Measured on the live estate: `rask-medallion-producer` logged 2,700
`401 Unauthorized` and zero 200s in twenty-five minutes, every cascade-lag edge failing.

WHY IT WAS INVISIBLE TO EVERY GATE. The chart render was correct and its invariants passed; the inbound
door was correct and answers 403 to a forged call; the unit suites run with FGA off and a literal token
in env, so `settings.app_api_token` is always populated there. Nothing compared the token a service
PRESENTS with the token its own doors EXPECT — which is the property this file adds, because those two
are the same secret and must be resolved by the same accessor.
"""

from __future__ import annotations

import pytest

from medallion.core.config import MedallionSettings, outbound_app_token


def test_the_store_path_resolves_a_token_even_with_no_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT. Store mode is exactly the deployed shape: the flag on, the env var gone."""
    monkeypatch.setenv("RASK_APP_TOKEN_FROM_STORE", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.setattr("service_kit.governed.dapr_auth.expected_app_token", lambda: "from-the-store")

    assert outbound_app_token(MedallionSettings()) == "from-the-store", (
        "the outbound credential resolved no token while the store held one — every catalog call goes out unauthenticated and the door answers 401"
    )


def test_env_still_works_for_a_deployment_not_on_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """`compute` is not scoped to `lance-secrets`, and `make dev-micro` runs with no sidecar at all.

    Pinned so the fix cannot be "always read the store": a deployment that legitimately carries the env
    value must keep working, which is what `expected_app_token` already does by returning it.
    """
    monkeypatch.delenv("RASK_APP_TOKEN_FROM_STORE", raising=False)
    monkeypatch.setenv("APP_API_TOKEN", "from-the-env")

    assert outbound_app_token(MedallionSettings()) == "from-the-env"


def test_an_unreadable_store_does_not_silently_send_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store outage must not degrade into an UNAUTHENTICATED call.

    `credential()` returns `{}` when the token is empty, and a request with no headers is not a refusal
    a caller can see — it is a 401 whose cause lives one service away. The resolver raising is the honest
    answer; the caller's own retry then means what it says.
    """
    from service_kit.governed.dapr_auth import SecretStoreUnreadable

    monkeypatch.setenv("RASK_APP_TOKEN_FROM_STORE", "true")
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    def _unreadable() -> str | None:
        raise SecretStoreUnreadable("the sidecar is not answering")

    monkeypatch.setattr("service_kit.governed.dapr_auth.expected_app_token", _unreadable)

    with pytest.raises(SecretStoreUnreadable):
        outbound_app_token(MedallionSettings())
