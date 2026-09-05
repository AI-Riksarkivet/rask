"""A down secret store must not be reported as "the secret exists but is empty" (VS-11).

docs/DECISIONS.md "The Python estate audit" VS-11 — `_creds` wrapped `fetch_dapr_secret` in try/except and raised a
"could not be read" 503 from the handler. But `fetch_dapr_secret` NEVER raises: it swallows
every failure internally and returns `{}` (service_kit/governed/secrets.py), so the handler
was unreachable dead code and every outage fell through to the other branch, whose message
asserted `secret {name!r} exists but carries no access_key/secret_key pair`. Fail-closed
held (a 503 was still raised) — the defect is the false diagnosis: an operator chasing a
down sidecar was told the secret was seeded empty.

Pinned here: the miss is detected from the empty-dict return (the only signal the fetch
gives), and the 503 detail claims neither that the secret exists nor that it is empty —
only that the credentials could not be read.
"""

from __future__ import annotations

import pytest

from service_kit.exceptions import ServiceUnavailableError
from viewer.api.v1.endpoints import objects as objects_ep


@pytest.fixture(autouse=True)
def _uncached() -> None:
    """`_creds` is lru_cached; a stale success from another test must not mask the miss."""
    objects_ep._creds.cache_clear()


def test_a_down_store_does_not_claim_the_secret_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects_ep, "fetch_dapr_secret", lambda *_a, **_kw: {})

    with pytest.raises(ServiceUnavailableError) as exc_info:
        objects_ep._creds("ext-store-secret")

    detail = str(exc_info.value)
    assert "exists" not in detail, (
        f"the 503 asserts the secret exists — but fetch_dapr_secret returns {{}} for a DOWN store "
        f"and an empty secret alike, so the message diagnoses a state it cannot know: {detail!r}"
    )
    assert "ext-store-secret" in detail, f"the 503 should still name the secret: {detail!r}"


def test_a_partial_secret_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundle missing either half of the pair is as unusable as no bundle at all."""
    monkeypatch.setattr(objects_ep, "fetch_dapr_secret", lambda *_a, **_kw: {"access_key": "ak"})

    with pytest.raises(ServiceUnavailableError):
        objects_ep._creds("ext-store-secret")


def test_a_seeded_secret_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects_ep, "fetch_dapr_secret", lambda *_a, **_kw: {"access_key": "ak", "secret_key": "sk"})

    assert objects_ep._creds("ext-store-secret") == ("ak", "sk")
