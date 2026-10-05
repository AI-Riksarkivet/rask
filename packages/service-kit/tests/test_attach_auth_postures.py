"""The three postures the estate's governed doors actually have, as PARAMETERS of one helper.

`attach_auth` was extracted with exactly one posture — provision-if-unpinned, log-and-continue — and
that is why ten hand-rolled copies survived it: the copies are not stylistic variants, they encode
decisions the single-posture helper cannot express.

  * **Provisioning.** `ingest` and `maintenance` must NEVER author an authorization model. A data
    writer that mints a store becomes the source of truth for everyone else's permissions. They
    resolve read-only (`fga.resolve`) and fail closed when the estate is not bootstrapped.
  * **Fatality.** `catalog`, `lineage` and both medallion apps build with no `try` at all, so a failed
    build CRASHES the pod. That is deliberate and visible. Collapsing them onto a swallowing helper
    would silently convert CrashLoopBackOff into a fleet of pods serving 503 — the same estate, minus
    the only signal anybody watches.

So these are asserted on the helper rather than remembered per call site, because "the copies differ"
is the finding and "the helper cannot express the difference" is the reason the copies exist.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from service_kit.governed import fga as fga_mod
from service_kit.governed import oidc as oidc_mod
from service_kit.governed.auth_lifespan import attach_auth, build_fga_client


class _Settings(BaseModel):
    """A structural stand-in for `GovernedAuthSettings` — `attach_auth` takes a Protocol, so any
    service's own settings class satisfies it without importing a base."""

    oidc_enabled: bool = True
    oidc_issuer: str | None = "https://issuer.test"
    oidc_audience: str | None = "rask"
    oidc_discovery_url: str | None = "http://dex.internal/dex"
    oidc_cache_ttl: int = 300
    oidc_leeway: int = 30
    oidc_allow_insecure: bool = False
    sa_issuer: str | None = None
    sa_audience: str | None = None
    sa_subjects: dict[str, str] = {}
    sa_fetch_token_file: str | None = None
    sa_ca_file: str | None = None
    fga_enabled: bool = True
    fga_api_url: str = "http://fga.test:8080"
    fga_store_id: str | None = None
    fga_model_id: str | None = None
    fga_timeout_seconds: float = 5.0
    fga_token_file: str | None = None


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

    async def resolve(api_url: str, **kwargs: object) -> tuple[str, str] | None:
        rec.calls.append("resolve")
        rec.kwargs["resolve"] = kwargs
        return ("01RESOLVED", "01MODEL")

    def make_client(api_url: str, store_id: str, model_id: str, **_: object) -> object:
        rec.calls.append(f"make_client:{store_id}")
        return object()

    monkeypatch.setattr(fga_mod, "provision", provision)
    monkeypatch.setattr(fga_mod, "resolve", resolve)
    monkeypatch.setattr(fga_mod, "make_client", make_client)

    # A stand-in with the ONE method the startup path calls on it. `attach_auth` warms the verifier
    # so a misconfigured issuer is in the pod's log at boot rather than in a user's failed request,
    # and a bare `object()` cannot answer that — these postures are about the FGA half, so the OIDC
    # half has to get far enough out of the way to reach it.
    class _WarmVerifier:
        def warm(self) -> list[tuple[str, str]]:
            return []

    monkeypatch.setattr(oidc_mod, "OIDCVerifier", lambda *a, **k: _WarmVerifier())
    return rec


@pytest.mark.asyncio
async def test_the_default_posture_RESOLVES_when_unpinned(fga_calls: _Recorder) -> None:
    """Behavioural baseline for every service but the ONE bootstrap — saying nothing must not publish.

    `fga.provision` writes a new immutable authorization model for a body the store lacks, so a default
    that provisions lets every image, older ones included, add its own body to the history. Measured live
    2026-09-09: 1,256 models in the store and three services carrying an older `model.fga` with no
    `maintainer` rung. The catalog opts in explicitly; everything else reads.
    """
    app = FastAPI()
    await attach_auth(app, _Settings(), service="t")

    assert "provision" not in fga_calls.calls, "the DEFAULT posture published a model — boot order now decides the estate's permissions"
    assert fga_calls.calls == ["resolve", "make_client:01RESOLVED"]
    assert getattr(app.state, "fga", None) is not None
    assert getattr(app.state, "oidc", None) is not None


@pytest.mark.asyncio
async def test_provision_False_with_no_store_leaves_the_door_SHUT(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """An unbootstrapped estate must not be bootstrapped BY the reader. No client, so the gate 503s —
    which is the honest answer, and the one `fga.resolve` returning None is for."""

    async def resolve(api_url: str, **_: object) -> tuple[str, str] | None:
        fga_calls.calls.append("resolve")
        return None

    monkeypatch.setattr(fga_mod, "resolve", resolve)
    app = FastAPI()
    await attach_auth(app, _Settings(), service="t", provision=False)

    assert getattr(app.state, "fga", None) is None
    assert not any(c.startswith("make_client") for c in fga_calls.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("provision", [False, True], ids=["resolve", "provision"])
async def test_a_pinned_store_reaches_the_lookup(fga_calls: _Recorder, provision: bool) -> None:
    """The hook and `bootstrap-admin` write into a pinned store; a service that looked its store up by
    name instead would check against a different one ([[LH-201]])."""
    await build_fga_client(_Settings(fga_store_id="01PINNED"), service="t", provision=provision)

    assert fga_calls.kwargs["provision" if provision else "resolve"].get("store_id") == "01PINNED"


@pytest.mark.asyncio
async def test_fatal_True_refuses_to_run_without_the_model_its_image_carries(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """The medallion stage runner and the train trigger read a missing client as "authorization off".
    A fatal caller whose resolve finds no store, or no model its image carries, must crash rather than
    come up open ([[LH-201]])."""

    async def resolve(api_url: str, **_: object) -> tuple[str, str] | None:
        fga_calls.calls.append("resolve")
        return None

    monkeypatch.setattr(fga_mod, "resolve", resolve)

    with pytest.raises(Exception, match="no model this image carries"):
        await build_fga_client(_Settings(), service="stage-runner", provision=False, fatal=True)
    assert not any(c.startswith("make_client") for c in fga_calls.calls)


@pytest.mark.asyncio
async def test_fatal_True_reraises_a_failed_fga_build(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """`catalog`, `lineage` and the medallion apps crash on boot today. A refactor that turns that
    into a logged line converts a CrashLoopBackOff an operator watches into a 503 nobody does."""

    async def boom(api_url: str, **_: object) -> tuple[str, str]:
        raise RuntimeError("openfga unreachable")

    # `resolve`, not `provision`: the default posture is read-only, so that is the call this failure
    # has to arrive on for the assertion to be about `fatal` rather than about which half was patched.
    monkeypatch.setattr(fga_mod, "resolve", boom)
    app = FastAPI()
    with pytest.raises(RuntimeError, match="openfga unreachable"):
        await attach_auth(app, _Settings(), service="t", fatal=True)


@pytest.mark.asyncio
async def test_fatal_True_reraises_a_failed_verifier_build(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """The OIDC half of the same posture — those services wrap neither construction in a `try`."""

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("discovery refused")

    monkeypatch.setattr(oidc_mod, "OIDCVerifier", boom)
    app = FastAPI()
    with pytest.raises(RuntimeError, match="discovery refused"):
        await attach_auth(app, _Settings(fga_enabled=False), service="t", fatal=True)


@pytest.mark.asyncio
async def test_the_default_stays_NON_fatal(monkeypatch: pytest.MonkeyPatch, fga_calls: _Recorder) -> None:
    """The four swallowing services keep swallowing. A failure to BUILD leaves the attribute unset and
    the dependency answers 503 — never a permissive fallback, which would turn a broken authorization
    layer into an open one."""

    async def boom(api_url: str, **_: object) -> tuple[str, str]:
        raise RuntimeError("openfga unreachable")

    # The read half, for the same reason as the fatal case above: it is the one the default takes.
    monkeypatch.setattr(fga_mod, "resolve", boom)
    app = FastAPI()
    await attach_auth(app, _Settings(), service="t")

    assert getattr(app.state, "fga", None) is None
    assert getattr(app.state, "oidc", None) is not None, "the OIDC half must build independently — one failure must not take the other down"


# ── the structured audit events survive the shared bootstrap (DUP-01 verify) ─────────────────────
#
# When the ten inline bootstraps collapsed onto this one helper, the first cut replaced their
# structured `log.info(event, extra=...)` events with printf message strings — silently narrowing
# telemetry: `openfga_provisioned` is a documented severity-9 INFO audit event (`service_kit.obs`)
# that `obs.setup` raises to OTLP, and maintenance's four `reconcile_fga_*` diagnostics carried
# `extra` payloads (store_id, the pin-for-production hint, the unpinned reason). A message string
# drops all of it. The single emitter must keep them structured — one emitter is the improvement,
# not fewer events.


@pytest.mark.asyncio
async def test_provisioning_emits_the_structured_audit_event(caplog: pytest.LogCaptureFixture, fga_calls: _Recorder) -> None:
    import logging

    with caplog.at_level(logging.INFO):
        await build_fga_client(_Settings(), service="catalog", provision=True)
    rec = next((r for r in caplog.records if r.getMessage() == "openfga_provisioned"), None)
    assert rec is not None, "openfga_provisioned is no longer emitted — obs.py raises it to OTLP as a severity-9 audit event"
    assert getattr(rec, "store_id", None) and getattr(rec, "model_id", None), "the event lost its store_id/model_id extra — a printf string would"
    assert getattr(rec, "service", None) == "catalog"


@pytest.mark.asyncio
async def test_resolve_by_name_emits_the_structured_diagnostic(caplog: pytest.LogCaptureFixture, fga_calls: _Recorder) -> None:
    import logging

    with caplog.at_level(logging.INFO):
        await build_fga_client(_Settings(), service="maintenance", provision=False)
    rec = next((r for r in caplog.records if r.getMessage() == "openfga_resolved_by_name"), None)
    assert rec is not None, "the unpinned resolve-by-name diagnostic (was maintenance's reconcile_fga_resolved_by_name) is gone"
    assert getattr(rec, "store_id", None) and getattr(rec, "model_id", None), "the diagnostic lost its store_id/model_id extra"
