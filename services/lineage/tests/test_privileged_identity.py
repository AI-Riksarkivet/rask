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
from lance_namespace import PermissionDeniedError

from lineage.api.security import _service_principal
from lineage.core.config import LineageSettings
from service_kit.governed import dapr_auth


SHARED = "the-shared-dapr-app-token"


def _settings(*, privileged: str = "") -> LineageSettings:
    return LineageSettings(
        LINEAGE_SERVICE_SUBJECTS="service-trainer,service-web",
        LINEAGE_PRIVILEGED_SUBJECTS=privileged,
    )


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
