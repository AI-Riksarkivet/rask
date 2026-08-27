"""An `http://` issuer must fail at STARTUP, not as a 401 on every request.

open_fastapi-audit — "An `http://` OIDC issuer in production surfaces as an opaque 401 on every
request, and the 'logged distinctly' the code promises does not exist".

`_require_https` raises `UnauthenticatedError`, which every call site maps to 401 "Invalid or expired
token". So an operator who ships `LANCE_OIDC_ISSUER=http://…` without `LANCE_OIDC_ALLOW_INSECURE` gets
a service where every VALID bearer answers 401 with the same body a genuinely expired token gets —
and the two are indistinguishable to the caller.

THE COMMENT CLAIMED OTHERWISE. It said the failure is "surfaced at verify time as an opaque auth
failure but logged distinctly", and `oidc.py` imported no logging at all. A comment asserting a
behaviour the code does not have is worse than none: it is what an operator reads instead of the logs
that were never written.

WHERE IT BELONGS. `GovernedAuthSettings._validate_governed_auth` already exists to "fail fast at
construction rather than fail open at request time", and already validates the adjacent invariants —
issuer and audience present when OIDC is on, OIDC on when FGA is on. A scheme is knowable at exactly
the same moment.

`_require_https` STAYS as the runtime backstop: `jwks_uri` comes from discovery, so the settings
cannot see it, and that path can still only be caught at verify time. It gets the log line its comment
always promised.
"""

from __future__ import annotations

import logging

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import GovernedAuthSettings


class _Governed(BaseSettings, GovernedAuthSettings):
    """A minimal concrete carrier for the mixin.

    `GovernedAuthSettings` is deliberately a plain mixin rather than a settings class of its own, so a
    test cannot instantiate it directly. Mixing it into a bare `BaseSettings` exercises the validator
    without dragging in one service's whole config — and keeps this a test of the SHARED rule rather
    than of whichever service happened to be imported.
    """

    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore")


def _settings(**over: object) -> _Governed:
    base = {
        "LANCE_OIDC_ENABLED": True,
        "LANCE_OIDC_ISSUER": "https://issuer.test",
        "LANCE_OIDC_AUDIENCE": "rask",
    }
    return _Governed.model_validate({**base, **over})


def test_an_http_issuer_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        _settings(LANCE_OIDC_ISSUER="http://issuer.test")


def test_the_dev_escape_hatch_still_works() -> None:
    """A local Dex over http is the case the flag exists for; this must not become a hard ban."""
    settings = _settings(LANCE_OIDC_ISSUER="http://dex.local:5556", LANCE_OIDC_ALLOW_INSECURE=True)
    assert settings.oidc_issuer == "http://dex.local:5556"


def test_https_is_unaffected() -> None:
    assert _settings().oidc_issuer == "https://issuer.test"


def test_the_check_is_skipped_when_oidc_is_off() -> None:
    """An issuer nobody verifies against is not a misconfiguration."""
    assert _Governed.model_validate({"LANCE_OIDC_ENABLED": False, "LANCE_OIDC_ISSUER": "http://x"}) is not None


def test_the_runtime_backstop_logs_what_its_comment_promises(caplog: pytest.LogCaptureFixture) -> None:
    """`jwks_uri` comes from DISCOVERY, so settings cannot see it and only verify time can catch it.
    That path keeps the check — and now actually logs, which is what the comment always claimed."""
    from service_kit.governed import oidc

    with caplog.at_level(logging.WARNING), pytest.raises(Exception, match="HTTPS"):
        oidc._require_https("http://idp.internal/jwks", label="jwks_uri", allow_insecure=False)

    assert "oidc_insecure_url" in caplog.text, (
        "the comment says the failure is 'logged distinctly' and nothing was logged — an operator sees only a 401 identical to an expired token"
    )


def test_the_catalog_twin_refuses_it_too() -> None:
    """The catalog still declares these knobs INLINE and predates the shared mixin.

    `governed/settings.py`'s own docstring makes the contract explicit: the aliases are byte-identical
    so one set of `LANCE_*` variables configures every service, and "adding a field here without adding
    it there would silently split the contract". A rule enforced in one of the two is exactly that
    split — the catalog is the service that OWNS the tuple store, so it is the worst one to leave
    accepting an issuer nobody else will.
    """
    from catalog.core.config import Settings as CatalogSettings

    base = {
        "LANCE_REST_IMPL": "dir",
        "LANCE_OIDC_ENABLED": True,
        "LANCE_OIDC_AUDIENCE": "rask",
        "LANCE_S3_ACCESS_KEY_ID": "k",
        "LANCE_S3_SECRET_ACCESS_KEY": "s",
    }
    with pytest.raises(ValueError, match="HTTPS"):
        CatalogSettings.model_validate({**base, "LANCE_OIDC_ISSUER": "http://issuer.test"})

    ok = CatalogSettings.model_validate({**base, "LANCE_OIDC_ISSUER": "http://dex.local:5556", "LANCE_OIDC_ALLOW_INSECURE": True})
    assert ok.oidc_issuer == "http://dex.local:5556"
