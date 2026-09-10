"""Ingest stops presenting the estate's shared bearer at the two doors it calls.

F2-3's remainder. `service-ingest` is one of the three subjects that still held the shared
`APP_API_TOKEN`; `service_identity.service_headers` is the ONE builder both of its doors use.

TWO DOORS ON TWO DIFFERENT SERVICES, one subject. Ingest claims `service-ingest` at the catalog
(`catalog_service.py`) and at the lineage graph (`provenance.py`), and `dapr_auth.service_principal`
refuses a privileged subject presenting the shared token AND an ordinary subject presenting a
dedicated one. Those branches are mutually exclusive, so a credential that reached one door and not
the other would leave the subject unable to satisfy both — whichever token it sent, one door would
refuse it. That is why the builder is shared rather than copied.

THE LINEAGE DOOR IS THE ONE THAT FAILS QUIETLY, which is why it is not the door to leave for later:
a refused emit is swallowed by design (a landed commit must not become a failed run), so the symptom
is a permanent gap in the graph rather than an error. That has already happened on this exact lane —
`service-ingest` 403'd for a day in 2026-08 while the data landed.

WHY THE STORE READ IS GATED HERE AND NOT IN MAINTENANCE. `catalog_token()` deliberately SKIPS the
secret store when the identity and shared token are both set, because a fail-closed fetch written
before the catalog had an identity door turned a missing-and-unneeded `catalog-token` into a failed
run at the first activity. Building the resolver unconditionally would put that read back and fail
closed on a dev stack that has no store and needs none — so `RASK_INGEST_SECRETS_FROM_DAPR` is an
explicit opt-in, and `test_with_no_store_configured_nothing_reads_one` is the test that pins it.
"""

from __future__ import annotations

from typing import Any

import pytest

from ingest import service_identity
from ingest.config import IngestSettings


def _config(**overrides: object) -> IngestSettings:
    base: dict[str, object] = {
        "RASK_CATALOG_SERVICE_IDENTITY": "service-ingest",
        "RASK_LINEAGE_SERVICE_IDENTITY": "service-ingest",
        "APP_API_TOKEN": "the-shared-bearer",
        "RASK_INGEST_SECRETS_FROM_DAPR": True,
    }
    return IngestSettings.model_validate(base | overrides)


def test_it_presents_its_OWN_credential_when_the_store_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the change: the token on the wire is this identity's, not the estate's shared one."""
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _c: lambda identity: f"token-for-{identity}")
    config = _config()
    headers = service_identity.service_headers(config, identity="service-ingest", shared_token=config.catalog_app_token)
    assert headers["dapr-api-token"] == "token-for-service-ingest"
    assert headers["x-lance-service-identity"] == "service-ingest"


def test_BOTH_doors_get_the_same_dedicated_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subject privileged at one door and ordinary at the other cannot satisfy both — whichever
    token it sends, one refuses it. So the catalog and the graph must receive the same one."""
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _c: lambda identity: f"token-for-{identity}")
    config = _config()
    catalog = service_identity.service_headers(config, identity=config.catalog_service_identity, shared_token=config.catalog_app_token)
    lineage = service_identity.service_headers(config, identity=config.lineage_service_identity, shared_token=config.lineage_app_token)
    assert catalog == lineage, "the two doors received different credentials for one subject"


def test_it_falls_back_to_the_shared_token_when_the_store_has_no_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A readable store that simply lacks this identity is the UNPRIVILEGED case — every estate that
    has not turned this on. It must keep working unchanged."""
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _c: lambda _identity: None)
    config = _config()
    headers = service_identity.service_headers(config, identity="service-ingest", shared_token=config.catalog_app_token)
    assert headers["dapr-api-token"] == "the-shared-bearer"


def test_with_no_store_configured_nothing_reads_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GATE THAT MAINTENANCE DID NOT NEED. `catalog_token` skips the store when the identity and
    the shared token are both set; an unconditional resolver would put that read back and fail closed
    on a dev stack that has one of each and no store at all."""

    def _explode(_config: IngestSettings) -> None:
        raise AssertionError("the store was read with RASK_INGEST_SECRETS_FROM_DAPR off")

    monkeypatch.setattr(service_identity, "dedicated_token_for", _explode)
    config = _config(RASK_INGEST_SECRETS_FROM_DAPR=False)
    # `dedicated_token_for` is the real one here — it must answer None before touching any store.
    monkeypatch.undo()
    assert service_identity.dedicated_token_for(config) is None
    headers = service_identity.service_headers(config, identity="service-ingest", shared_token=config.catalog_app_token)
    assert headers["dapr-api-token"] == "the-shared-bearer"


def test_an_UNREADABLE_store_raises_rather_than_downgrading(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DISTINCTION THAT MAKES THIS A CONTROL. Falling back when the store cannot be READ would
    return the whole plane to the shared bearer exactly when something is already wrong."""

    def _explode(_identity: str) -> str | None:
        raise RuntimeError("secret store unreachable")

    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _c: _explode)
    config = _config()
    with pytest.raises(RuntimeError, match="unreachable"):
        service_identity.service_headers(config, identity="service-ingest", shared_token=config.catalog_app_token)


def test_HALF_CONFIGURED_sends_nothing_at_all() -> None:
    """A door needs the token AND the subject; sending one is a request refused for a reason
    invisible from this side. `{}` lets the caller take its own fallback instead."""
    # The gate is off here on purpose: half-configured is about the identity/token PAIR, and leaving
    # the store in would test the resolver instead of the rule. Resolution still comes first when it
    # is on — a store holding the dedicated token must win over an absent shared one.
    config = _config(RASK_INGEST_SECRETS_FROM_DAPR=False)
    assert service_identity.service_headers(config, identity=None, shared_token="tok") == {}
    assert service_identity.service_headers(config, identity="service-ingest", shared_token=None) == {}


def test_EVERY_door_ingest_calls_uses_the_one_builder() -> None:
    """A credential control applied to one of two doors is not a control. Discovered by grep rather
    than listed, so a THIRD door reds this instead of drifting."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src/ingest"
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "x-lance-service-identity" not in text or path.name == "service_identity.py":
            continue
        if "service_headers" not in text:
            offenders.append(str(path.relative_to(src)))
    assert not offenders, (
        f"these build the identity headers themselves instead of calling service_headers: {offenders} — "
        "a dedicated credential applied to some doors and not others is not a credential control"
    )


def _lineage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transport half of the deployed environment: an endpoint, a claimed subject, a shared token."""
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://rask-lineage:8000")
    monkeypatch.setenv("RASK_LINEAGE_SERVICE_IDENTITY", "service-ingest")
    monkeypatch.setenv("APP_API_TOKEN", "the-shared-bearer")
    monkeypatch.setenv("RASK_INGEST_SECRETS_FROM_DAPR", "true")
    monkeypatch.delenv("RASK_LINEAGE_TOKEN_SERVICE_INGEST", raising=False)


def _wire_headers(emitter: Any) -> dict[str, str]:
    """The headers the built transport would actually send.

    Reached through the client rather than asserted on a builder's return value, because a builder
    that returns the right dict and a caller that never invokes it are indistinguishable from the
    outside — which is exactly the defect this test exists for.
    """
    transport = emitter._client.transport  # private by intent: the wire is the subject of this test
    return dict(transport.config.custom_headers or {})


def test_the_EMIT_presents_the_dedicated_credential_the_READ_path_already_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WIRE, not the builder. `service_headers` is correct and had ONE of its two lineage callers.

    Ingest holds two clients for the one lineage service: `provenance.py` READS `GET /runs/{id}` with
    the shared builder, and `lineage.py` WRITES through lineage-kit's own emitter, which knows only
    `LineageSettings.app_token` — the estate's shared bearer. `service-ingest` is on
    `LINEAGE_PRIVILEGED_SUBJECTS`, and `dapr_auth.service_principal` refuses the shared token from a
    privileged name, so every emit was refused.

    Measured on the deployed estate 2026-09-10, and the shape is why nobody saw it: the HTTP lineage
    door had served exactly two requests in its retained log and answered 401 to both, while 806
    events arrived over the Dapr topic from producers that do not use this path. A 100% failure rate
    on the one door ingest uses read as a healthy graph, because the ingest run reports COMPLETE by
    design (I8 — a landed commit must not become a failed run).
    """
    _lineage_env(monkeypatch)
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _c: lambda identity: f"token-for-{identity}")

    from ingest import lineage

    headers = _wire_headers(lineage._emitter())
    assert headers.get("x-lance-service-identity") == "service-ingest"
    assert headers.get("dapr-api-token") == "token-for-service-ingest", (
        "the emit presents the estate's shared bearer, which a privileged subject's door refuses"
    )


def test_with_no_dedicated_credential_the_emit_keeps_the_shared_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """An identity the store simply lacks is not privileged as far as this side can tell.

    The door stays the single authority on whether the shared bearer is acceptable — falling back is
    what keeps an auth-off dev stack, and an estate that has not turned dedicated credentials on,
    working exactly as before.
    """
    _lineage_env(monkeypatch)
    monkeypatch.setattr(service_identity, "dedicated_token_for", lambda _c: lambda _identity: None)

    from ingest import lineage

    assert _wire_headers(lineage._emitter()).get("dapr-api-token") == "the-shared-bearer"
