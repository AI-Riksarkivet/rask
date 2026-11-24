"""The three postures the estate's governed doors actually have, as PARAMETERS of one helper.

`attach_auth` was extracted with exactly one posture — provision-if-unpinned, log-and-continue — and
that is why ten hand-rolled copies survived it: the copies are not stylistic variants, they encode
decisions the single-posture helper cannot express.

  * **Provisioning.** `maintenance` must NEVER author an authorization model. A data
    writer that mints a store becomes the source of truth for everyone else's permissions. It
    resolves read-only (`fga_provision.resolve`) and fail closed when the estate is not bootstrapped.
  * **Fatality.** `catalog` and `lineage` build with no `try` at all, so a failed
    build CRASHES the pod. That is deliberate and visible. Collapsing them onto a swallowing helper
    would silently convert CrashLoopBackOff into a fleet of pods serving 503 — the same estate, minus
    the only signal anybody watches.

So these are asserted on the helper rather than remembered per call site, because "the copies differ"
is the finding and "the helper cannot express the difference" is the reason the copies exist.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.governed import fga_provision
from lakehouse.governed import oidc as oidc_mod
from lakehouse.governed.auth_lifespan import BootFailure, ModelRole, attach_auth, build_fga_client
from lakehouse.governed.settings import GovernedAuthSettings
from lakehouse.process.supervision import Supervisor


class _ServiceSettings(GovernedAuthSettings, BaseSettings):
    """A service's settings as each service composes them: the estate's auth mixins on a settings base."""

    model_config = SettingsConfigDict(validate_by_name=True, validate_by_alias=True, extra="ignore")


#: A person issuer reached in-cluster at its own discovery URL, and the OpenFGA the store is looked up on.
_GOVERNED: dict[str, Any] = {
    "oidc_enabled": True,
    "oidc_issuer": "https://issuer.test",
    "oidc_audience": "rask",
    "oidc_discovery_url": "http://dex.internal/dex",
    "oidc_leeway": 30,
    "oidc_idp_id": "dex",
    "oidc_connectors": {"local": "dex"},
    "fga_enabled": True,
    "fga_api_url": "http://fga.test:8080",
}


def _settings(**fields: Any) -> _ServiceSettings:
    return _ServiceSettings.model_validate({**_GOVERNED, **fields})


class _Recorder:
    """Records which of the two store-lookup halves was taken. A mock that only stubs `resolve` would
    let a `provision` slip through unseen, and the write is the property under test."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kwargs: dict[str, dict[str, object]] = {}


@pytest.fixture
def fga_calls(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    async def provision(api_url: str, **kwargs: object) -> tuple[str, str]:
        rec.calls.append("provision")
        rec.kwargs["provision"] = kwargs
        return "01PROVISIONED", "01MODEL"

    async def resolve(api_url: str, **kwargs: object) -> tuple[str, str]:
        rec.calls.append("resolve")
        rec.kwargs["resolve"] = kwargs
        return ("01RESOLVED", "01MODEL")

    def make_client(api_url: str, store_id: str, model_id: str, **_: object) -> object:
        rec.calls.append(f"make_client:{store_id}")
        return object()

    monkeypatch.setattr(fga_provision, "provision", provision)
    monkeypatch.setattr(fga_provision, "resolve", resolve)
    monkeypatch.setattr(fga_provision, "make_client", make_client)

    # A stand-in with the methods the startup path calls on it. `attach_auth` fetches the verifier's keys
    # so a misconfigured issuer is in the pod's log at boot rather than in a user's failed request, and
    # keeps them fetched on a background loop; a bare `object()` cannot answer either — these postures are
    # about the FGA half, so the OIDC half has to get far enough out of the way to reach it.
    class _WarmVerifier:
        def refresh(self) -> list[tuple[str, str]]:
            return []

        async def refresh_forever(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(oidc_mod, "OIDCVerifier", lambda *a, **k: _WarmVerifier())
    return rec


@pytest.mark.asyncio
async def test_the_default_posture_RESOLVES_when_unpinned(fga_calls: _Recorder) -> None:
    """Behavioural baseline for every service but the ONE bootstrap — saying nothing must not publish.

    `fga_provision.provision` writes a new immutable authorization model for a body the store lacks, so a default
    that provisions lets every image, older ones included, add its own body to the history. Measured live
    2026-09-09: 1,256 models in the store and three services carrying an older `model.fga` with no
    `maintainer` rung. The catalog opts in explicitly; everything else reads.
    """
    app = FastAPI()
    async with Supervisor() as loops:
        await attach_auth(app, _settings(), service="t", loops=loops)

    assert "provision" not in fga_calls.calls, "the DEFAULT posture published a model — boot order now decides the estate's permissions"
    assert fga_calls.calls == ["resolve", "make_client:01RESOLVED"]
    assert getattr(app.state, "fga", None) is not None
    assert getattr(app.state, "oidc", None) is not None


@pytest.mark.asyncio
async def test_a_reader_with_no_store_leaves_the_door_SHUT(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder, caplog: pytest.LogCaptureFixture) -> None:
    """An unbootstrapped estate must not be bootstrapped BY the reader. No client, so the gate 503s, and
    the warning names what `fga_provision.resolve` found missing."""

    async def resolve(api_url: str, **_: object) -> tuple[str, str]:
        fga_calls.calls.append("resolve")
        raise fga_provision.OpenFgaModelAbsentError(f"OpenFGA at {api_url} holds no store named 'lance-catalog'")

    monkeypatch.setattr(fga_provision, "resolve", resolve)
    app = FastAPI()
    with caplog.at_level(logging.WARNING):
        async with Supervisor() as loops:
            await attach_auth(app, _settings(), service="t", loops=loops, model_role=ModelRole.READER)

    assert getattr(app.state, "fga", None) is None
    assert not any(c.startswith("make_client") for c in fga_calls.calls)
    assert [getattr(r, "reason", None) for r in caplog.records if r.getMessage() == "openfga_unpinned"] == [
        "OpenFGA at http://fga.test:8080 holds no store named 'lance-catalog'; governed routes report unavailable"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("model_role", "lookup"), [(ModelRole.READER, "resolve"), (ModelRole.PUBLISHER, "provision")], ids=["resolve", "provision"])
async def test_a_pinned_store_reaches_the_lookup(fga_calls: _Recorder, model_role: ModelRole, lookup: str) -> None:
    """The model hook writes into a pinned store; a service that looked its store up by
    name instead would check against a different one ([[LH-201]])."""
    await build_fga_client(_settings(fga_store_id="01PINNED"), service="t", model_role=model_role)

    assert fga_calls.kwargs[lookup].get("store_id") == "01PINNED"


@pytest.mark.asyncio
async def test_a_crashing_boot_refuses_to_run_without_the_model_its_image_carries(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """A caller that reads a missing client as "authorization off" comes up open, so a fatal one (the
    catalog's cascade backfill) whose resolve finds no store, or no model its image carries, must crash
    instead ([[LH-201]])."""

    async def resolve(api_url: str, **_: object) -> tuple[str, str]:
        fga_calls.calls.append("resolve")
        raise fga_provision.OpenFgaModelAbsentError(f"store 01STORE at {api_url} holds no model carrying this image's model.json")

    monkeypatch.setattr(fga_provision, "resolve", resolve)

    with pytest.raises(fga_provision.OpenFgaModelAbsentError, match=r"holds no model carrying this image's model\.json"):
        await build_fga_client(_settings(), service="warehouse-grant-backfill", on_failure=BootFailure.CRASH)
    assert not any(c.startswith("make_client") for c in fga_calls.calls)


@pytest.mark.asyncio
async def test_a_crashing_boot_reraises_a_failed_fga_build(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """`catalog` and `lineage` crash on boot. A refactor that turns that
    into a logged line converts a CrashLoopBackOff an operator watches into a 503 nobody does."""

    async def boom(api_url: str, **_: object) -> tuple[str, str]:
        raise RuntimeError("openfga unreachable")

    # `resolve`, not `provision`: the default posture is read-only, so that is the call this failure
    # has to arrive on for the assertion to be about `on_failure` rather than about which half was patched.
    monkeypatch.setattr(fga_provision, "resolve", boom)
    app = FastAPI()
    with pytest.raises(RuntimeError, match="openfga unreachable"):
        async with Supervisor() as loops:
            await attach_auth(app, _settings(), service="t", loops=loops, on_failure=BootFailure.CRASH)


@pytest.mark.asyncio
async def test_a_crashing_boot_reraises_a_failed_verifier_build(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """The OIDC half of the same posture — those services wrap neither construction in a `try`."""

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("discovery refused")

    monkeypatch.setattr(oidc_mod, "OIDCVerifier", boom)
    app = FastAPI()
    with pytest.raises(RuntimeError, match="discovery refused"):
        async with Supervisor() as loops:
            await attach_auth(app, _settings(fga_enabled=False), service="t", loops=loops, on_failure=BootFailure.CRASH)


@pytest.mark.asyncio
async def test_the_default_stays_NON_fatal(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """The four swallowing services keep swallowing. A failure to BUILD leaves the attribute unset and
    the dependency answers 503 — never a permissive fallback, which would turn a broken authorization
    layer into an open one."""

    async def boom(api_url: str, **_: object) -> tuple[str, str]:
        raise RuntimeError("openfga unreachable")

    # The read half, for the same reason as the fatal case above: it is the one the default takes.
    monkeypatch.setattr(fga_provision, "resolve", boom)
    app = FastAPI()
    async with Supervisor() as loops:
        await attach_auth(app, _settings(), service="t", loops=loops)

    assert getattr(app.state, "fga", None) is None
    assert getattr(app.state, "oidc", None) is not None, "the OIDC half must build independently — one failure must not take the other down"


# ── the structured audit events survive the shared bootstrap (DUP-01 verify) ─────────────────────
#
# When the ten inline bootstraps collapsed onto this one helper, the first cut replaced their
# structured `log.info(event, extra=...)` events with printf message strings — silently narrowing
# telemetry: `openfga_provisioned` is a documented severity-9 INFO audit event (`lakehouse.process.log_handler`)
# that reaches OTLP at the default level, and maintenance's four `reconcile_fga_*` diagnostics carried
# `extra` payloads (store_id, the pin-for-production hint, the unpinned reason). A message string
# drops all of it. The single emitter must keep them structured — one emitter is the improvement,
# not fewer events.


@pytest.mark.asyncio
async def test_provisioning_emits_the_structured_audit_event(caplog: pytest.LogCaptureFixture, fga_calls: _Recorder) -> None:
    import logging

    with caplog.at_level(logging.INFO):
        await build_fga_client(_settings(), service="catalog", model_role=ModelRole.PUBLISHER)
    rec = next((r for r in caplog.records if r.getMessage() == "openfga_provisioned"), None)
    assert rec is not None, "openfga_provisioned is no longer emitted — obs.py raises it to OTLP as a severity-9 audit event"
    assert getattr(rec, "store_id", None) and getattr(rec, "model_id", None), "the event lost its store_id/model_id extra — a printf string would"
    assert getattr(rec, "service", None) == "catalog"


@pytest.mark.asyncio
async def test_resolve_by_name_emits_the_structured_diagnostic(caplog: pytest.LogCaptureFixture, fga_calls: _Recorder) -> None:
    with caplog.at_level(logging.INFO):
        await build_fga_client(_settings(), service="maintenance", model_role=ModelRole.READER)
    rec = next((r for r in caplog.records if r.getMessage() == "openfga_resolved_by_name"), None)
    assert rec is not None, "the unpinned resolve-by-name diagnostic (was maintenance's reconcile_fga_resolved_by_name) is gone"
    assert getattr(rec, "store_id", None) and getattr(rec, "model_id", None), "the diagnostic lost its store_id/model_id extra"
