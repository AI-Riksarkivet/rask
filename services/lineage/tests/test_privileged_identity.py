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
that subject". These tests pin the second question.
"""

from __future__ import annotations

import pytest
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from lineage.api.security import _service_principal
from lineage.core.config import LineageSettings


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


def test_THE_ESCALATION_the_shared_token_cannot_claim_a_privileged_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test this whole change exists for.

    A web pod holds the SHARED token. With `service-trainer` marked privileged, presenting that token
    while claiming to be the trainer is refused — even though the subject is still allowlisted and the
    token is still perfectly valid for what it IS.
    """
    monkeypatch.setattr("lineage.api.security._dedicated_token", lambda identity: TRAINER_OWN)

    with pytest.raises(UnauthenticatedError, match="may not claim"):
        _service_principal(_settings(privileged="service-trainer"), SHARED, "service-trainer")


def test_the_subject_can_still_authenticate_with_ITS_OWN_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legitimate caller is unaffected. The Ray train job holds the trainer's own secret and is
    admitted as before — the control binds the identity to a credential, it does not remove it."""
    monkeypatch.setattr("lineage.api.security._dedicated_token", lambda identity: TRAINER_OWN)

    principal = _service_principal(_settings(privileged="service-trainer"), TRAINER_OWN, "service-trainer")

    assert principal.sub == "service-trainer"


def test_an_UNPRIVILEGED_subject_still_uses_the_shared_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`service-web` is what the seven zones legitimately are, and they cannot reach the secret store
    (no Dapr sidecar). Forcing a dedicated credential on them would break every anonymous page load's
    read-only lineage feed, so the shared token stays valid for the read tier."""
    monkeypatch.setattr("lineage.api.security._dedicated_token", lambda identity: TRAINER_OWN)

    principal = _service_principal(_settings(privileged="service-trainer"), SHARED, "service-web")

    assert principal.sub == "service-web"


def test_a_privileged_subject_with_NO_provisioned_secret_FAILS_CLOSED(monkeypatch: pytest.MonkeyPatch) -> None:
    """The direction that matters if someone marks a subject privileged and forgets the secret.

    Falling back to the shared token would restore the escalation while LOOKING configured — the
    worst outcome, because the config now claims a protection it does not have. An outage is the
    right answer: it is loud, and it names itself.
    """
    monkeypatch.setattr("lineage.api.security._dedicated_token", lambda identity: None)

    with pytest.raises(UnauthenticatedError, match="no dedicated credential"):
        _service_principal(_settings(privileged="service-trainer"), SHARED, "service-trainer")


def test_the_default_is_BYTE_IDENTICAL_to_the_previous_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """`privileged_subjects` is empty by default, so nothing changes until a deployment opts in.

    Populating it requires provisioning a secret per listed subject, which is a deployment decision —
    shipping it on by default would take out the trainer lane on upgrade.
    """
    called: list[str] = []
    monkeypatch.setattr("lineage.api.security._dedicated_token", lambda identity: called.append(identity) or None)

    assert _service_principal(_settings(), SHARED, "service-trainer").sub == "service-trainer"
    assert called == [], "the secret store was consulted for a subject nobody marked privileged"


def test_an_UNLISTED_subject_is_still_refused_before_any_credential_check() -> None:
    """The original allowlist property, unchanged — and checked FIRST, so an unknown subject never
    reaches the secret store. A door that queries a credential store for arbitrary caller-supplied
    names is a lookup oracle."""
    with pytest.raises(PermissionDeniedError, match="not allowed"):
        _service_principal(_settings(privileged="service-trainer"), SHARED, "service-impostor")


def test_an_UNREADABLE_store_is_a_503_not_a_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_dapr.md §2.17: `fetch_dapr_secret` returns {} both when the store is down and when the
    bundle is empty — and both used to produce the identical 401 "no dedicated credential", the
    absent-vs-unreadable conflation the estate solved properly in the state plane. An outage must
    say outage."""
    from lineage.api import security

    security._secret_bundle.cache_clear()
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: {})

    with pytest.raises(ServiceUnavailableError, match="unreadable"):
        security._dedicated_token("service-trainer")


def test_a_readable_bundle_without_the_field_is_a_genuine_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the store WAS read and the field is simply not provisioned, None is the honest answer —
    the caller turns it into the loud fail-closed 401."""
    from lineage.api import security

    security._secret_bundle.cache_clear()
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: {"unrelated": "field"})

    assert security._dedicated_token("service-trainer") is None


def test_the_bundle_is_fetched_once_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """This runs inside a SYNC request dependency, not a boot lifespan — the boot retry budget
    (10 attempts, exponential backoff) stalled a request worker for minutes per cold call. One
    fetch, cached; retries=1 (the viewer precedent)."""
    from lineage.api import security

    security._secret_bundle.cache_clear()
    calls: list[dict[str, object]] = []

    def _fetch(store: str, key: str, **kwargs: object) -> dict[str, str]:
        calls.append(kwargs)
        return {"service-token-service-trainer": TRAINER_OWN}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)

    assert security._dedicated_token("service-trainer") == TRAINER_OWN
    assert security._dedicated_token("service-trainer") == TRAINER_OWN
    assert len(calls) == 1, "the bundle must be fetched once, not per request"
    assert calls[0].get("retries") == 1, "a request-path fetch must not burn the boot retry budget"
    security._secret_bundle.cache_clear()


def test_a_missing_identity_is_refused() -> None:
    with pytest.raises(PermissionDeniedError, match="not allowed"):
        _service_principal(_settings(), SHARED, None)
