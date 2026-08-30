"""The lineage call site drives the SHARED service door, and renders its refusals as problem+json.

The residual of unifying the service doors: the shared resolver landed but not the shared door. This module kept a
full second copy of `service_kit.governed.dapr_auth.service_principal` — same allowlist, same
privileged branch, and a DIFFERENT answer when `APP_API_TOKEN` was unset. The catalog's equivalent
suite (`tests/unit/test_catalog_gateway_proxied_human.py`) drives `catalog.authenticate` itself for
exactly this reason: a test that calls the shared helper directly stays green while the fork it was
meant to delete is the code that actually runs. So every test here goes through
`lineage.api.security.authenticate`.

What it pins:

  * the door BODY is the shared one, called with lineage's settings (allowlist, privileged list, and
    the store coordinates from `LINEAGE_DAPR_SECRET_STORE`/`_KEY` — the fork read
    `LINEAGE_SECRET_STORE`, a name nothing in the estate sets);
  * the UNIFIED no-credential answer: an unconfigured door is a 401 that names itself, at both call
    sites, never a silent fall-through to OIDC;
  * absent vs unreadable stays split — a store outage is 503, never "no credential provisioned";
  * the public front door still cannot mint a service principal.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

from lineage.api import security
from lineage.core.config import LineageSettings
from service_kit.governed import dapr_auth
from service_kit.governed.dapr_auth import ServiceIdentity


SHARED = "the-shared-dapr-app-token"
TRAINER_OWN = "the-trainers-own-credential"
_ISSUER = "https://idp.example.com"


def _settings(*, privileged: str = "", subjects: str = "service-trainer,service-web") -> LineageSettings:
    return LineageSettings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": _ISSUER,
            "oidc_audience": "lance",
            "service_subjects": subjects,
            "privileged_subjects": privileged,
        }
    )


def _request(**state: object) -> Request:
    """A fake request exposing only ``request.app.state`` — `authenticate` reads ``oidc``."""
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state))))


def _verifier() -> object:
    """A wired OIDC verifier, so a fall-through would end at 401 "Missing bearer token" rather than
    at the 503 an unwired verifier produces. Without it, "did it fall through?" is unanswerable."""
    return SimpleNamespace(verify=lambda _t: None)


@pytest.fixture(autouse=True)
def _clean_bundle_cache() -> Iterator[None]:
    dapr_auth._secret_bundle.cache_clear()
    yield
    dapr_auth._secret_bundle.cache_clear()


@pytest.fixture(autouse=True)
def _app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", SHARED)


def _seed_store(monkeypatch: pytest.MonkeyPatch, bundle: dict[str, str]) -> None:
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: bundle)


def _authenticate(settings: LineageSettings, *, token: str | None, identity: str | None, caller: str | None = "medallion") -> security.Principal | None:
    return security.authenticate(
        _request(oidc=_verifier()),
        settings,
        None,
        dapr_api_token=token,
        x_lance_service_identity=identity,
        dapr_caller_app_id=caller,
    )


# --------------------------------------------------------------------------- #
# ONE DOOR — the fork cannot silently return
# --------------------------------------------------------------------------- #


def test_the_door_body_is_the_SHARED_one_called_with_lineage_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The structural half of §2.8. A re-forked `_service_principal` would answer this request out of
    its own body and never touch the recorded seam — which is exactly how the fork survived a suite
    that tested `service_principal` directly."""
    seen: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> ServiceIdentity:
        seen.update(kwargs)
        return ServiceIdentity("service-trainer")

    monkeypatch.setattr(security, "service_principal", _spy)

    principal = _authenticate(_settings(privileged="service-trainer"), token=TRAINER_OWN, identity="service-trainer")

    assert principal is not None and principal.sub == "service-trainer"
    assert seen["allowed_subjects"] == "service-trainer,service-web"
    assert seen["privileged_subjects"] == "service-trainer"
    assert seen["token"] == TRAINER_OWN
    assert seen["identity"] == "service-trainer"
    assert callable(seen["dedicated_token"]), "the resolver must be PASSED — §2.8's defect was one missing kwarg"


def test_the_resolver_reads_the_store_the_operator_actually_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fork read `LINEAGE_SECRET_STORE`/`LINEAGE_SECRET_KEY` from `os.environ`, and nothing in the
    estate sets those names — the chart, the compose stacks and `apply_dapr_secrets` all speak
    `LINEAGE_DAPR_SECRET_STORE`/`_KEY`. So an operator who repointed the store correctly still had
    this door querying `lance-secrets`, reading nothing, and refusing every privileged subject on a
    deployment that looked configured."""
    asked: list[tuple[str, str]] = []

    def _fetch(store: str, key: str, **_kwargs: object) -> dict[str, str]:
        asked.append((store, key))
        return {"service-token-service-trainer": TRAINER_OWN}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)
    settings = _settings(privileged="service-trainer").model_copy(update={"dapr_secret_store": "prod-secrets", "dapr_secret_key": "rask"})

    principal = _authenticate(settings, token=TRAINER_OWN, identity="service-trainer")

    assert principal is not None and principal.sub == "service-trainer"
    assert asked == [("prod-secrets", "rask")]


# --------------------------------------------------------------------------- #
# THE UNIFIED NO-CREDENTIAL ANSWER
# --------------------------------------------------------------------------- #


def test_an_unconfigured_door_is_a_401_that_NAMES_ITSELF(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both call sites give the same answer now: the caller asked for the service door by sending
    both service headers, so an absent `APP_API_TOKEN` is reported as an absent service door — not
    re-asked as "Missing bearer token", which is what a fall-through hands the operator."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(UnauthenticatedError, match="APP_API_TOKEN"):
        _authenticate(_settings(), token=SHARED, identity="service-trainer")


def test_a_request_that_did_NOT_ask_for_the_door_still_falls_to_OIDC(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half, and the reason the refusal above is safe: with `dapr.io/app-token-secret` set,
    daprd stamps `dapr-api-token` on EVERY request it delivers, so a proxied human arrives holding
    it. Only the identity header makes the request a service call, and without it the door is never
    entered (audit 2026-07-15)."""
    with pytest.raises(UnauthenticatedError, match="Missing bearer token"):
        _authenticate(_settings(), token=SHARED, identity=None)


# --------------------------------------------------------------------------- #
# the refusals, rendered in lineage's problem vocabulary
# --------------------------------------------------------------------------- #


def test_an_unlisted_subject_is_a_403_not_a_401() -> None:
    """The allowlist answers "may this SUBJECT use the door". Asserting the TYPE of refusal is what
    distinguishes "you are barred" from "we did not recognise you"."""
    with pytest.raises(PermissionDeniedError, match="not allowed"):
        _authenticate(_settings(), token=SHARED, identity="alice")


def test_the_shared_token_cannot_claim_a_privileged_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ESCALATION. The shared app token sits in the env of seven sidecar-less web pods; with
    `service-trainer` privileged, presenting it while claiming to be the trainer is refused even
    though the subject is allowlisted and the token is perfectly valid for what it IS."""
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    with pytest.raises(UnauthenticatedError, match="may not claim"):
        _authenticate(_settings(privileged="service-trainer"), token=SHARED, identity="service-trainer")


def test_the_privileged_subject_is_admitted_with_ITS_OWN_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The legitimate caller is unaffected — the control binds the identity to a credential, it does
    not remove it. This is the half §2.8 broke on the catalog side: with no resolver passed, the
    privileged branch refused here too, no matter what was seeded."""
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    principal = _authenticate(_settings(privileged="service-trainer"), token=TRAINER_OWN, identity="service-trainer")

    assert principal is not None and principal.sub == "service-trainer"
    assert isinstance(principal, security.ServicePrincipal)


def test_an_UNPRIVILEGED_subject_still_uses_the_shared_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`service-web` is what the seven zones legitimately are, and they cannot reach a secret store
    at all. Forcing a dedicated credential on them would break every anonymous page load's read-only
    lineage feed, so the shared token stays valid for the read tier."""
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    principal = _authenticate(_settings(privileged="service-trainer"), token=SHARED, identity="service-web")

    assert principal is not None and principal.sub == "service-web"


def test_an_unprovisioned_privileged_credential_FAILS_CLOSED(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falling back to the shared token would restore the escalation while LOOKING configured — the
    worst outcome, because the config then claims a protection it does not have."""
    _seed_store(monkeypatch, {"unrelated": "field"})

    with pytest.raises(UnauthenticatedError, match="no dedicated credential"):
        _authenticate(_settings(privileged="service-trainer"), token=SHARED, identity="service-trainer")


def test_an_UNREADABLE_store_is_a_503_not_a_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The absent-vs-unreadable rule, through the real door: `fetch_dapr_secret` returns {} both when the store
    is down and when the bundle is empty, and both used to produce the identical 401. An outage must
    say outage — the absent-vs-unreadable rule the estate enforces in the state plane."""
    _seed_store(monkeypatch, {})

    with pytest.raises(ServiceUnavailableError, match="unreadable"):
        _authenticate(_settings(privileged="service-trainer"), token=TRAINER_OWN, identity="service-trainer")


# --------------------------------------------------------------------------- #
# the rule that must not have loosened
# --------------------------------------------------------------------------- #


def test_the_public_front_door_still_cannot_mint_a_service_principal() -> None:
    """The laundering path stays SHUT and is refused BEFORE the door: the gateway forwards through
    Dapr service invocation and the callee's daprd stamps a valid `dapr-api-token` on the way in, so
    an anonymous public request can arrive holding the estate's service credential while naming an
    allowlisted subject itself."""
    with pytest.raises(PermissionDeniedError, match="public front door"):
        _authenticate(_settings(), token=SHARED, identity="service-trainer", caller="gateway")


def test_oidc_off_is_still_the_open_dev_default() -> None:
    """Unchanged: with OIDC disabled the whole function is a no-op and every route stays open."""
    settings = LineageSettings.model_validate({"service_subjects": "service-trainer"})

    assert _authenticate(settings, token=SHARED, identity="service-trainer") is None
