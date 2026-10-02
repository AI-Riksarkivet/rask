"""A service's signing key is read from the secret store once, and an unreadable store is never read as absent.

`dedicated_token_from_store` resolves `service-token-<identity>`, the key a service signs its lineage events with and
lineage verifies them with until [[LH-064]] moves signing to per-identity keys. No door authenticates with it: a
service is the service account its projected token names ([[LH-220]]). What lives here is the resolver's half of the
absent-vs-unreadable split (§2.17): a store outage is never reported as a missing credential.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from service_kit.governed import dapr_auth
from service_kit.governed.dapr_auth import SecretStoreUnreadable, dedicated_token_from_store


SHARED = "the-shared-dapr-app-token"
TRAINER_OWN = "the-trainers-own-credential"


@pytest.fixture(autouse=True)
def _clean_bundle_cache() -> Iterator[None]:
    """`_secret_bundle` is an `lru_cache` on the module, so one test's seeded store would otherwise
    answer the next one's fetch."""
    dapr_auth._secret_bundle.cache_clear()
    yield
    dapr_auth._secret_bundle.cache_clear()


# --------------------------------------------------------------------------- #
# absent vs unreadable (§2.17) — the resolver's half
# --------------------------------------------------------------------------- #


def test_the_bundle_is_fetched_once_and_with_the_REQUEST_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """This runs inside a SYNC request dependency (the AnyIO threadpool), not a boot lifespan — the
    boot budget of 10 exponential-backoff attempts stalled a worker for minutes per cold call
    (§2.17). One fetch, cached; retries=1, the viewer's precedent."""
    calls: list[dict[str, object]] = []

    def _fetch(store: str, key: str, **kwargs: object) -> dict[str, str]:
        calls.append(kwargs)
        return {"token": TRAINER_OWN} if key == "service-token-service-trainer" else {"app-api-token": SHARED}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)
    resolve = dedicated_token_from_store("lance-secrets")

    assert resolve("service-trainer") == TRAINER_OWN
    assert resolve("service-trainer") == TRAINER_OWN

    assert len(calls) == 1, "the bundle must be fetched once, not per request"
    assert calls[0].get("retries") == 1, "a request-path fetch must not burn the boot retry budget"


def test_an_unreadable_store_is_NOT_cached_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lru_cache` never caches exceptions, and that is load-bearing: a store that was down at the
    first privileged request must be retried on the next one, not remembered as unreadable until the
    pod restarts."""
    # A FLAG, NOT A FETCH COUNTER. A failed resolve now makes TWO reads -- the identity's own secret and
    # the shared bundle it uses as a reachability control -- so counting fetches would flip this double
    # to "healthy" midway through the first resolve and it would answer None instead of raising.
    down = [True]

    def _fetch(store: str, key: str, **kwargs: object) -> dict[str, str]:
        if down[0]:
            return {}
        return {"token": TRAINER_OWN} if key == "service-token-service-trainer" else {"app-api-token": SHARED}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)
    resolve = dedicated_token_from_store("lance-secrets")

    with pytest.raises(SecretStoreUnreadable):
        resolve("service-trainer")
    down[0] = False
    assert resolve("service-trainer") == TRAINER_OWN


def test_an_absent_identity_answers_None_while_an_unreadable_store_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absent-vs-unreadable split, preserved across the per-identity split ([[XC-072]]).

    Before each credential became its own secret, a subject with none was a missing FIELD of a bundle
    that still answered, so the two cases were already distinct. Addressing one secret per identity
    collapses them -- both look like a fetch that returned nothing -- and the difference decides the
    caller's status: `None` refuses this subject on its merits (401), raising fails the door closed
    (503). Collapsed the wrong way, an unreachable store 401s every privileged producer, which reads as
    a credential problem and sends the operator to the wrong system entirely.

    The shared bundle is the control: same store, same sidecar, so if it answers the store is up.
    """
    from service_kit.governed import dapr_auth

    dapr_auth._secret_bundle.cache_clear()

    # STORE UP, IDENTITY GENUINELY ABSENT -> None.
    def _up(_store: str, key: str, **_kw: object) -> dict[str, str]:
        return {} if key.startswith("service-token-") else {"app-api-token": SHARED}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _up)
    assert dedicated_token_from_store("lance-secrets")("service-trainer") is None

    # STORE DOWN -> raise, so the door fails closed rather than blaming the credential.
    dapr_auth._secret_bundle.cache_clear()
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: {})
    with pytest.raises(SecretStoreUnreadable):
        dedicated_token_from_store("lance-secrets")("service-trainer")
    dapr_auth._secret_bundle.cache_clear()
