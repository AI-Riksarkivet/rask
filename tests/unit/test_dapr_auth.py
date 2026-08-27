"""Unit tests for ``service_kit.governed.dapr_auth`` — the guard on every sidecar-delivered route.

The app-api-token check is the ONLY auth layer on those routes that survives a gateway swap
(the nginx 403s are replaceable edge config), so its contract is pinned here: constant-time
bytes compare (a non-ASCII header is a clean 403, never a ``TypeError``), the open dev
default when the token is unset, and the fail-closed startup assert — including the lineage
coupling where the cron reconcile binding ALONE (pub/sub ingest off) must still demand the
token, since the flags can diverge (the audit's mount/assert decoupling hole).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from lance_namespace import PermissionDeniedError
from lineage.core.config import LineageSettings

from service_kit.governed.dapr_auth import assert_app_token_configured, require_dapr_token
from service_kit.lakehouse.ns_errors import problem_detail


# --------------------------------------------------------------------------- #
# require_dapr_token — the per-request header check.
# --------------------------------------------------------------------------- #


def test_open_when_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: no APP_API_TOKEN = the open dev default — any (or no) header passes.
    ``assert_app_token_configured`` makes that a startup error once a sidecar route mounts."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    require_dapr_token("anything")
    require_dapr_token(None)


def test_matching_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    require_dapr_token("s3cret")


@pytest.mark.parametrize("presented", ["wrong", "", None, "s3cret ", "s3cre"])
def test_mismatch_or_missing_header_is_403(monkeypatch: pytest.MonkeyPatch, presented: str | None) -> None:
    """CONTRACT: with the token set, any non-matching/absent ``dapr-api-token`` header is a 403."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    # `PermissionDeniedError`, not `HTTPException`: the guard raises a DOMAIN error now so its 403
    # wears the same problem+json envelope as every other refusal (open_fastapi-audit). The status is
    # still 403 — asserted through the translator that renders it, which is what a caller actually sees.
    with pytest.raises(PermissionDeniedError):
        require_dapr_token(presented)
    assert problem_detail(PermissionDeniedError("x"))[0] == 403


def test_non_ascii_header_is_a_clean_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: the compare is over BYTES — ``secrets.compare_digest`` on str raises ``TypeError``
    for non-ASCII, which would turn an attacker-controlled header into a 500 instead of a 403."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with pytest.raises(PermissionDeniedError):
        require_dapr_token("sécrét")
    assert problem_detail(PermissionDeniedError("x"))[0] == 403


# --------------------------------------------------------------------------- #
# assert_app_token_configured — the fail-closed startup assert.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", [None, ""])
def test_assert_fails_closed_when_enabled_and_unset(monkeypatch: pytest.MonkeyPatch, blank: str | None) -> None:
    """CONTRACT: Dapr delivery on + unset/blank token = refuse to start (never an open route)."""
    if blank is None:
        monkeypatch.delenv("APP_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("APP_API_TOKEN", blank)
    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        assert_app_token_configured(dapr_enabled=True)


def test_assert_noop_when_disabled_or_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    assert_app_token_configured(dapr_enabled=False)  # dev: no sidecar route mounts
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    assert_app_token_configured(dapr_enabled=True)


def test_lineage_boot_fails_when_only_the_reconcile_route_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACT: the lineage assert covers ANY sidecar-delivered mount — the cron reconcile binding
    alone (pub/sub ingest OFF) must still refuse to boot without the token, else the flag divergence
    leaves a token-less graph-mutating route (the audit's mount/assert decoupling hole). The assert
    runs before the pool opens, so this is infra-free."""
    from lineage import main as lineage_main

    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    # Keep it infra-free EVEN UNDER the regression: the assert must fire before the pool opens, so make
    # make_pool blow up loudly if it's ever reached — else a regressed assert would fall through to a
    # real DB connect and hang ~30s on PoolTimeout (or hit a live dev DB) instead of failing clean.
    def _no_pool(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("make_pool reached — the boot app-token assert did not fire first")

    monkeypatch.setattr(lineage_main, "make_pool", _no_pool)
    settings = LineageSettings.model_validate({"reconcile_binding_name": "lineage-reconcile-cron"})
    assert not settings.dapr_enabled  # the divergence under test: binding on, pub/sub ingest off
    monkeypatch.setattr(lineage_main, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        asyncio.run(lineage_main.lifespan(lineage_main.app).__aenter__())


def test_the_privileged_door_OPENS_with_the_shared_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """`service_principal` grew `dedicated_token=` for the privileged branch, but
    the catalog never passed a resolver — so its privileged door hard-refused every privileged
    subject with 'no dedicated credential provisioned' no matter what the store held. This pins the
    shared resolver end-to-end: the seeded credential admits its holder, the shared token cannot
    claim the subject, and an unreadable store is a SecretStoreUnreadable (503 at the caller), never
    a 401."""
    from service_kit.governed import dapr_auth
    from service_kit.governed.dapr_auth import SecretStoreUnreadable, dedicated_token_from_store, service_principal

    monkeypatch.setenv("APP_API_TOKEN", "shared-token")
    dapr_auth._secret_bundle.cache_clear()
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_dapr_secret",
        lambda *_a, **_k: {"service-token-service-trainer": "trainer-own"},
    )
    resolver = dedicated_token_from_store("lance-secrets", "lance")

    admitted = service_principal(
        token="trainer-own",
        identity="service-trainer",
        allowed_subjects="service-trainer",
        privileged_subjects="service-trainer",
        dedicated_token=resolver,
    )
    assert admitted.sub == "service-trainer"

    with pytest.raises(Exception, match="may not claim"):
        service_principal(
            token="shared-token",
            identity="service-trainer",
            allowed_subjects="service-trainer",
            privileged_subjects="service-trainer",
            dedicated_token=resolver,
        )

    dapr_auth._secret_bundle.cache_clear()
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: {})
    with pytest.raises(SecretStoreUnreadable):
        service_principal(
            token="trainer-own",
            identity="service-trainer",
            allowed_subjects="service-trainer",
            privileged_subjects="service-trainer",
            dedicated_token=dedicated_token_from_store("lance-secrets", "lance"),
        )
    dapr_auth._secret_bundle.cache_clear()
