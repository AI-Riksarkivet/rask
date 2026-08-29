"""The governance doors that 404'd on a `/produce` bronze dataset answer it now — measured, not argued.

THE DEFECT. `medallion/services/produce.py` had no catalog import. The bronze dataset the cascade HEAD
seeds was never registered, so it held no `table:` object and every governed door bounced off it:
`POST /v1/table/bronze$events/policy/set` answered **404 "table has no storage location to police"**, so
there was no retention override and no legal hold; no `_protection/` record was reachable; and no FGA
grant could name it, because `seed_ownership` runs at the CREATE/REGISTER door and that door was never
opened. Silver and gold were governed the whole time (`transform.py` calls `ensure_stage_output` before
every write), and the separate ingest plane governs the bronze it lands — so the same tier was governed
or not purely by which door produced it.

MEASURED THROUGH A REAL SOCKET, against the real catalog app over a real `dir`-backend namespace, with
the real `produce()` writing a real Lance dataset. Not TestClient: this estate has already shipped a
route that TestClient bound happily and a live uvicorn 422'd on every call
(`test_search_spec_binding`), so a governance claim proven only in-process is not proven.

WHAT IS STUBBED, and why it is not the claim: the catalog's authentication and its authorization gate
are overridden (a fixed subject, an open gate) so that FGA SEEDING can be observed at all — seeding
no-ops without a token — and the lineage emitter is swapped for a recorder so the event the register
door publishes can be inspected. The register, describe and policy doors themselves, the namespace
backend, the Lance write and the medallion's own registration path are all real.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn


# ── the recording catalog: a real app, a real port ────────────────────────────────────────────────


class _RecordingEmitter:
    """The catalog's own event builder, with the transport replaced by a list.

    Subclassed from `_BaseLineageEmitter` on purpose: an event hand-written here would prove only that
    a hand-written event behaves as expected. What the cascade head has to survive is the event the
    REGISTER door really publishes onto `lineage.events.v1`.
    """

    events: list[dict[str, Any]]

    def __init__(self) -> None:
        self.events = []
        self._job_namespace = "lance-catalog"

    async def project_for(self, top_ns: str) -> str | None:
        return None

    async def _send(self, event: dict[str, Any], **_: object) -> None:
        self.events.append(event)


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The catalog service, listening on a real ephemeral port, rooted on a local directory."""
    from catalog.api import fga_deps, security
    from catalog.core.config import get_settings
    from catalog.core.lineage_emit import _BaseLineageEmitter

    from service_kit.governed import fga
    from service_kit.governed.oidc import IDToken

    root = tmp_path / "lance-catalog"
    root.mkdir()
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"file://{root}",
        "LANCE_CONTROL_ROOT": f"file://{tmp_path / 'control'}",
        # Authn ON only because the settings refuse FGA without it (authz needs a user); the verifier
        # itself is never reached — `security.authenticate` is overridden below.
        "LANCE_AUTH_ENABLED": "true",
        "LANCE_OIDC_ENABLED": "true",
        "LANCE_OIDC_ISSUER": "https://dex.test/dex",
        "LANCE_OIDC_AUDIENCE": "lance-catalog",
        "LANCE_OIDC_ALLOW_INSECURE": "true",
        "LANCE_FGA_ENABLED": "true",  # so the register door's ownership seed actually runs
        # PINNED ids = the production posture: no boot-time store provisioning, so the lifespan never
        # reaches for an OpenFGA that is not running here. The grant call itself is recorded below.
        "LANCE_FGA_API_URL": "http://127.0.0.1:9",
        "LANCE_FGA_STORE_ID": "01TESTSTORE",
        "LANCE_FGA_MODEL_ID": "01TESTMODEL",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_S3_ACCESS_KEY_ID": "x",
        "LANCE_S3_SECRET_ACCESS_KEY": "x",
        "LANCE_S3_ENDPOINT_URL": "http://127.0.0.1:9",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    from catalog.main import app

    granted: list[dict[str, Any]] = []

    async def _record_grant(_client: object, **kwargs: object) -> None:
        granted.append(dict(kwargs))

    monkeypatch.setattr(fga, "grant_on_create", _record_grant)

    token = IDToken(iss="https://dex.test/dex", sub="service-medallion-producer", aud="lance-catalog", iat=0, exp=1 << 31)
    app.dependency_overrides[security.authenticate] = lambda: token
    app.dependency_overrides[fga_deps.authorize] = lambda: None

    emitter = cast("Any", type("_Emitter", (_RecordingEmitter, _BaseLineageEmitter), {})())
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = 30.0
        while not server.started and thread.is_alive() and deadline > 0:
            threading.Event().wait(0.05)
            deadline -= 0.05
        assert server.started, "the catalog never came up"
        app.state.fga = object()  # a wired client, so seed_ownership is not skipped
        app.state.lineage_emitter = emitter
        port = server.servers[0].sockets[0].getsockname()[1]
        yield type("_Catalog", (), {"url": f"http://127.0.0.1:{port}", "root": root, "granted": granted, "events": emitter.events})
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _post(url: str, path: str, body: dict[str, Any]) -> Any:
    import httpx

    return httpx.post(f"{url}{path}", json=body, timeout=30.0)


# ── the run ───────────────────────────────────────────────────────────────────────────────────────


async def _produce_once(catalog: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """One real `/produce`: a real Lance bronze write under the catalog's own root."""
    from medallion.core.config import MedallionSettings
    from medallion.services import produce as produce_module

    head: list[dict[str, Any]] = []

    async def publish(*_a: object, **kwargs: object) -> None:
        head.append(json.loads(cast("str", kwargs["event_json"])))

    monkeypatch.setattr(produce_module.outbox, "publish_lineage_with_outbox", publish)
    settings = MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_BRONZE_URI": f"file://{catalog.root}/medallion/bronze",
            "MEDALLION_CATALOG_ROOT": f"file://{catalog.root}",
            "MEDALLION_CATALOG_URL": catalog.url,
        }
    )
    result = await produce_module.produce(cast("Any", None), settings, token="idem-e2e")
    return result, head


@pytest.fixture
def produced(catalog: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], list[dict[str, Any]]]:
    # The tier namespace is the WAREHOUSE's to create, never a lane's — the medallion registers into it
    # and never mints it (`register_stage_output` learned that in-cluster: a top-level create is refused
    # 400 outright once warehouses are on). So the estate's own provisioning step stands in here.
    assert _post(catalog.url, "/v1/namespace/bronze/create", {"id": ["bronze"], "mode": "EXIST_OK"}).status_code == 200
    return asyncio.run(_produce_once(catalog, monkeypatch))


def test_a_produced_bronze_holds_a_catalog_record(catalog: Any, produced: tuple[dict[str, str], list[dict[str, Any]]]) -> None:
    result, _ = produced
    assert result["status"] == "produced", result

    described = _post(catalog.url, "/v1/table/bronze$events/describe", {"id": ["bronze", "events"]})

    assert described.status_code == 200, f"the seeded bronze dataset is not a catalog object: {described.text[:300]}"
    assert described.json()["location"] == f"file://{catalog.root}/medallion/bronze", "the catalog governs a different copy than the head wrote"


def test_the_policy_door_that_404d_now_answers(catalog: Any, produced: tuple[dict[str, str], list[dict[str, Any]]]) -> None:
    """The measured symptom: retention/legal-hold was unreachable for the head's own tier."""
    response = _post(catalog.url, "/v1/table/bronze$events/policy/set", {"retain_versions": 5})

    assert response.status_code == 200, f"no retention override is reachable for a produced bronze: {response.text[:300]}"
    assert response.json()["path"].endswith("/medallion/bronze")


def test_the_doors_are_shut_until_the_head_registers(catalog: Any) -> None:
    """The same door, on the same estate, WITHOUT the produce — the state this change leaves behind."""
    assert _post(catalog.url, "/v1/namespace/bronze/create", {"id": ["bronze"], "mode": "EXIST_OK"}).status_code == 200

    assert _post(catalog.url, "/v1/table/bronze$events/policy/set", {"retain_versions": 5}).status_code == 404


def test_registering_seeds_the_ownership_tuples(catalog: Any, produced: tuple[dict[str, str], list[dict[str, Any]]]) -> None:
    """Governance is not only the policy door: without an owner tuple no FGA grant can name the table,
    so it is invisible to every governed read path."""
    seeds = [g for g in catalog.granted if g.get("obj_id") == "bronze$events"]

    assert seeds, f"the bronze table got no ownership tuple — {catalog.granted}"
    assert seeds[0]["resource"] == "table"
    assert seeds[0]["parent_object"] == "namespace:bronze", "no containment edge, so no namespace-level grant reaches it"


def test_the_cascade_head_still_fires_exactly_once(catalog: Any, produced: tuple[dict[str, str], list[dict[str, Any]]]) -> None:
    """The head's own event is unchanged, and the REGISTER marker the catalog publishes alongside it
    must not fire a SECOND cascade: it names the same namespace and dataset and is also a COMPLETE, so
    without the guard one `/produce` would drive two unrelated bronze->gold runs over one batch."""
    from medallion.core.config import MedallionSettings
    from medallion.services.ingest_trigger import _bronze_write_dataset

    _, head = produced
    settings = MedallionSettings.model_validate({})
    fired = [e for e in [*head, *catalog.events] if _bronze_write_dataset(e, settings, "") is not None]

    assert len(head) == 1, f"the head must emit exactly one bronze-write event, got {len(head)}"
    assert len(fired) == 1, f"one produce fired {len(fired)} cascades — the catalog's register marker is not a bronze arrival"
