"""Unit tests for ``common.dapr_auth`` — the guard on every sidecar-delivered route.

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
from common.dapr_auth import assert_app_token_configured, require_dapr_token
from fastapi import HTTPException
from lineage.core.config import LineageSettings


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
    with pytest.raises(HTTPException) as exc:
        require_dapr_token(presented)
    assert exc.value.status_code == 403


def test_non_ascii_header_is_a_clean_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTRACT: the compare is over BYTES — ``secrets.compare_digest`` on str raises ``TypeError``
    for non-ASCII, which would turn an attacker-controlled header into a 500 instead of a 403."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as exc:
        require_dapr_token("sécrét")
    assert exc.value.status_code == 403


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
