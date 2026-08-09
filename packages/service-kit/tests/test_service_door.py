"""The estate's ONE service door — its refusals, and the fact that they are the door's own types.

open_dapr.md §2.8 landed the shared resolver but not the shared door: `services/lineage` kept a full
second copy of this body, and the two disagreed about the unconfigured door — lineage refused, the
catalog swallowed the signal and re-asked OIDC. Two doors with different answers to the same question
is worse than either answer, because whichever one an auditor reads, the other is the one that ran.

These tests pin the single body. The call-site RENDERINGS are pinned next to each service
(`services/lineage/tests/test_service_door.py`, `services/catalog/tests/test_service_door.py`); what
lives here is the decision itself:

  * the unconfigured door REFUSES — `ServiceDoorClosed` is a refusal, not a fall-through signal;
  * every refusal is a `ServiceDoorError`, never a `fastapi.HTTPException`, because both consumers
    answer in RFC 9457 problem+json and a raw HTTPException escapes those handlers as a bare
    `{"detail": ...}` body;
  * absent vs unreadable stays split (§2.17) — a store outage is never a credential refusal.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException

from service_kit.governed import dapr_auth
from service_kit.governed.dapr_auth import (
    CredentialRejected,
    SecretStoreUnreadable,
    ServiceDoorClosed,
    ServiceDoorError,
    SubjectNotAllowed,
    dedicated_token_from_store,
    service_principal,
)


SHARED = "the-shared-dapr-app-token"
TRAINER_OWN = "the-trainers-own-credential"
ALLOWED = "service-trainer,service-web"


@pytest.fixture(autouse=True)
def _clean_bundle_cache() -> Iterator[None]:
    """`_secret_bundle` is an `lru_cache` on the module, so one test's seeded store would otherwise
    answer the next one's fetch."""
    dapr_auth._secret_bundle.cache_clear()
    yield
    dapr_auth._secret_bundle.cache_clear()


@pytest.fixture
def app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", SHARED)


def _seed_store(monkeypatch: pytest.MonkeyPatch, bundle: dict[str, str]) -> None:
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: bundle)


# --------------------------------------------------------------------------- #
# THE UNIFIED NO-CREDENTIAL ANSWER
# --------------------------------------------------------------------------- #


def test_the_unconfigured_door_REFUSES_rather_than_signalling_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `APP_API_TOKEN` = the door does not exist here, and that is a 401 that NAMES ITSELF.

    The catalog used to catch this and `pass`, so an operator who set the allowlist and forgot the
    token read "Missing bearer token" and went hunting in the IdP. Reaching this door at all takes
    BOTH service headers, which the gateway strips and the zones' BFF sends only when there is no
    session — so the caller asked to be a service, and the service door is the only one they get.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(ServiceDoorClosed, match="APP_API_TOKEN"):
        service_principal(token=SHARED, identity="service-trainer", allowed_subjects=ALLOWED)


def test_the_unconfigured_door_is_refused_BEFORE_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order matters for the message: an unconfigured door must not be reported as an allowlist
    problem, or the operator fixes the wrong knob."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(ServiceDoorClosed):
        service_principal(token=SHARED, identity="nobody-at-all", allowed_subjects=ALLOWED)


# --------------------------------------------------------------------------- #
# THE DOOR DECIDES, THE CALL SITE RENDERS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"token": SHARED, "identity": "alice"}, SubjectNotAllowed),
        ({"token": SHARED, "identity": None}, SubjectNotAllowed),
        ({"token": "wrong", "identity": "service-trainer"}, CredentialRejected),
        ({"token": "", "identity": "service-trainer"}, CredentialRejected),
    ],
)
def test_every_refusal_is_a_ServiceDoorError_never_an_HTTPException(app_token: None, kwargs: dict[str, Any], expected: type[ServiceDoorError]) -> None:
    """`service_kit` must not pick the wire shape. Both consumers install
    `service_kit.lakehouse.ns_errors`' problem+json handlers, and a `fastapi.HTTPException` slips
    past them as a bare `{"detail": ...}` body — breaking the contract each of their module
    docstrings promises. So the door raises its own neutral types and each call site maps them."""
    with pytest.raises(expected) as exc:
        service_principal(allowed_subjects=ALLOWED, **kwargs)

    assert isinstance(exc.value, ServiceDoorError)
    assert not isinstance(exc.value, HTTPException)


def test_an_allowlisted_subject_with_the_shared_token_is_admitted(app_token: None) -> None:
    """The unprivileged read tier: the seven sidecar-less web pods cannot reach a secret store, so
    the shared token stays valid for the subjects nobody marked privileged."""
    assert service_principal(token=SHARED, identity="service-web", allowed_subjects=ALLOWED).sub == "service-web"


# --------------------------------------------------------------------------- #
# the PRIVILEGED branch — the escalation this door exists to stop
# --------------------------------------------------------------------------- #


def test_the_shared_token_cannot_claim_a_privileged_subject(app_token: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist answers "may this SUBJECT use the door"; only a dedicated credential answers
    "may THIS CALLER be that subject". Without the second, any holder of the shared token picks the
    highest-privileged name on the list."""
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})
    resolver = dedicated_token_from_store("lance-secrets", "lance")

    admitted = service_principal(
        token=TRAINER_OWN, identity="service-trainer", allowed_subjects=ALLOWED, privileged_subjects="service-trainer", dedicated_token=resolver
    )
    assert admitted.sub == "service-trainer"

    with pytest.raises(CredentialRejected, match="may not claim"):
        service_principal(token=SHARED, identity="service-trainer", allowed_subjects=ALLOWED, privileged_subjects="service-trainer", dedicated_token=resolver)


def test_an_unprovisioned_privileged_credential_FAILS_CLOSED(app_token: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A privileged subject whose secret was never seeded must NOT fall back to the shared token: a
    quiet fallback restores the escalation while LOOKING configured, which is worse than not having
    the control at all."""
    _seed_store(monkeypatch, {"unrelated": "field"})

    with pytest.raises(CredentialRejected, match="no dedicated credential"):
        service_principal(
            token=SHARED,
            identity="service-trainer",
            allowed_subjects=ALLOWED,
            privileged_subjects="service-trainer",
            dedicated_token=dedicated_token_from_store("lance-secrets", "lance"),
        )


def test_omitting_the_resolver_cannot_open_the_privileged_branch(app_token: None) -> None:
    """§2.8 as it actually shipped: the catalog passed no `dedicated_token=`, the callback defaulted
    to None, and every privileged subject was refused no matter what the store held. Refusing is the
    right direction — pinned here so the default can never become "fall back to the shared token"."""
    with pytest.raises(CredentialRejected, match="no dedicated credential"):
        service_principal(token=SHARED, identity="service-trainer", allowed_subjects=ALLOWED, privileged_subjects="service-trainer")


def test_an_unlisted_subject_never_reaches_the_credential_store(app_token: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is checked FIRST. A door that looks up arbitrary caller-supplied names in a
    credential store is an enumeration oracle."""
    consulted: list[str] = []

    def _resolver(identity: str) -> str | None:
        consulted.append(identity)
        return TRAINER_OWN

    with pytest.raises(SubjectNotAllowed):
        service_principal(
            token=SHARED, identity="service-impostor", allowed_subjects=ALLOWED, privileged_subjects="service-impostor", dedicated_token=_resolver
        )
    assert consulted == []


def test_an_unprivileged_subject_never_consults_the_store(app_token: None) -> None:
    """`privileged_subjects` is empty by default, and until a deployment opts in the store must not
    be touched — populating it requires seeding a secret per listed subject first."""
    consulted: list[str] = []

    def _resolver(identity: str) -> str | None:
        consulted.append(identity)
        return TRAINER_OWN

    admitted = service_principal(token=SHARED, identity="service-trainer", allowed_subjects=ALLOWED, dedicated_token=_resolver)

    assert admitted.sub == "service-trainer"
    assert consulted == [], "the store was consulted for a subject nobody marked privileged"


# --------------------------------------------------------------------------- #
# absent vs unreadable (§2.17) — the resolver's half
# --------------------------------------------------------------------------- #


def test_an_UNREADABLE_store_is_not_a_missing_credential(app_token: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fetch_dapr_secret` returns {} both when the store is down and when the bundle is empty, and
    both used to produce the identical 401 — the conflation the estate solved properly in the state
    plane. An outage must say outage: `SecretStoreUnreadable` is a RuntimeError, deliberately NOT a
    `ServiceDoorError`, so a call site cannot render it as a refusal by catching the base class."""
    _seed_store(monkeypatch, {})

    with pytest.raises(SecretStoreUnreadable, match="unreadable") as exc:
        service_principal(
            token=TRAINER_OWN,
            identity="service-trainer",
            allowed_subjects=ALLOWED,
            privileged_subjects="service-trainer",
            dedicated_token=dedicated_token_from_store("lance-secrets", "lance"),
        )
    assert not isinstance(exc.value, ServiceDoorError)


def test_a_readable_bundle_without_the_field_is_a_genuine_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_store(monkeypatch, {"unrelated": "field"})

    assert dedicated_token_from_store("lance-secrets", "lance")("service-trainer") is None


def test_the_bundle_is_fetched_once_and_with_the_REQUEST_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """This runs inside a SYNC request dependency (the AnyIO threadpool), not a boot lifespan — the
    boot budget of 10 exponential-backoff attempts stalled a worker for minutes per cold call
    (§2.17). One fetch, cached; retries=1, the viewer's precedent."""
    calls: list[dict[str, object]] = []

    def _fetch(store: str, key: str, **kwargs: object) -> dict[str, str]:
        calls.append(kwargs)
        return {"service-token-service-trainer": TRAINER_OWN}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)
    resolve = dedicated_token_from_store("lance-secrets", "lance")

    assert resolve("service-trainer") == TRAINER_OWN
    assert resolve("service-trainer") == TRAINER_OWN

    assert len(calls) == 1, "the bundle must be fetched once, not per request"
    assert calls[0].get("retries") == 1, "a request-path fetch must not burn the boot retry budget"


def test_an_unreadable_store_is_NOT_cached_as_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lru_cache` never caches exceptions, and that is load-bearing: a store that was down at the
    first privileged request must be retried on the next one, not remembered as unreadable until the
    pod restarts."""
    attempts: list[int] = []

    def _fetch(store: str, key: str, **kwargs: object) -> dict[str, str]:
        attempts.append(1)
        return {} if len(attempts) == 1 else {"service-token-service-trainer": TRAINER_OWN}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)
    resolve = dedicated_token_from_store("lance-secrets", "lance")

    with pytest.raises(SecretStoreUnreadable):
        resolve("service-trainer")
    assert resolve("service-trainer") == TRAINER_OWN
