"""The Dapr door's expected token can come from the secret store, so it need not come from env.

[[LH-160]]. The estate rule is verbatim: *"Never secret through envs. Either from ESO, secret store
dapr and STS for zero trust."* `APP_API_TOKEN` is delivered to eleven deployments as a `secretKeyRef`,
which is a Kubernetes Secret injected as an environment variable — the banned path — and it could not
simply be deleted, because `DaprDoorSettings()` is constructed per request and reads the environment
each time, so nothing a lifespan resolved could ever reach the door.

THE ACCESSOR IS THE WHOLE BLOCKER, and it is one mode switch: `RASK_APP_TOKEN_FROM_STORE` points the
resolver at the bundle the pod's own sidecar already serves. A pod with a sidecar needs no new
plumbing for this — `lance-secrets` is scoped to every app-id that guards a Dapr door (measured
2026-09-15 against the live Component).

WHY A MODE AND NOT A FALLBACK CHAIN. Reading env when the store misses would mean a store outage
silently promotes a stale environment value back to authoritative — the precise failure the rule is
written against. On, the store is the sole source; off, env is. `test_the_store_is_the_SOLE_source`
is the one that would go green under a chain, so it is the one that matters here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from lance_namespace import PermissionDeniedError, ServiceUnavailableError

from service_kit.governed import dapr_auth
from service_kit.governed.dapr_auth import assert_app_token_configured, expected_app_token, require_dapr_token


@pytest.fixture(autouse=True)
def _a_cold_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The bundle is cached for the process lifetime, so each test starts from an unread store.

    Without this the SECOND test in the file would assert against the FIRST one's bundle and pass no
    matter what the resolver does — the hollow-test shape the estate has already paid for once.
    """
    monkeypatch.setenv("RASK_APP_TOKEN_FROM_STORE", "true")
    monkeypatch.delenv("RASK_ALLOW_UNAUTHENTICATED_DAPR", raising=False)
    dapr_auth._secret_bundle.cache_clear()
    yield
    dapr_auth._secret_bundle.cache_clear()


def _store(monkeypatch: pytest.MonkeyPatch, bundle: dict[str, str] | None) -> list[tuple[str, str]]:
    """Stand in for the sidecar's secret store; returns the calls it received.

    Patched at `service_kit.governed.secrets.fetch_dapr_secret` rather than on `dapr_auth`, because
    that is where `_secret_bundle` imports it from at call time — patching the consuming module would
    leave the real fetch in place and every test here would fail against a store that is not there.
    """
    calls: list[tuple[str, str]] = []

    def _fetch(store: str, key: str, **_: object) -> dict[str, str]:
        calls.append((store, key))
        return bundle if bundle is not None else {}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fetch)
    return calls


def test_the_door_authenticates_against_the_STORE(monkeypatch: pytest.MonkeyPatch) -> None:
    """The delivery the sidecar signs with the stored token is admitted, with nothing in env."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    _store(monkeypatch, {"dapr-app-token": "from-the-store"})

    require_dapr_token(dapr_api_token="from-the-store", dapr_caller_app_id="")

    with pytest.raises(PermissionDeniedError, match="invalid or missing"):
        require_dapr_token(dapr_api_token="not-the-stored-token", dapr_caller_app_id="")


def test_the_store_is_the_SOLE_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """In store mode the environment is not consulted — not even as a fallback.

    The two values are deliberately BOTH present and different: a resolver that preferred env, or
    that tried env after a store miss, admits `from-the-env` and this test is the only thing that
    would notice.
    """
    monkeypatch.setenv("APP_API_TOKEN", "from-the-env")
    _store(monkeypatch, {"dapr-app-token": "from-the-store"})

    with pytest.raises(PermissionDeniedError, match="invalid or missing"):
        require_dapr_token(dapr_api_token="from-the-env", dapr_caller_app_id="")

    require_dapr_token(dapr_api_token="from-the-store", dapr_caller_app_id="")


def test_a_bundle_without_the_field_refuses_rather_than_falling_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fallback-chain case, and the only shape that exposes one.

    A chain is invisible while the store answers — it only fires on a MISS — so a test where the
    bundle carries the field proves nothing about it. Here the store answers without the field and
    the environment holds a perfectly good token: a resolver ending `... or door.app_api_token`
    admits `from-the-env`, and nothing else in this file notices.

    Refusing is also the honest answer operationally: the deployment asked for the store, so a bundle
    that does not carry the token is a seeding defect, and authenticating against a value the
    operator believes they removed is how a "migrated" service stays on the banned path unnoticed.
    """
    monkeypatch.setenv("APP_API_TOKEN", "from-the-env")
    _store(monkeypatch, {"catalog-s3-secret-key": "irrelevant"})

    with pytest.raises(PermissionDeniedError, match="not configured"):
        require_dapr_token(dapr_api_token="from-the-env", dapr_caller_app_id="")
    with pytest.raises(PermissionDeniedError, match="not configured"):
        require_dapr_token(dapr_api_token="anything", dapr_caller_app_id="")


def test_an_UNREADABLE_store_is_an_outage_not_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """503, not 403 — and never an open door.

    The distinction is operational rather than cosmetic: a 403 tells the sidecar its credential was
    rejected, which is a permanent answer it should stop retrying; a 503 tells it to come back. The
    estate's absent-vs-unreadable rule, at the one seam where getting it wrong drops deliveries.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    _store(monkeypatch, None)

    with pytest.raises(ServiceUnavailableError):
        require_dapr_token(dapr_api_token="anything", dapr_caller_app_id="")


def test_the_store_is_read_ONCE_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every guarded request resolves this, so an uncached read would put a sidecar hop on each one."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    calls = _store(monkeypatch, {"dapr-app-token": "from-the-store"})

    for _ in range(5):
        require_dapr_token(dapr_api_token="from-the-store", dapr_caller_app_id="")

    assert calls == [("lance-secrets", "lance")], f"the store was read {len(calls)} times"


def test_the_public_front_door_is_refused_before_the_store_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller check runs first, so a forged front-door invocation costs no secret-store hop.

    It also means the front-door refusal survives a store outage — the one guard that must never
    depend on anything that can be unavailable.
    """
    calls = _store(monkeypatch, None)

    with pytest.raises(PermissionDeniedError, match="public front door"):
        require_dapr_token(dapr_api_token="anything", dapr_caller_app_id="gateway")

    assert calls == []


def test_the_boot_assertion_reads_the_store_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service that asserts at boot must assert against the source it will authenticate against.

    Reading env here while the door reads the store would let a store-mode pod pass its own startup
    check and then refuse every delivery it receives.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    _store(monkeypatch, {"dapr-app-token": "from-the-store"})

    assert_app_token_configured(dapr_enabled=True)

    dapr_auth._secret_bundle.cache_clear()
    _store(monkeypatch, {"catalog-s3-secret-key": "irrelevant"})
    with pytest.raises(RuntimeError, match="failing closed"):
        assert_app_token_configured(dapr_enabled=True)


def test_env_mode_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The eleven deployments still on env must not change behaviour when this ships.

    The store stub raises if it is reached, so this also pins that env mode issues NO sidecar call —
    a resolver that read the store first and fell back would be caught here rather than in production.
    """
    monkeypatch.setenv("RASK_APP_TOKEN_FROM_STORE", "false")
    monkeypatch.setenv("APP_API_TOKEN", "from-the-env")

    def _never(*_: object, **__: object) -> dict[str, str]:
        raise AssertionError("env mode must not touch the secret store")

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _never)

    assert expected_app_token() == "from-the-env"
    require_dapr_token(dapr_api_token="from-the-env", dapr_caller_app_id="")


def test_a_LATE_seed_heals_without_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bundle is cached, and a cached successful MISS would outlive the thing that caused it.

    The live ordering makes this reachable rather than theoretical: the chart's OpenBao seed Job and
    this pod roll in the same `helm upgrade`, so the app can read the bundle before the token is
    written to it. An unreadable store already heals by itself, because `_secret_bundle` never caches
    an exception — a store that ANSWERS without the field is the case that does not, and it would
    refuse every delivery for the life of the process while the field sat there.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    bundle: dict[str, str] = {"catalog-s3-secret-key": "irrelevant"}
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: dict(bundle))

    with pytest.raises(PermissionDeniedError, match="not configured"):
        require_dapr_token(dapr_api_token="seeded-later", dapr_caller_app_id="")

    bundle["dapr-app-token"] = "seeded-later"

    require_dapr_token(dapr_api_token="seeded-later", dapr_caller_app_id="")
