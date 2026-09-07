"""Maintenance stops presenting the estate's shared bearer at the catalog's service door.

F2-3 / Q17-7's remainder. `service_headers` is the ONE builder both of maintenance's catalog doors
use — the compaction pair and credential vending. Vending is the dangerous one to leave behind,
because it reports any `>=400` as "vending unavailable" and returns `None`, so a 401 there does not
fail: it silently falls back to the configured S3 key. Three subjects still hold the shared
`APP_API_TOKEN`:
`service-maintenance`, `service-ingest` and `notifications`. This is the first of them.

WHY THE CLIENT HALF COMES FIRST, and it is not a preference. `dapr_auth.service_principal` binds a
PRIVILEGED subject to `service-token-<identity>` at the door, and refuses that name presented with
the shared token. So naming a subject privileged before it can present its own credential 401s it
outright — measured on the live estate 2026-08-26, when rendering the server-side expectation alone
broke every stage-runner call until it was reverted. And for a subject being ADDED to the privileged
set there is no safe gap in EITHER direction: the client half alone is inert (nothing yet demands
it), the server half alone is an outage. They land together; this file asserts the client half so the
pair can be completed in one change rather than discovered in production.

FALLBACK IS DELIBERATE AND IS NOT A WEAKENING. A store that has no entry for this identity resolves
to `None` and the caller presents the shared token, exactly as before — the DOOR remains the single
authority on whether that is acceptable, and it is what keeps a dev stack with no secret store
working. What must never happen is the opposite: silently downgrading when the store is UNREADABLE,
because "we could not read it" and "this identity is not privileged" are different answers and
conflating them is how a credential control becomes decorative.
"""

from __future__ import annotations

from typing import Any

import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import catalog_identity


def _settings(**overrides: Any) -> MaintenanceSettings:
    base: dict[str, Any] = {"catalog_service_identity": "service-maintenance", "secrets_from_dapr": True}
    return MaintenanceSettings(**(base | overrides))


def test_it_presents_its_OWN_credential_when_the_store_has_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the change: the token on the wire is this identity's, not the estate's shared one."""
    monkeypatch.setattr(
        catalog_identity,
        "dedicated_token_for",
        lambda _s: lambda identity: f"token-for-{identity}",
    )
    headers = catalog_identity.service_headers(_settings())
    assert headers["dapr-api-token"] == "token-for-service-maintenance"
    assert headers["x-lance-service-identity"] == "service-maintenance"


def test_it_falls_back_to_the_shared_token_when_the_store_has_no_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that is readable and simply has nothing for this identity is the UNPRIVILEGED case,
    which is every estate that has not turned this on. It must keep working unchanged."""
    monkeypatch.setattr(catalog_identity, "dedicated_token_for", lambda _s: lambda _identity: None)
    monkeypatch.setenv("APP_API_TOKEN", "the-shared-bearer")
    headers = catalog_identity.service_headers(_settings())
    assert headers["dapr-api-token"] == "the-shared-bearer"


def test_with_no_secret_store_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`secrets_from_dapr` off is a dev stack. It keeps the shared-token path exactly as it was, so
    this landing cannot break an estate that has no store to read."""
    monkeypatch.setenv("APP_API_TOKEN", "the-shared-bearer")
    headers = catalog_identity.service_headers(_settings(secrets_from_dapr=False))
    assert headers["dapr-api-token"] == "the-shared-bearer"


def test_an_UNREADABLE_store_raises_rather_than_downgrading(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DISTINCTION THAT MAKES THIS A CONTROL. Falling back when the store cannot be read would
    mean an outage in the secret store silently returns the whole plane to the shared bearer — the
    control disappearing exactly when something is already wrong."""

    def _explode(_identity: str) -> str | None:
        raise RuntimeError("secret store unreachable")

    monkeypatch.setattr(catalog_identity, "dedicated_token_for", lambda _s: _explode)
    monkeypatch.setenv("APP_API_TOKEN", "the-shared-bearer")
    with pytest.raises(RuntimeError, match="unreachable"):
        catalog_identity.service_headers(_settings())


def test_the_identity_header_is_always_sent() -> None:
    """Both halves of the identity or the door refuses with a reason invisible from here — the
    invariant the original `_headers` docstring states, kept through the change."""
    headers = catalog_identity.service_headers(_settings(secrets_from_dapr=False))
    assert headers["x-lance-service-identity"] == "service-maintenance"


def test_EVERY_door_maintenance_calls_uses_the_one_builder() -> None:
    """A credential control applied to one of two doors is not a control.

    Both sites stamped the same two headers from their own copy, and the copy that mattered most was
    the easiest to miss: `credentials.py` reports any `>=400` as "vending unavailable" and returns
    `None`, so a 401 there does not fail loudly — maintenance quietly falls back to its configured S3
    key with an `info` log. Discovered by grep rather than listed, so a THIRD door reds this instead
    of drifting.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src/maintenance"
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "x-lance-service-identity" not in text or path.name == "catalog_identity.py":
            continue
        if "service_headers" not in text:
            offenders.append(str(path.relative_to(src)))
    assert not offenders, (
        f"these build the identity headers themselves instead of calling service_headers: {offenders} — "
        "a dedicated credential applied to some doors and not others is not a credential control"
    )
