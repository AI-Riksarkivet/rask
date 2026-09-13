"""Pinning the store + model ids stops every boot-time model write. It must not stop the ESTATE
knowing that this image's `model.json` and the pinned model have diverged.

[[LH-139]] closed one direction: an older catalog image booted, `provision` wrote ITS bundled
`model.json`, and the store's newest model lost `warehouse#event_stager` — every door needing that
relation then ERRORED ("object relation does not exist") rather than denying, which the fail-closed
wrapper renders as "authorization service unavailable". The row's remaining half is the operator
posture that prevents it outright: pin `RASK_FGA_STORE_ID` + `RASK_FGA_MODEL_ID`, and
`build_fga_client` never calls `provision` at all.

THAT POSTURE IS THE SAME FAILURE FROM THE OTHER SIDE, and nothing announced it. A pinned boot never
reads `load_model()`, so an edit to `model.fga` that ships in the image takes effect nowhere and says
so nowhere — the symptom is again a door erroring on a relation, with nothing naming the model. Both
directions produce an outage-shaped signal for what is really a configuration difference; the estate's
signature failure is a control pointed at the wrong authority, and an unannounced pin is exactly that.

Measured 2026-09-13: `chart/values.yaml:925-926` ships `fgaStoreId: ""` / `fgaModelId: ""`, the live
catalog has both env vars UNSET, and no shipped values file sets either — so the posture the row
prescribes is the one nobody can adopt safely. This is the control that makes it adoptable.

IT REPORTS AND NEVER REFUSES. A pin is a deliberate deployment decision, so a divergence is news, not
bad data; raising would turn an operator's pin into a crash-loop that no redelivery can clear.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from openfga_sdk.client import OpenFgaClient
from pydantic import BaseModel

from service_kit.governed import fga
from service_kit.governed import fga as fga_mod
from service_kit.governed import oidc as oidc_mod
from service_kit.governed.auth_lifespan import build_fga_client


IMAGE_MODEL: dict[str, Any] = {
    "schema_version": "1.1",
    "type_definitions": [
        {"type": "warehouse", "relations": {"owner": {"this": {}}, "event_stager": {"this": {}}}},
        {"type": "table", "relations": {"owner": {"this": {}}}},
    ],
    "conditions": {},
}


def _typedef(type_name: str, relations: dict[str, Any]) -> Any:
    """The SDK's `TypeDefinition` shape — a `.type` and a `.relations` mapping.

    Built as objects rather than as the authored dict on purpose: a double that echoes its input agrees
    by construction and can never catch a shape difference, which is how [[LH-070]]'s skip shipped
    unable to fire.
    """
    return SimpleNamespace(type=type_name, relations=relations)


def _pinned(*type_definitions: Any) -> Any:
    return SimpleNamespace(id="model-PINNED", schema_version="1.1", conditions={}, type_definitions=list(type_definitions))


class _PinnedClient:
    """A client whose `read_authorization_model` answers whatever the test pinned."""

    model: ClassVar[Any] = None
    raises: ClassVar[BaseException | None] = None
    reads: ClassVar[int] = 0

    async def read_authorization_model(self) -> Any:
        _PinnedClient.reads += 1
        if _PinnedClient.raises is not None:
            raise _PinnedClient.raises
        return SimpleNamespace(authorization_model=_PinnedClient.model)


@pytest.fixture(autouse=True)
def _image(monkeypatch: pytest.MonkeyPatch) -> None:
    _PinnedClient.raises = None
    _PinnedClient.reads = 0
    monkeypatch.setattr(fga, "load_model", lambda: IMAGE_MODEL)


@pytest.mark.asyncio
async def test_a_pin_that_still_says_what_the_image_says_reports_no_drift() -> None:
    """The control that keeps this from becoming a check that fires on everything."""
    _PinnedClient.model = _pinned(
        _typedef("warehouse", {"owner": {"this": {}}, "event_stager": {"this": {}}}),
        _typedef("table", {"owner": {"this": {}}}),
    )

    drift = await fga.audit_pinned_model(cast(OpenFgaClient, _PinnedClient()), store_id="store-1", model_id="model-PINNED")

    assert drift.readable is True
    assert not drift.drifted, f"an identical model must not be reported as drifted: {drift}"


@pytest.mark.asyncio
async def test_a_relation_the_image_defines_and_the_pin_does_not_is_NAMED() -> None:
    """THE LIVE SHAPE, from the pinned side: `warehouse#event_stager` ships in the image and the store
    is pinned to a model without it. Under a pin nothing writes, so the relation simply never exists and
    every door that needs it errors."""
    _PinnedClient.model = _pinned(_typedef("warehouse", {"owner": {"this": {}}}), _typedef("table", {"owner": {"this": {}}}))

    drift = await fga.audit_pinned_model(cast(OpenFgaClient, _PinnedClient()), store_id="store-1", model_id="model-PINNED")

    assert drift.drifted
    assert "warehouse#event_stager" in drift.absent_from_pin, drift
    assert drift.absent_from_image == (), "the image defines everything the pin does here — reporting a loss would be a false alarm"


@pytest.mark.asyncio
async def test_a_relation_the_PIN_defines_and_the_image_does_not_is_named_separately() -> None:
    """The opposite direction is a different operator action — the pin is AHEAD of this image — so it
    must not be collapsed into the same field. One list for both would make a rollback and a
    roll-forward indistinguishable at exactly the moment the difference decides what to do."""
    _PinnedClient.model = _pinned(
        _typedef("warehouse", {"owner": {"this": {}}, "event_stager": {"this": {}}, "auditor": {"this": {}}}),
        _typedef("table", {"owner": {"this": {}}}),
    )

    drift = await fga.audit_pinned_model(cast(OpenFgaClient, _PinnedClient()), store_id="store-1", model_id="model-PINNED")

    assert drift.drifted
    assert "warehouse#auditor" in drift.absent_from_image, drift
    assert drift.absent_from_pin == ()


@pytest.mark.asyncio
async def test_a_relation_REDEFINED_in_place_is_reported_even_though_both_names_match() -> None:
    """Name-set comparison alone cannot see this — `_narrowings` discards every userset body — and a
    silently redefined relation is the change most likely to alter who may do what without altering any
    name. `_body_changes` is the half that sees it."""
    _PinnedClient.model = _pinned(
        _typedef("warehouse", {"owner": {"this": {}}, "event_stager": {"computedUserset": {"relation": "owner"}}}),
        _typedef("table", {"owner": {"this": {}}}),
    )

    drift = await fga.audit_pinned_model(cast(OpenFgaClient, _PinnedClient()), store_id="store-1", model_id="model-PINNED")

    assert drift.drifted
    assert "warehouse#event_stager" in drift.redefined, drift
    assert drift.absent_from_pin == () and drift.absent_from_image == (), "the names all match — only the body differs"


@pytest.mark.asyncio
async def test_an_unreadable_pinned_model_leaves_the_boot_SERVING() -> None:
    """The posture difference from `_current_model`, stated as a test because it is the whole reason
    this read is not under `_guarded`: that read gates a WRITE, so failing closed stops a narrowing
    model reaching the store. This one gates nothing — it only reports — and an estate must not be taken
    down because its diagnostic could not run."""
    _PinnedClient.model = None
    _PinnedClient.raises = RuntimeError("openfga unreachable")

    drift = await fga.audit_pinned_model(cast(OpenFgaClient, _PinnedClient()), store_id="store-1", model_id="model-PINNED")

    assert drift.readable is False
    assert not drift.drifted, "an unread model is UNKNOWN, never 'in agreement' — and never a reported divergence either"


@pytest.mark.asyncio
async def test_a_CORRUPT_bundled_model_costs_the_audit_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit runs inside `build_fga_client`'s own `try`, which RE-RAISES for every service built
    with `fatal=True` — the catalog among them. So a bundled `model.json` this image cannot parse would
    crash-loop a pinned catalog that, under a pin, does not need that file to serve at all. Reading the
    image is therefore inside the same guard as reading the store."""
    _PinnedClient.model = _pinned(_typedef("warehouse", {"owner": {"this": {}}}))

    def boom() -> dict[str, Any]:
        raise ValueError("model.json is not parseable in this image")

    monkeypatch.setattr(fga, "load_model", boom)

    drift = await fga.audit_pinned_model(cast(OpenFgaClient, _PinnedClient()), store_id="store-1", model_id="model-PINNED")

    assert drift.readable is False
    assert not drift.drifted


class _Settings(BaseModel):
    """Structural stand-in for `GovernedAuthSettings`, matching `test_attach_auth_postures.py`."""

    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_discovery_url: str | None = None
    oidc_cache_ttl: int = 300
    oidc_leeway: int = 30
    oidc_allow_insecure: bool = False
    fga_enabled: bool = True
    fga_api_url: str = "http://fga.test:8080"
    fga_store_id: str | None = None
    fga_model_id: str | None = None
    fga_timeout_seconds: float = 5.0


@pytest.fixture
def _wiring(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records which halves a boot took. The audit is only worth anything if a PINNED boot reaches it —
    testing `audit_pinned_model` directly and nothing else is the [[LH-140]] shape, where the rule had
    sixteen tests and the wiring that carries it had none."""
    calls: list[str] = []

    async def provision(api_url: str, **_: object) -> tuple[str, str]:
        calls.append("provision")
        return "01PROVISIONED", "01MODEL"

    async def resolve(api_url: str, **_: object) -> tuple[str, str] | None:
        calls.append("resolve")
        return ("01RESOLVED", "01MODEL")

    def make_client(api_url: str, store_id: str, model_id: str, **_: object) -> object:
        calls.append(f"make_client:{store_id}:{model_id}")
        return cast(OpenFgaClient, _PinnedClient())

    async def audit(client: object, *, store_id: str, model_id: str) -> object:
        calls.append(f"audit:{store_id}:{model_id}")
        return SimpleNamespace(readable=True, drifted=False)

    monkeypatch.setattr(fga_mod, "provision", provision)
    monkeypatch.setattr(fga_mod, "resolve", resolve)
    monkeypatch.setattr(fga_mod, "make_client", make_client)
    monkeypatch.setattr(fga_mod, "audit_pinned_model", audit)
    monkeypatch.setattr(oidc_mod, "OIDCVerifier", lambda *a, **k: object())
    return calls


@pytest.mark.asyncio
async def test_a_PINNED_boot_of_the_model_owner_audits_instead_of_provisioning(_wiring: list[str]) -> None:
    """The wiring, both halves in one assertion: pinning must skip the write AND reach the audit."""
    settings = _Settings(fga_store_id="store-PINNED", fga_model_id="model-PINNED")

    await build_fga_client(settings, service="catalog", provision=True)

    assert "provision" not in _wiring, "a pin exists so no boot rewrites the estate's model"
    assert "audit:store-PINNED:model-PINNED" in _wiring, "a pin that is never compared to the image is the unannounced half of LH-139"


@pytest.mark.asyncio
async def test_an_UNPINNED_boot_still_provisions_and_audits_nothing(_wiring: list[str]) -> None:
    """The negative twin. Unpinned, `provision` already compares the image against the store and
    refuses a narrowing — auditing there would report the same divergence twice under two names."""
    await build_fga_client(_Settings(), service="catalog", provision=True)

    assert "provision" in _wiring
    assert not [c for c in _wiring if c.startswith("audit:")], "nothing to audit — provision has just reconciled the model itself"


@pytest.mark.asyncio
async def test_a_service_that_does_not_OWN_the_model_does_not_audit_it(_wiring: list[str]) -> None:
    """Nine services mix in the governed settings and every one of them ships the same bundled
    `model.json`. Auditing from each would emit the identical divergence nine times, and the second
    through ninth are noise from a pod with no authority over the model. `provision=True` — the one
    caller that owns it — is the same gate that already decides who may publish."""
    settings = _Settings(fga_store_id="store-PINNED", fga_model_id="model-PINNED")

    await build_fga_client(settings, service="maintenance", provision=False)

    assert not [c for c in _wiring if c.startswith("audit:")]
    assert "make_client:store-PINNED:model-PINNED" in _wiring, "it still builds its client from the pin — only the audit is the owner's"
