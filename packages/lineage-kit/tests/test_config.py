"""Env-driven transport config: RASK_* first, official OpenLineage names as aliases."""

from __future__ import annotations

import pytest

from lineage_kit import ClientEmitter, LineageSettings, NoopEmitter, build_emitter


def test_defaults_are_noop_friendly() -> None:
    s = LineageSettings()
    assert s.endpoint is None
    assert s.api_key is None
    assert s.namespace == "rask"
    assert s.endpoint_path == "api/v1/lineage"
    assert s.transport == "auto"


def test_rask_env_vars_configure_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    monkeypatch.setenv("RASK_LINEAGE_API_KEY", "sekrit")
    monkeypatch.setenv("RASK_LINEAGE_NAMESPACE", "htr")
    monkeypatch.setenv("RASK_LINEAGE_TIMEOUT", "2.5")
    s = LineageSettings()
    assert s.endpoint == "http://marquez:5000"
    assert s.api_key == "sekrit"
    assert s.namespace == "htr"
    assert s.timeout == 2.5


def test_official_openlineage_names_are_accepted_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLINEAGE_URL", "http://marquez:5000")
    monkeypatch.setenv("OPENLINEAGE_NAMESPACE", "htr")
    s = LineageSettings()
    assert s.endpoint == "http://marquez:5000"
    assert s.namespace == "htr"


def test_rask_name_wins_over_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://rask-wins:5000")
    monkeypatch.setenv("OPENLINEAGE_URL", "http://alias-loses:5000")
    assert LineageSettings().endpoint == "http://rask-wins:5000"


def test_auto_transport_without_endpoint_is_noop() -> None:
    assert isinstance(build_emitter(), NoopEmitter)


def test_auto_transport_with_endpoint_is_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    assert isinstance(build_emitter(), ClientEmitter)


def _headers(emitter: object) -> dict[str, str]:
    """The custom headers the built transport will actually send."""
    return dict(emitter._client.transport.config.custom_headers)  # ty: ignore[unresolved-attribute]


def test_service_door_credentials_are_sent_as_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """rask's ingest authenticates a service by TWO HEADERS, not by a bearer.

    ``lineage.api.security.authenticate`` opens the service door only when both ``dapr-api-token`` and
    ``x-lance-service-identity`` are present, and otherwise falls through to OIDC. A transport that can
    only set ``Authorization: Bearer`` therefore 401s against a governed deployment — and since
    ``ClientEmitter.emit`` catches transport errors, the events disappear behind one log line. That is
    the recorded 2026-07-13 incident ("every training RunEvent 401'd, silently losing all training
    provenance"), which this makes structurally impossible to repeat.
    """
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://lineage:8000")
    monkeypatch.setenv("LINEAGE_SERVICE_TOKEN", "shared-app-token")
    monkeypatch.setenv("LINEAGE_SERVICE_ID", "service-trainer")

    headers = _headers(build_emitter())
    assert headers["dapr-api-token"] == "shared-app-token"
    assert headers["x-lance-service-identity"] == "service-trainer"


def test_app_api_token_is_accepted_as_the_token_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fleet pod already mounts APP_API_TOKEN from the Dapr app-token secret; reading it means a
    deployment does not carry the same secret twice under two names."""
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://lineage:8000")
    monkeypatch.setenv("APP_API_TOKEN", "from-the-dapr-secret")
    monkeypatch.setenv("LINEAGE_SERVICE_ID", "service-trainer")
    assert _headers(build_emitter())["dapr-api-token"] == "from-the-dapr-secret"


@pytest.mark.parametrize(
    ("env", "value"),
    [("LINEAGE_SERVICE_TOKEN", "tok"), ("LINEAGE_SERVICE_ID", "service-trainer")],
    ids=["token-only", "identity-only"],
)
def test_half_configured_service_door_sends_neither_header(monkeypatch: pytest.MonkeyPatch, env: str, value: str) -> None:
    """Half-configured must send NOTHING, not half.

    The ingest deliberately treats a token-only request as a human request and falls through to OIDC (so
    a gateway-proxied user carrying a sidecar-stamped token is not diverted into the service door and
    403'd on the missing identity — the 2026-07-15 audit). Sending one header would therefore be worse
    than sending none: it changes which branch the ingest takes without being able to satisfy it.
    """
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://lineage:8000")
    monkeypatch.setenv(env, value)
    assert _headers(build_emitter()) == {}


def test_no_service_door_config_sends_no_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The open dev path (auth off) must stay byte-identical — no headers invented from nothing."""
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://lineage:8000")
    assert _headers(build_emitter()) == {}


def test_forced_noop_overrides_a_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "noop")
    assert isinstance(build_emitter(), NoopEmitter)


def test_console_transport_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "console")
    assert isinstance(build_emitter(), ClientEmitter)


def test_http_forced_without_endpoint_degrades_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "http")
    assert isinstance(build_emitter(), NoopEmitter)
