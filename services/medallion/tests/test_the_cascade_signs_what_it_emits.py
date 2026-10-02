"""Every lineage event the cascade emits is signed as its service with the key its identity publishes, or not emitted.

[[LH-064]]. The bus verifier refuses an unsigned event and acknowledges the refusal, so an event that left unsigned
would be gone, and the cascade head reacts to the bronze-write event, so a run whose first event is lost never starts.
`emit_lineage` is the one door out of this service, so what it does with the key is the whole claim: a resolved key
signs the event the service built, and a service that is meant to sign and has no key sends nothing.

The sidecar's secret API is answered by respx, so the key reaches the door the way it does in the cluster: through
`start_signing`, the holder and the two secrets. The events are the real builder's, with a float duration, because that
is the shape a medallion COMPLETE carries on the wire.

The route-level half of "emits nothing" (the readiness probe and the RETRY on every delivery while the key is
unresolved) is `test_a_signer_without_its_key_emits_nothing.py`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from lineage_kit import verify_signature
from medallion.core import lineage_publish
from medallion.core.config import MedallionSettings
from medallion.schemas.events import build_run_event
from service_kit.governed.signing_key import SigningKeyUnavailableError
from service_kit.lakehouse import outbox


IDENTITY = "service-bronze-to-silver"
ROLE = "data_eng"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _Published:
    """The one identity's public keys, as lineage would read them."""

    def __init__(self, *keys: str) -> None:
        self._keys = keys

    def published(self, identity: str) -> Sequence[str]:
        return self._keys

    def refresh(self, identity: str) -> Sequence[str]:
        return self._keys


def _settings(*, from_store: bool = True, identity: str = IDENTITY) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_SECRETS_FROM_DAPR": from_store,
            "RASK_SIGNING_IDENTITY": identity,
            "MEDALLION_FGA_SERVICE_IDENTITY": IDENTITY,
            "MEDALLION_AUTHOR": ROLE,
            "MEDALLION_LINEAGE_OUTBOX_URI": "",
        }
    )


def _event() -> dict[str, Any]:
    return build_run_event(
        operation="stage_silver",
        author=ROLE,
        author_subject=IDENTITY,
        job_namespace="lance-medallion",
        inputs=[("lance", "bronze$events")],
        output_namespace="lance",
        output_name="silver$events",
        token="a-token",
        duration_seconds=12.345678901,
        event_type="COMPLETE",
    )


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch, respx_allows_unused_routes: None) -> Iterator[list[str]]:
    """The JSON the outbox seam is handed: the bytes that are staged AND published."""
    captured: list[str] = []

    async def _capture(_client: object, **kwargs: Any) -> None:
        captured.append(str(kwargs["event_json"]))

    monkeypatch.setattr(outbox, "publish_lineage_with_outbox", _capture)
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    with respx.mock:
        yield captured


def _store(seed: httpx.Response, keys: httpx.Response) -> None:
    respx.get(f"{SECRETS}/signing-key-{IDENTITY}").mock(return_value=seed)
    respx.get(f"{SECRETS}/signing-public-{IDENTITY}").mock(return_value=keys)


@pytest.mark.asyncio
async def test_an_emitted_event_verifies_against_the_published_key_as_the_service_and_not_the_role(published: list[str], event_signer: Any) -> None:
    pair = event_signer(IDENTITY)
    _store(httpx.Response(200, json={"seed": pair.seed}), httpx.Response(200, json={"keys": pair.public}))
    settings = _settings()
    holder = await lineage_publish.start_signing(FastAPI(), settings)
    try:
        await lineage_publish.emit_lineage(object(), settings, _event())
    finally:
        await lineage_publish.stop_signing(holder)

    assert len(published) == 1, "nothing was published"
    verified = verify_signature(json.loads(published[0]), source=_Published(pair.public), signers=frozenset({IDENTITY}), delegators=frozenset())
    assert (verified.identity, verified.kid, verified.on_behalf_of) == (IDENTITY, pair.kid, None)


@pytest.mark.parametrize(
    "lifespan_ran",
    [
        pytest.param(True, id="a-key-the-store-will-not-give"),
        pytest.param(False, id="a-service-whose-lifespan-never-ran"),
    ],
)
@pytest.mark.asyncio
async def test_a_service_meant_to_sign_that_has_no_key_emits_nothing(published: list[str], lifespan_ran: bool) -> None:
    _store(httpx.Response(500), httpx.Response(404))
    settings = _settings()
    holder = await lineage_publish.start_signing(FastAPI(), settings) if lifespan_ran else None
    try:
        with pytest.raises(SigningKeyUnavailableError):
            await lineage_publish.emit_lineage(object(), settings, _event())
    finally:
        await lineage_publish.stop_signing(holder)

    assert published == [], f"a signer without its key published {published}"


@pytest.mark.parametrize(
    ("from_store", "identity"),
    [
        pytest.param(False, IDENTITY, id="a-service-with-no-secret-store"),
        pytest.param(True, "", id="a-service-the-chart-gave-no-signing-identity"),
    ],
)
@pytest.mark.asyncio
async def test_a_service_that_is_not_configured_to_sign_emits_the_event_it_built(published: list[str], from_store: bool, identity: str) -> None:
    _store(httpx.Response(500), httpx.Response(404))
    settings = _settings(from_store=from_store, identity=identity)
    event = _event()
    holder = await lineage_publish.start_signing(FastAPI(), settings)
    try:
        await lineage_publish.emit_lineage(object(), settings, event)
    finally:
        await lineage_publish.stop_signing(holder)

    assert [json.loads(sent) for sent in published] == [event]


@pytest.mark.asyncio
async def test_a_service_refuses_to_sign_as_an_identity_it_does_not_stamp_as_its_author(published: list[str]) -> None:
    """A verifier refuses a signer that is not its event's author, so every event would be lost: the boot refuses."""
    with pytest.raises(ValueError, match="signer that is not its event's author"):
        await lineage_publish.start_signing(FastAPI(), _settings(identity="service-someone-else"))
