"""Env-driven transport config: RASK_* first, official OpenLineage names as aliases."""

from __future__ import annotations

import pytest
from openlineage.client.transport.http import HttpTransport

from lineage_kit import ClientEmitter, Emitter, LineageSettings, NoopEmitter, build_emitter


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


def _wire_headers(emitter: Emitter) -> dict[str, str]:
    """Every header one emit puts on the wire: the custom headers plus the auth provider's bearer.

    The bearer lives on the transport's auth provider rather than in `custom_headers`, so it is folded in
    under ``authorization``, the header the transport sends it as.
    """
    assert isinstance(emitter, ClientEmitter), f"expected an HTTP emitter, got {type(emitter).__name__}"
    transport = emitter._client.transport
    assert isinstance(transport, HttpTransport), f"expected the HTTP transport, got {type(transport).__name__}"
    headers = dict(transport.config.custom_headers)
    if (bearer := transport.config.auth.get_bearer()) is not None:
        headers["authorization"] = bearer
    return headers


def test_an_absent_service_id_never_becomes_an_empty_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-empty `x-lance-service-identity` is worse than an absent one, because the
    receiving door forks on PRESENCE.

    `services/lineage/src/lineage/api/security.py:164` reads
    `if dapr_api_token is not None and x_lance_service_identity is not None`, and `""` is not None —
    so an empty identity ASKS FOR the service door, and that branch is final: its own comment says
    "a refusal inside this branch is final and never re-asks OIDC". An emitter that set the header
    whenever `LINEAGE_SERVICE_TOKEN` is present, defaulting the id to `""`, would take that door with no
    subject, and a perfectly good `LINEAGE_TOKEN` bearer would never be tried.

    The result is a job that does its work and loses its provenance: the run's rows land and its
    terminal event 403s, which is invisible from the job and from the graph alike.
    """
    # The env below is what keeps this from being a tautology: these are the RAY TRAIN LANE's own
    # variable spellings, not `lineage-kit`'s canonical `RASK_LINEAGE_*` ones. The package accepts them
    # only through `AliasChoices`; with `LINEAGE_URL` unaccepted the credential resolves and the endpoint
    # does not, and the lane degrades to a silent no-op.
    name = "lineage-kit, driven with the Ray train lane's env"
    for key in ("LINEAGE_URL", "LINEAGE_SERVICE_TOKEN", "LINEAGE_SERVICE_ID", "LINEAGE_TOKEN", "RASK_LINEAGE_TOKEN_SERVICE_BRONZE_TO_SILVER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {"LINEAGE_URL": "http://lineage:8000", "LINEAGE_SERVICE_TOKEN": "app-token", "LINEAGE_TOKEN": "a.valid.bearer"}.items():
        monkeypatch.setenv(key, value)

    headers = _wire_headers(build_emitter())

    assert headers.get("x-lance-service-identity") != "", f"{name} sends an EMPTY service identity, which takes the service door with no subject and 403s"
    assert "authorization" in headers, f"{name} discarded a valid LINEAGE_TOKEN bearer while presenting no usable service identity"


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
