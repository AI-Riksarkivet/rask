"""A privileged service identity cannot be claimed with the SHARED app token.

THE ESCALATION, measured before this landed:

  LINEAGE_SERVICE_SUBJECTS = "service-trainer,service-web"     one allowlist
  APP_API_TOKEN            = {release}-dapr-app-token          one shared credential

and the caller chose which subject to be, in the `x-lance-service-identity` HEADER. The two subjects
are not peers — `service-web` is a reader on the warehouse; `service-trainer` holds `writer` on
`namespace:models` (`scripts/seed_medallion_fga.sh:80-82`). That shared token is injected into the
env of all SEVEN frontend zones, which run without a Dapr sidecar and therefore cannot use the secret
store at all.

So anyone with env read in any web pod could present the token, claim `service-trainer`, and forge
author-stamped writes into the authoritative lineage graph.

An allowlist cannot close that: it answers "may this SUBJECT use the door", never "may THIS CALLER be
that subject". These tests pin the second question, at lineage's rendering of the door.

WHAT MOVED, and why. These tests used to monkeypatch a lineage-local `_dedicated_token`, and the
door itself was a lineage-local copy of `service_kit.governed.dapr_auth.service_principal`. Both are gone: there is one door and one resolver. So the RESOLVER's own
contract — caching, the request-path retry budget, absent vs unreadable — is pinned next to it in
`packages/service-kit/tests/test_service_door.py`, and what stays here is the escalation itself,
driven through the real secret store seam so no test-only stub can make the door look right.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

from lineage.api.security import _service_principal
from lineage.core.config import LineageSettings
from service_kit.governed import dapr_auth


SHARED = "the-shared-dapr-app-token"
TRAINER_OWN = "the-trainers-own-credential"


def _settings(*, privileged: str = "") -> LineageSettings:
    return LineageSettings(
        LINEAGE_SERVICE_SUBJECTS="service-trainer,service-web",
        LINEAGE_PRIVILEGED_SUBJECTS=privileged,
    )  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", SHARED)


@pytest.fixture(autouse=True)
def _clean_bundle_cache() -> Iterator[None]:
    """The resolver caches per (store, key) for the process lifetime, so one test's seeded store
    would otherwise answer the next one's fetch."""
    dapr_auth._secret_bundle.cache_clear()
    yield
    dapr_auth._secret_bundle.cache_clear()


def _seed_store(monkeypatch: pytest.MonkeyPatch, bundle: dict[str, str]) -> None:
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: bundle)


def test_THE_ESCALATION_the_shared_token_cannot_claim_a_privileged_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test this whole change exists for.

    A web pod holds the SHARED token. With `service-trainer` marked privileged, presenting that token
    while claiming to be the trainer is refused — even though the subject is still allowlisted and the
    token is still perfectly valid for what it IS.
    """
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    with pytest.raises(UnauthenticatedError, match="may not claim"):
        _service_principal(_settings(privileged="service-trainer"), SHARED, "service-trainer")


def test_the_subject_can_still_authenticate_with_ITS_OWN_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legitimate caller is unaffected. The Ray train job holds the trainer's own secret and is
    admitted as before — the control binds the identity to a credential, it does not remove it."""
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    principal = _service_principal(_settings(privileged="service-trainer"), TRAINER_OWN, "service-trainer")

    assert principal.sub == "service-trainer"


def test_an_UNPRIVILEGED_subject_still_uses_the_shared_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`service-web` is what the seven zones legitimately are, and they cannot reach the secret store
    (no Dapr sidecar). Forcing a dedicated credential on them would break every anonymous page load's
    read-only lineage feed, so the shared token stays valid for the read tier."""
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    principal = _service_principal(_settings(privileged="service-trainer"), SHARED, "service-web")

    assert principal.sub == "service-web"


def test_a_privileged_subject_with_NO_provisioned_secret_FAILS_CLOSED(monkeypatch: pytest.MonkeyPatch) -> None:
    """The direction that matters if someone marks a subject privileged and forgets the secret.

    Falling back to the shared token would restore the escalation while LOOKING configured — the
    worst outcome, because the config now claims a protection it does not have. An outage is the
    right answer: it is loud, and it names itself.
    """
    _seed_store(monkeypatch, {"service-token-service-web": "some-other-credential"})

    with pytest.raises(UnauthenticatedError, match="no dedicated credential"):
        _service_principal(_settings(privileged="service-trainer"), SHARED, "service-trainer")


def test_the_default_is_BYTE_IDENTICAL_to_the_previous_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """`privileged_subjects` is empty by default, so nothing changes until a deployment opts in.

    Populating it requires provisioning a secret per listed subject, which is a deployment decision —
    shipping it on by default would take out the trainer lane on upgrade.
    """
    consulted: list[str] = []

    def _fetch(store: str, key: str, **_kwargs: object) -> dict[str, str]:
        consulted.append(store)
        return {"service-token-service-trainer": TRAINER_OWN}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)

    assert _service_principal(_settings(), SHARED, "service-trainer").sub == "service-trainer"
    assert consulted == [], "the secret store was consulted for a subject nobody marked privileged"


def test_an_UNLISTED_subject_is_still_refused_before_any_credential_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original allowlist property, unchanged — and checked FIRST, so an unknown subject never
    reaches the secret store. A door that queries a credential store for arbitrary caller-supplied
    names is a lookup oracle."""
    consulted: list[str] = []

    def _fetch(store: str, key: str, **_kwargs: object) -> dict[str, str]:
        consulted.append(store)
        return {}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)

    with pytest.raises(PermissionDeniedError, match="not allowed"):
        _service_principal(_settings(privileged="service-trainer"), SHARED, "service-impostor")
    assert consulted == []


def test_an_UNREADABLE_store_is_a_503_not_a_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absent-vs-unreadable rule, at lineage's rendering: `fetch_dapr_secret` returns {} both when the store
    is down and when the bundle is empty, and both used to produce the identical 401 "no dedicated
    credential" — the absent-vs-unreadable conflation the estate solved properly in the state plane.
    An outage must say outage, and only this call site can say it in lineage's problem vocabulary."""
    _seed_store(monkeypatch, {})

    with pytest.raises(ServiceUnavailableError, match="unreadable"):
        _service_principal(_settings(privileged="service-trainer"), SHARED, "service-trainer")


def test_a_missing_identity_is_refused() -> None:
    with pytest.raises(PermissionDeniedError, match="not allowed"):
        _service_principal(_settings(), SHARED, None)


def test_an_unconfigured_door_is_refused_not_re_asked_as_OIDC(monkeypatch: pytest.MonkeyPatch) -> None:
    """The §2.8 residual: this door and the catalog's disagreed here. Lineage refused; the catalog
    swallowed the signal and re-asked OIDC, so the same request got two answers depending on which
    service received it. The unified answer is the refusal, and it names the missing knob rather than
    telling an operator their bearer is missing."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(UnauthenticatedError, match="APP_API_TOKEN"):
        _service_principal(_settings(), SHARED, "service-trainer")
