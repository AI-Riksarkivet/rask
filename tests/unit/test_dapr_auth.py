"""Unit tests for ``lakehouse.governed.dapr_auth`` — the guard on every sidecar-delivered route.

The app-api-token check is the ONLY auth layer on those routes that survives a gateway swap
(the nginx 403s are replaceable edge config), so its contract is pinned here: constant-time
bytes compare (a non-ASCII header is a clean 403, never a ``TypeError``), the open dev
default when the token is unset, and the fail-closed startup assert — including the lineage
coupling where the cron reconcile binding ALONE (pub/sub ingest off) must still demand the
token, since the flags can diverge (the audit's mount/assert decoupling hole).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import PermissionDeniedError

from lakehouse.governed.dapr_auth import assert_app_token_configured, require_dapr_token, sidecar_app
from lakehouse.ns_errors import install_problem_handlers
from lineage.core.config import LineageSettings


# --------------------------------------------------------------------------- #
# require_dapr_token — the per-request header check.
# --------------------------------------------------------------------------- #


def test_non_ascii_header_is_a_clean_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compare is over bytes: ``secrets.compare_digest`` on str raises ``TypeError`` for non-ASCII, which would turn
    an attacker-controlled header into a 500 instead of the 403 a ``PermissionDeniedError`` renders as."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with pytest.raises(PermissionDeniedError):
        require_dapr_token("sécrét")


def test_a_sidecar_apps_subscription_read_answers_the_sidecar_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list names the app's topics, components and delivery routes, and any pod that reaches the port can ask."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    sidecar_app(app).subscribe(pubsub="bus", topic="t", route="/t")(lambda: None)
    client = TestClient(app)

    answers = (client.get("/dapr/subscribe", headers={"dapr-api-token": "s3cret"}), client.get("/dapr/subscribe"))

    assert (answers[0].json(), answers[1].status_code) == ([{"pubsubname": "bus", "topic": "t", "route": "/t", "metadata": {}}], 403)
    assert "/dapr/subscribe" not in app.openapi()["paths"]


# --------------------------------------------------------------------------- #
# assert_app_token_configured — the fail-closed startup assert.
# --------------------------------------------------------------------------- #


def test_assert_noop_when_disabled_or_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    assert_app_token_configured(dapr_enabled=False)  # dev: no sidecar route mounts
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    assert_app_token_configured(dapr_enabled=True)


def test_lineage_boot_fails_when_only_the_relay_route_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONTRACT: the lineage assert covers ANY sidecar-delivered mount — the relay's cron binding
    alone (pub/sub ingest OFF) must still refuse to boot without the token, else the flag divergence
    leaves a token-less route that re-publishes the outbox (the audit's mount/assert decoupling hole).
    The assert runs before the store is built, so this is infra-free."""
    from lineage import main as lineage_main

    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    # Keep it infra-free EVEN UNDER the regression: the assert must fire before the store is built, so make
    # building it blow up loudly if it's ever reached — else a regressed assert would fall through to a
    # catalog connect and retry for its whole boot deadline instead of failing clean.
    def _no_store(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the provenance store was built — the boot app-token assert did not fire first")

    monkeypatch.setattr(lineage_main.ProvenanceStore, "from_settings", _no_store)
    settings = LineageSettings.model_validate({"relay_binding_name": "lineage-relay-cron"})
    assert not settings.dapr_enabled  # the divergence under test: binding on, pub/sub ingest off
    monkeypatch.setattr(lineage_main, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="APP_API_TOKEN"):
        asyncio.run(lineage_main.lifespan(lineage_main.app).__aenter__())
