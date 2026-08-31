"""The ingest-first ordering hazard: `/bronze-arrival` must name the location the CATALOG vends.

`/publication-arrival` carries `from_uri` (I2) — it reads the catalog's vended `location` off the
control event and puts it on the stage trigger, so the mover opens the table that was actually written.
Without the same field on `/bronze-arrival`, the bronze->silver mover falls through to `_resolve_roots`'
composed `{root}/medallion/{namespace}` — a DEPLOYMENT CONTRACT between `produce.py` and the chart's
`MEDALLION_FROM_URI`, and a path no catalog-vended table occupies.

That makes the first leg of the cascade depend on WHICH SERVICE CREATED THE TABLE FIRST:

* producer first — `register_written_dataset` attaches `bronze$events` to the composed path, `ingest`'s
  `ensure` adopts that registration, and both writers and the mover agree.
* ingest first — `ensure` creates through the catalog's own door and takes the vended
  `{root}/{hash}_{ns}${name}`. Ingest writes its rows there, the mover opens the composed path, and the
  cascade fires correctly, wakes, finds none of those rows, and ACKS 200.

The second ordering is what this suite pins. It is deliberately asserted on the SILVER OUTPUT rather
than on the trigger payload alone: the payload is the mechanism, but the damage is a tier that
transformed the wrong bytes under real-looking lineage without a single red signal.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import lance
import pytest
import respx
from dapr.aio.clients import DaprClient
from httpx import Response

from medallion.core.config import MedallionSettings
from medallion.services.compute import seed_bronze
from medallion.services.ingest_trigger import handle_bronze_arrival
from medallion.services.transform import handle_stage
from service_kit.lakehouse import warehouse_registry


CATALOG = "http://catalog.test"
#: The bronze table both writers name — project-qualified, as `project_namespace` composes it.
BRONZE_ID = "acme-bronze$events"
#: The shape the catalog actually vends: `{root}/{hash}_{namespace}${name}`, never `{root}/medallion/{ns}`.
VENDED_STEM = "9f2c1a_acme-bronze$events"


class _FakeDapr:
    """Captures every published event — the head's stage trigger and the movers' lineage alike."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


@pytest.fixture(autouse=True)
def _fresh_registry_cache() -> Any:
    warehouse_registry.clear_cache()
    yield
    warehouse_registry.clear_cache()


def _provision(control: Path, project: str, root: Path) -> None:
    """One ACTIVE warehouse record for ``project``, in the catalog registry's on-disk shape."""
    registry = control / "_warehouses"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "wh1.json").write_text(json.dumps({"id": "wh1", "project": project, "root_uri": str(root), "status": "active"}))


def _bronze_event() -> dict[str, Any]:
    """The bronze write's OpenLineage COMPLETE — the only thing the head is given.

    It names the table and NOT where the table lives: neither the catalog's `insert` emit
    (`lineage_deps.emit_measured_write`) nor ingest's own terminal carries a `dataSource` facet, which
    is precisely why the location has to be resolved rather than read off the wire.
    """
    return {
        "eventType": "COMPLETE",
        "run": {"runId": "run-1", "facets": {"lance": {"operation": "insert", "token": "tok1", "project": "acme"}}},
        "outputs": [{"namespace": "acme-bronze", "name": BRONZE_ID}],
    }


def _head_settings(control: Path, decoy_bronze: Path) -> MedallionSettings:
    return MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": str(decoy_bronze), "control_root": str(control), "catalog_url": CATALOG})


def _mover_settings(control: Path, decoys: dict[str, str]) -> MedallionSettings:
    """The bronze->silver mover, wired exactly as the chart wires it: composed env URIs + a control root."""
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_uri": decoys["bronze"],
            "to_uri": decoys["silver"],
            "from_namespace": "bronze",
            "from_dataset": "bronze$events",
            "to_namespace": "silver",
            "to_dataset": "silver$features",
            "operation": "embed",
            "pub_topic": "medallion.silver",
            "control_root": str(control),
        }
    )


def _describe(location: str, status: int = 200) -> respx.Route:
    return respx.post(f"{CATALOG}/v1/table/{BRONZE_ID}/describe").mock(return_value=Response(status, json={"location": location}))


@respx.mock
def test_an_ingest_first_bronze_cascades_from_the_location_the_catalog_vends(tmp_path: Path) -> None:
    """THE ORDERING HAZARD. `ingest` created the table, so bronze lives at the vended hash path — and the
    mover must transform THOSE rows, not whatever happens to sit at the composed path."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    vended = str(wh / VENDED_STEM)
    composed = str(wh / "medallion" / "bronze")
    # The rows ingest actually wrote, at the location the catalog vended for it.
    seed_bronze(vended, {}, rows=5)
    # A STALE batch at the composed path. Seeded on purpose: with the path merely absent the mover
    # errors and the failure is visible, which would hide the defect behind an accident. Present, the
    # spurious run succeeds end to end and ACKS 200 — which is the damage.
    seed_bronze(composed, {}, rows=2)
    _describe(vended)
    dapr = _FakeDapr()
    decoys = {ns: str(tmp_path / f"default-{ns}") for ns in ("bronze", "silver")}

    head = _head_settings(control, tmp_path / "default-bronze")
    assert asyncio.run(handle_bronze_arrival(cast("DaprClient", dapr), head, {"data": _bronze_event()})) == {"status": "SUCCESS"}
    trigger = next(p["data"] for p in dapr.published if p["topic"] == head.bronze_topic)

    mover = _mover_settings(control, decoys)
    assert asyncio.run(handle_stage(cast("DaprClient", dapr), mover, {"data": trigger})) == {"status": "SUCCESS"}

    # THE DAMAGE, asserted before the mechanism: a green run over the wrong bytes is what the composed
    # path buys, and it is indistinguishable from a correct one on every other signal.
    silver = lance.dataset(str(wh / "medallion" / "silver")).to_table()
    assert silver.num_rows == 5, f"silver transformed {silver.num_rows} rows — the cascade woke and read a dataset nobody wrote to"
    # THE MECHANISM: the head resolved the upstream through the catalog and named it on the trigger.
    assert trigger.get("from_uri") == vended


@respx.mock
def test_a_produce_first_bronze_still_cascades_from_the_composed_path(tmp_path: Path) -> None:
    """THE OTHER ORDERING, unchanged. `POST /produce` attaches its deployment-contract URI through
    `register_table`, so the catalog vends the composed `{root}/medallion/{ns}` — the head names it, the
    mover's confinement accepts it (it IS the resolved root's own tier path), and the cascade reads
    exactly what it read before the trigger carried a location at all."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    composed = str(wh / "medallion" / "bronze")
    seed_bronze(composed, {}, rows=3)
    _describe(composed)
    dapr = _FakeDapr()
    decoys = {ns: str(tmp_path / f"default-{ns}") for ns in ("bronze", "silver")}

    head = _head_settings(control, tmp_path / "default-bronze")
    assert asyncio.run(handle_bronze_arrival(cast("DaprClient", dapr), head, {"data": _bronze_event()})) == {"status": "SUCCESS"}
    trigger = next(p["data"] for p in dapr.published if p["topic"] == head.bronze_topic)
    assert trigger["from_uri"] == composed

    mover = _mover_settings(control, decoys)
    assert asyncio.run(handle_stage(cast("DaprClient", dapr), mover, {"data": trigger})) == {"status": "SUCCESS"}
    assert lance.dataset(str(wh / "medallion" / "silver")).to_table().num_rows == 3


@respx.mock
def test_a_head_that_cannot_reach_the_catalog_still_fires_the_cascade(tmp_path: Path) -> None:
    """BACKWARD COMPATIBLE IN THE RIGHT DIRECTION. The composed-path fallback stays reachable — it stops
    being the ONLY path. A catalog that cannot answer must degrade to today's behaviour (a trigger with
    no `from_uri`), never to a head that stops firing: for the produce-first estate the composed path is
    the correct one, and a describe outage there would otherwise halt a cascade that works."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    _describe("", status=503)
    dapr = _FakeDapr()

    head = _head_settings(control, tmp_path / "default-bronze")
    assert asyncio.run(handle_bronze_arrival(cast("DaprClient", dapr), head, {"data": _bronze_event()})) == {"status": "SUCCESS"}

    trigger = next(p["data"] for p in dapr.published if p["topic"] == head.bronze_topic)
    assert "from_uri" not in trigger
    assert trigger["dataset"] == "bronze$events" and trigger["project"] == "acme"


@respx.mock
def test_a_table_the_catalog_does_not_govern_names_no_upstream(tmp_path: Path) -> None:
    """An external OpenLineage producer may write a bronze table this catalog has no record of, and
    `describe` answers 4xx (403 for an absent table, not 404). That is an ANSWER, not an outage: no
    location to name, so the composed path stands and the cascade fires exactly as it always has."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    _describe("", status=403)
    dapr = _FakeDapr()

    head = _head_settings(control, tmp_path / "default-bronze")
    assert asyncio.run(handle_bronze_arrival(cast("DaprClient", dapr), head, {"data": _bronze_event()})) == {"status": "SUCCESS"}

    trigger = next(p["data"] for p in dapr.published if p["topic"] == head.bronze_topic)
    assert "from_uri" not in trigger


def test_an_ungoverned_head_names_no_upstream(tmp_path: Path) -> None:
    """The dev/demo shape (no `MEDALLION_CATALOG_URL`) asks nobody and carries nothing — the same
    escape hatch `produce.py` and the movers keep, so a stack with no catalog still cascades."""
    control = tmp_path / "control"
    _provision(control, "acme", tmp_path / "acme-wh")
    dapr = _FakeDapr()
    head = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": str(tmp_path / "b"), "control_root": str(control)})

    assert asyncio.run(handle_bronze_arrival(cast("DaprClient", dapr), head, {"data": _bronze_event()})) == {"status": "SUCCESS"}

    trigger = next(p["data"] for p in dapr.published if p["topic"] == head.bronze_topic)
    assert "from_uri" not in trigger


@respx.mock
def test_a_vended_location_outside_the_read_root_is_still_refused(tmp_path: Path) -> None:
    """THE CONFINEMENT GUARD STILL APPLIES. `from_uri` is honoured by OPENING it with the mover's own
    object-store credentials, so a location outside the resolved root is DROPped — never silently
    swapped for the composed path, which would transform a dataset nobody asked for."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    seed_bronze(str(wh / "medallion" / "bronze"), {}, rows=2)
    _describe(str(tmp_path / "elsewhere" / "acme-bronze$events"))
    dapr = _FakeDapr()
    decoys = {ns: str(tmp_path / f"default-{ns}") for ns in ("bronze", "silver")}

    head = _head_settings(control, tmp_path / "default-bronze")
    assert asyncio.run(handle_bronze_arrival(cast("DaprClient", dapr), head, {"data": _bronze_event()})) == {"status": "SUCCESS"}
    trigger = next(p["data"] for p in dapr.published if p["topic"] == head.bronze_topic)

    mover = _mover_settings(control, decoys)
    assert asyncio.run(handle_stage(cast("DaprClient", dapr), mover, {"data": trigger})) == {"status": "DROP"}
    assert not Path(wh / "medallion" / "silver").exists()
