"""A signer holds its own key only while its identity lists it, and heals in place ([[LH-064]]).

A verifier refuses an event signed with a key its identity does not publish and acknowledges the refusal, so a signer
that kept signing with an unlisted key would destroy every event it emits while every probe stayed green. The holder
therefore reads the seed and the published list through its own sidecar, is ready only while the derived key is listed,
holds no key at all otherwise, and keeps re-resolving so a store that is re-minted or seeded late is followed without a
restart.

DRIVEN AGAINST THE SIDECAR'S SECRET API, which respx answers over the real `fetch_dapr_secret`: the claim is what the
holder does with each answer the sidecar can give (a value, a 500, a 404, an empty field), so the answers are the inputs.
The two callables the holder takes from lineage-kit are plain stand-ins here, because this package cannot import it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Annotated

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from service_kit.governed.signing_key import SigningKeyHolder, SigningKeyUnavailableError, attach_signing, retry_until_signed, signing_ready_check
from service_kit.lifecycle import mark_started
from service_kit.probes import make_probes_router


IDENTITY = "service-test"
SIDECAR = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _Key(BaseModel):
    """What the holder needs of a key: the text a verifier lists and the id that names it."""

    model_config = ConfigDict(frozen=True)

    public_nkey: str
    kid: str


def _load_key(seed: str) -> _Key:
    if not seed.startswith("SEED-"):
        raise ValueError("not a seed")
    name = seed.removeprefix("SEED-")
    return _Key(public_nkey=f"PUB-{name}", kid=name)


def _parse_published(field: str) -> list[str]:
    return [entry.strip() for entry in field.split(",") if entry.strip()]


def _holder(*, refresh_seconds: float = 300.0, retry_seconds: float = 15.0) -> SigningKeyHolder[_Key]:
    return SigningKeyHolder(
        identity=IDENTITY,
        store="lance-secrets",
        load_key=_load_key,
        parse_published=_parse_published,
        refresh_seconds=refresh_seconds,
        retry_seconds=retry_seconds,
    )


def _found(field: str, value: str) -> httpx.Response:
    return httpx.Response(200, json={field: value})


class _Store:
    """The sidecar's two answers for this identity, changeable between reads."""

    def __init__(self) -> None:
        self.seed = respx.get(f"{SIDECAR}/signing-key-{IDENTITY}")
        self.keys = respx.get(f"{SIDECAR}/signing-public-{IDENTITY}")

    def serve(self, seed: httpx.Response, keys: httpx.Response) -> None:
        self.seed.mock(return_value=seed)
        self.keys.mock(return_value=keys)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, respx_allows_unused_routes: None) -> Iterator[_Store]:
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    with respx.mock:
        yield _Store()


def _held_kid(holder: SigningKeyHolder[_Key]) -> str | None:
    try:
        return holder.key().kid
    except SigningKeyUnavailableError:
        return None


@pytest.mark.parametrize(
    ("seed", "keys", "kid"),
    [
        pytest.param(_found("seed", "SEED-a"), _found("keys", "PUB-b, PUB-a"), "a", id="a-key-its-identity-lists"),
        pytest.param(httpx.Response(500), _found("keys", "PUB-a"), None, id="a-seed-the-store-cannot-answer"),
        pytest.param(httpx.Response(404), _found("keys", "PUB-a"), None, id="a-seed-that-is-not-there"),
        pytest.param(_found("seed", ""), _found("keys", "PUB-a"), None, id="an-empty-seed"),
        pytest.param(_found("seed", "not-a-seed"), _found("keys", "PUB-a"), None, id="a-seed-that-is-no-key"),
        pytest.param(_found("seed", "SEED-a"), httpx.Response(404), None, id="an-identity-that-publishes-nothing"),
        pytest.param(_found("seed", "SEED-a"), _found("keys", "PUB-b"), None, id="a-key-its-identity-does-not-list"),
    ],
)
def test_a_signer_holds_a_key_only_while_its_identity_lists_it(store: _Store, seed: httpx.Response, keys: httpx.Response, kid: str | None) -> None:
    store.serve(seed, keys)
    holder = _holder()

    ready = holder.resolve()

    assert (ready, holder.ready, _held_kid(holder)) == (kid is not None, kid is not None, kid)


@pytest.mark.asyncio
async def test_a_signer_heals_in_place_follows_a_re_minted_store_and_drops_an_unlisted_key(store: _Store) -> None:
    """Driven through the background loop, with a poll for each state it must reach: the loop is the claim."""

    store.serve(httpx.Response(500), _found("keys", "PUB-a"))
    holder = _holder(refresh_seconds=0.05, retry_seconds=0.01)

    async def reaches(kid: str | None, state: str) -> None:
        deadline = time.monotonic() + 5.0
        while _held_kid(holder) != kid:
            assert time.monotonic() < deadline, f"the signer never reached {state}: it holds {_held_kid(holder)!r}, wanted {kid!r}"
            await asyncio.sleep(0.01)

    await holder.start()
    try:
        assert _held_kid(holder) is None, "the signer held a key the store would not give it"

        store.serve(_found("seed", "SEED-a"), _found("keys", "PUB-a"))
        await reaches("a", "the key the store later answered")

        store.serve(_found("seed", "SEED-b"), _found("keys", "PUB-b"))
        await reaches("b", "the key a re-minted store published")

        store.serve(_found("seed", "SEED-b"), _found("keys", "PUB-c"))
        await reaches(None, "no key once its identity stopped listing it")
    finally:
        await holder.stop()


@pytest.mark.parametrize(
    ("seed", "delivery", "readiness"),
    [
        pytest.param(_found("seed", "SEED-a"), "SUCCESS", 200, id="a-key-its-identity-lists"),
        pytest.param(httpx.Response(500), "RETRY", 503, id="no-key"),
        pytest.param(None, "SUCCESS", 200, id="a-service-that-does-not-sign"),
    ],
)
def test_a_signer_takes_deliveries_and_reports_ready_only_while_it_holds_its_key(
    store: _Store, seed: httpx.Response | None, delivery: str, readiness: int
) -> None:
    holder: SigningKeyHolder[_Key] | None = None
    if seed is not None:
        store.serve(seed, _found("keys", "PUB-a"))
        holder = _holder()
        holder.resolve()
    app = FastAPI()
    app.include_router(make_probes_router(signing_ready_check()))
    mark_started(app)
    attach_signing(app, holder)

    @app.post("/delivery")
    async def deliver(signing: Annotated[dict[str, str] | None, Depends(retry_until_signed)] = None) -> dict[str, str]:
        return signing if signing is not None else {"status": "SUCCESS"}

    client = TestClient(app)
    answered, probed = client.post("/delivery"), client.get("/readyz")

    assert (answered.json()["status"], probed.status_code) == (delivery, readiness)


@pytest.mark.parametrize(
    ("identity", "refresh_seconds", "retry_seconds"),
    [
        pytest.param("../lance", 300.0, 15.0, id="an-identity-that-escapes-its-secret-name"),
        pytest.param("Service-Test", 300.0, 15.0, id="an-identity-the-chart-never-renders"),
        pytest.param(IDENTITY, 301.0, 15.0, id="a-refresh-longer-than-five-minutes"),
        pytest.param(IDENTITY, 10.0, 15.0, id="a-retry-slower-than-the-refresh"),
    ],
)
def test_a_holder_refuses_an_identity_and_intervals_it_could_not_honour(identity: str, refresh_seconds: float, retry_seconds: float) -> None:
    with pytest.raises(ValueError, match=r"signing identity|intervals"):
        SigningKeyHolder(
            identity=identity,
            store="lance-secrets",
            load_key=_load_key,
            parse_published=_parse_published,
            refresh_seconds=refresh_seconds,
            retry_seconds=retry_seconds,
        )
