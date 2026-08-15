"""The catalog call site gives the SAME answers as lineage, in its own problem vocabulary.

open_dapr.md §2.8 left two service doors running. `tests/unit/test_catalog_gateway_proxied_human.py`
already pins the half that mattered most here — that a gateway-proxied HUMAN authenticates as the
human, and that the privileged branch opens now that the resolver is passed. What this module pins is
the half that only becomes visible once lineage's fork is deleted: the two call sites must answer the
same request the same way.

Two behaviours changed on this side to make that true, and both are tested below:

  * an unconfigured door (`APP_API_TOKEN` unset) used to be swallowed and re-asked as OIDC, so an
    operator who set the allowlist and forgot the token was told "Missing bearer token". It is now
    the refusal lineage always gave, and it names the missing knob;
  * `settings.service_subjects` was a PRE-GATE on the branch, so an empty allowlist meant a caller
    who explicitly asked to be a service was silently re-asked for a bearer, while lineage (no such
    pre-gate) refused. The allowlist is now checked in exactly one place, for both services.

Neither loosens anything: OIDC admits only a valid bearer either way, so no caller is admitted who
was not admitted before.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api import security
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError

from service_kit.governed import dapr_auth
from service_kit.governed.oidc import IDToken


SHARED = "the-shared-dapr-app-token"
TRAINER_OWN = "the-trainers-own-credential"
_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"


def _settings(**over: Any) -> Any:
    """Structural settings — `authenticate` reads only these."""
    base: dict[str, Any] = {
        "oidc_enabled": True,
        "oidc_audience": "lance-catalog",
        "service_subjects": "service-trainer,service-web",
        "privileged_subjects": "",
        "dapr_secret_store": "lance-secrets",
        "dapr_secret_key": "lance",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _request() -> Request:
    """A wired OIDC verifier, so a fall-through would end at 401 "Missing bearer token" rather than
    at the 503 an unwired verifier gives. Without it, "did it fall through?" is unanswerable."""
    verifier = SimpleNamespace(verify=lambda _t: IDToken(iss="https://dex.example/dex", sub=_SUB, aud="lance-catalog", iat=0, exp=1 << 31))
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=verifier))))


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


def _authenticate(settings: Any, *, token: str | None, identity: str | None, caller: str | None = "medallion-producer") -> IDToken | None:
    return security.authenticate(
        _request(),
        settings,
        None,
        dapr_api_token=token,
        x_lance_service_identity=identity,
        dapr_caller_app_id=caller,
    )


# --------------------------------------------------------------------------- #
# THE UNIFIED NO-CREDENTIAL ANSWER
# --------------------------------------------------------------------------- #


def test_an_unconfigured_door_is_a_401_that_NAMES_ITSELF(monkeypatch: pytest.MonkeyPatch) -> None:
    """This used to `pass` and re-ask OIDC. The caller sent both service headers — the gateway strips
    them and the zones' BFF sends them only when there is no session — so they asked to be a service.
    Answering "Missing bearer token" sent operators to the IdP to debug a missing env var."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with pytest.raises(UnauthenticatedError, match="APP_API_TOKEN"):
        _authenticate(_settings(), token=SHARED, identity="service-trainer")


def test_an_EMPTY_allowlist_refuses_at_the_door_instead_of_re_asking_for_a_bearer() -> None:
    """The pre-gate removal. `service_subjects` empty is the default, and it used to skip the branch
    entirely here while lineage refused the identical request — same headers, same config, two
    answers. The door is SHUT either way; what changed is that both services now say so."""
    with pytest.raises(PermissionDeniedError, match="not allowed"):
        _authenticate(_settings(service_subjects=""), token=SHARED, identity="service-trainer")


def test_a_request_that_did_NOT_ask_for_the_door_still_authenticates_as_a_human() -> None:
    """The regression that must not come back with the pre-gate: only the identity header makes a
    request a service call. With `dapr.io/app-token-secret` set daprd stamps `dapr-api-token` on
    every request it delivers, so a proxied human arrives holding it and must still be themselves."""
    token = security.authenticate(
        _request(),
        _settings(),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="a.real.jwt"),
        dapr_api_token=SHARED,
        x_lance_service_identity=None,
        dapr_caller_app_id="gateway",
    )

    assert token is not None and token.sub == _SUB
    assert token.iss == "https://dex.example/dex", "a human, not the synthetic service-door principal"


# --------------------------------------------------------------------------- #
# the refusals, rendered as problem+json rather than a bare HTTPException
# --------------------------------------------------------------------------- #


def test_an_unlisted_subject_is_a_403_in_the_catalogs_own_vocabulary() -> None:
    """The shared door raises its own neutral types precisely so this renders through
    `service_kit.lakehouse.ns_errors` — a `fastapi.HTTPException` would escape those handlers as a
    bare `{"detail": ...}` body, contradicting this module's own docstring."""
    with pytest.raises(PermissionDeniedError, match="not allowed"):
        _authenticate(_settings(), token=SHARED, identity="alice")


def test_a_wrong_service_token_is_a_401_in_the_catalogs_own_vocabulary() -> None:
    with pytest.raises(UnauthenticatedError, match="invalid service token"):
        _authenticate(_settings(), token="wrong", identity="service-trainer")


def test_the_shared_token_cannot_claim_a_privileged_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    with pytest.raises(UnauthenticatedError, match="may not claim"):
        _authenticate(_settings(privileged_subjects="service-trainer"), token=SHARED, identity="service-trainer")


def test_the_privileged_subject_is_admitted_with_ITS_OWN_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.8's original defect was one missing kwarg at THIS call site. Kept alongside the lineage
    equivalent so a regression on either side is visible from its own suite."""
    _seed_store(monkeypatch, {"service-token-service-trainer": TRAINER_OWN})

    token = _authenticate(_settings(privileged_subjects="service-trainer"), token=TRAINER_OWN, identity="service-trainer")

    assert token is not None and token.sub == "service-trainer"
    assert token.iss == "rask://service-door", "the synthetic principal must never look like a human login"


def test_an_UNREADABLE_store_is_a_503_not_a_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_dapr.md §2.17: an outage must say outage, never the same 401 a misconfigured subject gets."""
    _seed_store(monkeypatch, {})

    with pytest.raises(ServiceUnavailableError, match="unreadable"):
        _authenticate(_settings(privileged_subjects="service-trainer"), token=TRAINER_OWN, identity="service-trainer")


def test_the_public_front_door_still_cannot_mint_a_service_principal() -> None:
    """Unchanged and checked here too, because the pre-gate removal widened the branch: an empty
    allowlist now enters it, so the laundering refusal must still fire first."""
    with pytest.raises(PermissionDeniedError, match="public front door"):
        _authenticate(_settings(service_subjects=""), token=SHARED, identity="service-trainer", caller="gateway")
