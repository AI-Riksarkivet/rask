"""Notifications stops presenting the estate's shared bearer at lineage's door.

F2-3's LAST subject. `service-web`, `service-maintenance` and `service-ingest` moved first; this
closes the row.

ITS REFUSAL IS THE ONE THAT HIDES BEST, which is why it was worth doing before anyone reported a
symptom. The reconciler WALKS `GET /events` precisely because the bus alone is provably incomplete
(ingest, Ray TRAIN and external OpenLineage producers emit over HTTP only) — and a refused walk
returns no rows rather than an error. So a 401 here is not a failure anyone sees: it is an inbox that
is quietly missing exactly the events the walk existed to catch.

BOTH HEADERS OR NEITHER is preserved through the change. Lineage's door opens on the pair: a request
carrying only `dapr-api-token` falls through to OIDC by design (the sidecar stamps that token on
everything it delivers), and one carrying only the identity is an unauthenticated claim. A deployment
with no credential at all therefore still sends nothing rather than half a service door.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from notifications.api import service_identity
from notifications.api.settings import IngressSettings


def _settings(**overrides: Any) -> IngressSettings:
    base: dict[str, Any] = {"APP_API_TOKEN": "the-shared-bearer", "RASK_NOTIFICATIONS_SECRETS_FROM_DAPR": True}
    return IngressSettings.model_validate(base | overrides)


def test_it_presents_its_OWN_credential_when_the_store_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the change: the token on the wire is this identity's, not the estate's shared one."""
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _s: lambda identity: f"token-for-{identity}")
    token = service_identity.feed_token(_settings())
    assert token is not None
    assert token.get_secret_value() == "token-for-notifications"


def test_it_falls_back_to_the_shared_token_when_the_store_has_no_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A readable store that simply lacks this identity is the UNPRIVILEGED case — every estate that
    has not turned this on. It must keep working unchanged."""
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _s: lambda _identity: None)
    token = service_identity.feed_token(_settings())
    assert token is not None
    assert token.get_secret_value() == "the-shared-bearer"


def test_with_no_store_configured_nothing_reads_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate its siblings needed too: a dev stack has no store, and a service that reads one
    unconditionally fails closed on a configuration that works today."""

    def _explode(_settings: IngressSettings) -> None:
        raise AssertionError("the store was read with RASK_NOTIFICATIONS_SECRETS_FROM_DAPR off")

    monkeypatch.setattr(service_identity, "dedicated_token_for", _explode)
    config = _settings(RASK_NOTIFICATIONS_SECRETS_FROM_DAPR=False)
    monkeypatch.undo()
    assert service_identity.dedicated_token_for(config) is None
    token = service_identity.feed_token(config)
    assert token is not None
    assert token.get_secret_value() == "the-shared-bearer"


def test_an_UNREADABLE_store_raises_rather_than_downgrading(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DISTINCTION THAT MAKES THIS A CONTROL. Falling back when the store cannot be READ would
    return the whole plane to the shared bearer exactly when something is already wrong."""

    def _explode(_identity: str) -> str | None:
        raise RuntimeError("secret store unreachable")

    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _s: _explode)
    with pytest.raises(RuntimeError, match="unreachable"):
        service_identity.feed_token(_settings())


def test_NO_credential_at_all_still_sends_NOTHING(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both headers or neither. Without a token there is no service door in this deployment, and
    sending the identity alone is an unauthenticated claim that 401s for a reason nobody can see."""
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _s: lambda _identity: None)
    assert service_identity.feed_token(_settings(APP_API_TOKEN=None)) is None


def test_the_token_never_leaves_SecretStr() -> None:
    """It was already read as a setting rather than off `os.environ` so it could not reach a log line
    or a repr by accident. Resolving a DIFFERENT token must not lose that."""
    resolved = service_identity.feed_token(_settings(RASK_NOTIFICATIONS_SECRETS_FROM_DAPR=False))
    assert isinstance(resolved, SecretStr)
    assert "the-shared-bearer" not in repr(resolved)
