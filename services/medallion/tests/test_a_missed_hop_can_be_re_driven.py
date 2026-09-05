"""The cascade's re-run verb — docs/DECISIONS.md "Cascade repair" (C2).

A hop can be missed four ways and the estate could DETECT some of them and REPAIR none: the only
remedy was re-publishing the upstream table, which re-drives every consumer of it rather than the one
edge that failed. This is the edge-addressed remedy.

Three properties carry it, and each is a decision the spec argued for rather than a mechanism:

* **the token is OPTIONAL (R1)** — it is the `table_published` event id, which the control outbox
  drops on ack and no store retains, so a verb that required one could not be built. Supplied, the
  trigger is verbatim and the mover's deterministic instance id reattaches; absent, a fresh one and a
  full recompute;
* **the rung is the MOVER's (R4)**, not `/produce`'s `can_administer` — which is coarser AND
  different, and would lock out exactly the non-admin validator `can_promote` exists for;
* **no 409 (R2a)** — the check R2 prescribed needs a Ray job listing, and Ray's `GET /api/jobs/` takes
  no parameters at all: measured at 81,155 jobs / 164.7 MB in one response, 1179 MiB RSS against a
  1536 MiB limit. The stage write is overwrite-convergent, so the data is safe and the cost is
  compute; the response says that rather than implying a guarantee the listing could not make.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from medallion.api import rerun
from medallion.api.dependencies import FgaClientDep, SettingsDep
from medallion.core.config import MedallionSettings
from medallion.services.catalog_register import RegisterError
from service_kit.lakehouse.ns_errors import install_problem_handlers


_GATES = {
    "bronze": {"to_namespace": "silver", "required_action": "can_create_table"},
    "silver": {"to_namespace": "gold", "required_action": "can_promote"},
}
_ROUTES = {"bronze": "medallion.bronze", "silver": "medallion.silver"}


class _Bus:
    def __init__(self, *, lands: bool = True) -> None:
        self.published: list[dict[str, Any]] = []
        self.lands = lands


@pytest.fixture
def bus() -> _Bus:
    return _Bus()


@pytest.fixture
def checks() -> list[dict[str, str]]:
    return []


_VENDED = "s3://acme-bucket/e41135a5_acme-silver$features"


@pytest.fixture
def located() -> list[str]:
    return []


def _app(
    bus: _Bus,
    checks: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    subject: str | None = "alice",
    allow: bool = True,
    location: str | Exception | None = _VENDED,
    located: list[str] | None = None,
) -> FastAPI:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(rerun.router)
    application.state.dapr = object()

    # `catalog_url` is what makes the location lookup happen at all — the chart renders it
    # unconditionally on the producer, so a double omitting it tests the ungoverned dev shape only.
    settings = MedallionSettings.model_validate(
        {"mover_gates": _GATES, "transform_routes": _ROUTES, "pubsub": "pubsub", "catalog_url": "http://catalog:2333"}
    )
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[FgaClientDep.__metadata__[0].dependency] = lambda: object()
    application.dependency_overrides[rerun.authenticate_subject] = lambda: subject

    async def _check(_client: Any, *, user: str, relation: str, obj: str) -> bool:
        checks.append({"user": user, "relation": relation, "obj": obj})
        return allow

    async def _publish(_dapr: Any, **kwargs: Any) -> bool:
        bus.published.append(kwargs)
        return bus.lands

    def _describe(*, table_id: str, **_: Any) -> str | None:
        if located is not None:
            located.append(table_id)
        if isinstance(location, Exception):
            raise location
        return location

    monkeypatch.setattr(rerun.fga, "check", _check)
    monkeypatch.setattr(rerun.dapr_publish, "publish_json", _publish)
    monkeypatch.setattr(rerun.catalog_register, "describe_table_location", _describe)
    return application


@pytest.fixture
def client(bus: _Bus, checks: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch, located: list[str]) -> Iterator[TestClient]:
    with TestClient(_app(bus, checks, monkeypatch, located=located)) as c:
        yield c


_BODY = {"object_id": "table:acme-silver$features", "project": "acme", "to_version": 7, "from_version": 4}


def test_a_supplied_TOKEN_re_mints_the_trigger_VERBATIM(client: TestClient, bus: _Bus) -> None:
    """The cheap repair. The mover derives its instance id from this token, so a verbatim trigger
    reattaches to a running or succeeded job instead of starting a second one."""
    response = client.post("/movers/stages/rerun", json={**_BODY, "token": "ev-123"})

    assert response.status_code == 202, response.text
    assert response.json()["mode"] == rerun.REATTACH
    assert response.json()["token"] == "ev-123"
    assert bus.published[0]["payload"]["token"] == "ev-123"
    assert bus.published[0]["topic_name"] == "medallion.silver"


def test_an_ABSENT_token_mints_a_fresh_one_and_SAYS_what_that_costs(client: TestClient, bus: _Bus) -> None:
    """The token cannot be re-minted — the outbox drops it on ack — so requiring one would make the
    verb unimplementable. What the verb owes instead is honesty about the price."""
    response = client.post("/movers/stages/rerun", json=_BODY)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == rerun.RECOMPUTE
    assert body["token"] and body["token"] != "ev-123"
    assert "duplicate a live job" in body["note"], "the recompute path must state its cost, not imply a guarantee"


def test_the_RUNG_is_the_edge_s_own_not_produce_s(client: TestClient, checks: list[dict[str, str]]) -> None:
    """A silver->gold re-run asks `can_promote` on `namespace:acme-gold`, exactly as the mover asks
    when it runs the hop itself. `authorize_produce`'s `can_administer` is coarser AND different and
    would lock out the non-admin validator the rung exists for."""
    client.post("/movers/stages/rerun", json=_BODY)

    assert checks == [{"user": "alice", "relation": "can_promote", "obj": "namespace:acme-gold"}]


def test_a_BRONZE_edge_asks_a_DIFFERENT_rung(client: TestClient, checks: list[dict[str, str]]) -> None:
    """Each edge carries its own action and its own target tier; a single hardcoded rung would be
    wrong for one of them in whichever direction it was chosen."""
    client.post("/movers/stages/rerun", json={**_BODY, "object_id": "table:acme-bronze$events"})

    assert checks == [{"user": "alice", "relation": "can_create_table", "obj": "namespace:acme-silver"}]


def test_a_DENIED_caller_publishes_NOTHING(bus: _Bus, checks: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate runs before the publish, so a refusal cannot leave a trigger on the bus."""
    with TestClient(_app(bus, checks, monkeypatch, allow=False)) as client:
        assert client.post("/movers/stages/rerun", json=_BODY).status_code == 403

    assert bus.published == []


def test_an_ANONYMOUS_caller_is_REFUSED_even_with_auth_off(bus: _Bus, checks: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """No dev-open path, for the reason the promotion decision has none: a re-run is an act with a
    responsible party, and an anonymous one is not an act anyone made. `/produce` may be open because
    the sub it returns is a targeting hint; this is a decision."""
    with TestClient(_app(bus, checks, monkeypatch, subject=None)) as client:
        assert client.post("/movers/stages/rerun", json=_BODY).status_code == 403

    assert bus.published == [] and checks == []


def test_an_object_that_names_NO_EDGE_is_refused_before_any_gate(bus: _Bus, checks: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """403 rather than 404, deliberately: the object may exist perfectly well and simply not name a
    cascade lane, and a 404 would tell an unauthorized caller which object ids are real."""
    with TestClient(_app(bus, checks, monkeypatch)) as client:
        assert client.post("/movers/stages/rerun", json={**_BODY, "object_id": "table:acme-gold$catalog"}).status_code == 403

    assert bus.published == [] and checks == []


def test_the_RANGE_reaches_the_trigger_and_an_absent_FLOOR_stays_absent(client: TestClient, bus: _Bus) -> None:
    """`from_version` absent means "everything up to `to`" — carried as-is rather than coerced to 0,
    because "no prior publication" and "published from version 0" are different claims and the mover
    reads them differently."""
    client.post("/movers/stages/rerun", json={"object_id": "table:acme-silver$features", "project": "acme", "to_version": 7})

    payload = bus.published[0]["payload"]
    assert payload["to_version"] == 7
    assert payload["from_version"] is None


def test_the_ORIGINATOR_survives_a_re_run(client: TestClient, bus: _Bus) -> None:
    """A re-run of a person's cascade must still reach that person's inbox. It authorizes nothing —
    the notifications plane re-derives visibility per recipient at delivery."""
    client.post("/movers/stages/rerun", json={**_BODY, "originator": "bob", "cascade_id": "c-1"})

    payload = bus.published[0]["payload"]
    assert payload["originator"] == "bob" and payload["cascade_id"] == "c-1"


def test_a_BROKER_outage_is_503_and_not_a_202_that_re_drove_nothing(bus: _Bus, checks: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure a caller can act on by retrying. Every other refusal happens before anything
    is published, so only this one can be reported after the decision was made."""
    bus.lands = False
    with TestClient(_app(bus, checks, monkeypatch)) as client:
        assert client.post("/movers/stages/rerun", json=_BODY).status_code == 503


def test_the_TRIGGER_is_the_shape_the_subscription_mints(client: TestClient, bus: _Bus) -> None:
    """Both producers go through `build_stage_trigger`, and that is the point: every field is read by
    a different guard on the mover, so a mismatch is not a loud failure but a wrong one — the wrong
    lane dropped as another's, the wrong delta range, or a composed path instead of vended bytes."""
    client.post("/movers/stages/rerun", json={**_BODY, "token": "ev-9"})

    payload = bus.published[0]["payload"]
    assert set(payload) >= {"token", "dataset", "namespace", "from_version", "to_version", "project"}
    assert payload["dataset"] == "silver$features" and payload["namespace"] == "silver"
    assert json.loads(json.dumps(payload)) == payload, "the trigger must be JSON-serializable to cross the bus"


def test_the_trigger_carries_the_CATALOG_VENDED_location(client: TestClient, bus: _Bus, located: list[str]) -> None:
    """I2 on the third head, and without it the repair reads a path nothing has ever written.

    `_confine_from_uri` honours a trigger's `from_uri` and otherwise falls back to `_resolve_roots`'
    composed `{root}/medallion/{namespace}` — which `transform.py` calls "a path no catalog-written
    table has ever occupied", because the catalog vends `{root}/{hash}_{ns}${name}`. So a re-run of a
    catalog-written table woke the mover, opened the wrong location, and found none of the rows it was
    sent to re-drive. Both other heads already resolve it: `publication_trigger` reads it off the
    control event, `ingest_trigger._vended_upstream` asks the catalog. This is the third.
    """
    client.post("/movers/stages/rerun", json=_BODY)

    assert located == ["acme-silver$features"], "the verb did not ask the catalog where the table lives"
    assert bus.published[0]["payload"]["from_uri"] == "s3://acme-bucket/e41135a5_acme-silver$features"


def test_an_UNGOVERNED_table_still_re_runs_off_the_composed_path(bus: _Bus, checks: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer is ADVISORY, exactly as it is on the other head. A table this catalog does not govern
    is a real and supported case — an external OpenLineage producer writing its own dataset — and the
    composed path is the correct upstream for it. Refusing would make the repair verb narrower than the
    cascade it repairs."""
    with TestClient(_app(bus, checks, monkeypatch, location=None)) as client:
        assert client.post("/movers/stages/rerun", json=_BODY).status_code == 202

    assert "from_uri" not in bus.published[0]["payload"] or bus.published[0]["payload"]["from_uri"] is None


def test_a_CATALOG_OUTAGE_does_not_block_the_repair(bus: _Bus, checks: list[dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """"We could not ask" is not "there is nothing there", and the two must not collapse — but neither
    may an unreachable catalog stop an operator repairing a hop. Logged, published, and the mover falls
    back to the composed path exactly as it did before this field existed."""
    with TestClient(_app(bus, checks, monkeypatch, location=RegisterError("catalog unreachable"))) as client:
        assert client.post("/movers/stages/rerun", json=_BODY).status_code == 202

    assert bus.published[0]["payload"]["dataset"] == "silver$features"
