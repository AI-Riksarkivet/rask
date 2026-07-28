"""End-to-end test of the fake-Ray medallion CASCADE (#25) — bronze → silver → gold, in-process.

R23: bronze is the FIRST governed tier — the producer ingests straight into it (raw is the external
world and owns no dataset). This is the regression guard for "the event-driven loop produces real DATA +
a correct lineage CHAIN". It runs the producer + both stage movers in sequence with the fake-Ray compute
ON, against a temp directory (real Lance, no S3/Dapr/AGE), capturing every emitted OpenLineage event, and
asserts BOTH halves:

* **Data** — each stage's Lance dataset really exists, the original rows flow all the way to gold, and each
  hop is stamped with its stage; the lineage carries the real (advancing) Lance versions.
* **Lineage** — the captured events parse as the lineage service's ``RunEvent`` and form the
  ``bronze → silver → gold`` ``DERIVED_FROM`` chain (each hop's input is the previous hop's output),
  i.e. the exact graph the lineage consumer would ingest.

The Dapr pub/sub fan-out + AGE ingest are exercised by the gated live e2e
(``tests/e2e-py/test_medallion_e2e.py``); here we prove the compute + lineage contract the whole cascade
rests on, runnably and deterministically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast

import lance
import pytest
from dapr.aio.clients import DaprClient
from lineage.models import Dataset, RunEvent
from medallion.core.config import MedallionSettings
from medallion.services.compute import seed_bronze
from medallion.services.ingest_trigger import handle_bronze_arrival
from medallion.services.produce import produce
from medallion.services.transform import handle_stage

from service_kit.lakehouse import warehouse_registry
from service_kit.lakehouse.warehouse_registry import UnresolvableProjectError


class _FakeDapr:
    """Captures every published event across the whole cascade (lineage emits + stage triggers)."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


# The medallion DAG as (operation, from_ns, from_ds, to_ns, to_ds) — the same shape the chart wires per mover.
_HOPS = [
    ("embed", "bronze", "bronze$events", "silver", "silver$features"),
    ("aggregate", "silver", "silver$features", "gold", "gold$catalog"),
]


def test_cascade_produces_real_data_and_a_correct_lineage_chain(tmp_path: Any) -> None:
    uris = {ns: str(tmp_path / ns) for ns in ("bronze", "silver", "gold")}
    dapr = _FakeDapr()

    # Head of the pipeline: lance-ray seeds bronze$events DIRECTLY (real Lance write — R23).
    producer = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": uris["bronze"]})
    asyncio.run(produce(cast(DaprClient, dapr), producer))
    bronze_tbl = lance.dataset(uris["bronze"]).to_table()
    bronze_rows = bronze_tbl.num_rows
    assert bronze_rows > 0
    # The retired raw→bronze mover's stage stamp merged into the bronze ingest head (R23).
    assert set(bronze_tbl.column("stage").to_pylist()) == {"bronze"}

    # The 2 movers, each reading its upstream Lance dataset and writing the downstream one.
    for op, from_ns, from_ds, to_ns, to_ds in _HOPS:
        settings = MedallionSettings.model_validate(
            {
                "compute_enabled": True,
                "from_uri": uris[from_ns],
                "to_uri": uris[to_ns],
                "from_namespace": from_ns,
                "from_dataset": from_ds,
                "to_namespace": to_ns,
                "to_dataset": to_ds,
                "operation": op,
                "pub_topic": "" if to_ns == "gold" else f"medallion.{to_ns}",
            }
        )
        result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok"}}))
        assert result == {"status": "SUCCESS"}

    # --- Data: the original rows flowed all the way to gold, stamped at each stage. ---
    gold = lance.dataset(uris["gold"]).to_table()
    assert gold.num_rows == bronze_rows  # no rows lost across the cascade
    assert set(gold.column("stage").to_pylist()) == {"gold"}

    # --- Lineage: the emitted events form the bronze → silver → gold DERIVED_FROM chain. ---
    events = [RunEvent.model_validate(p["data"]) for p in dapr.published if p["topic"] == producer.lineage_topic]
    by_output = {e.outputs[0].name: e for e in events}
    assert set(by_output) == {"bronze$events", "silver$features", "gold$catalog"}
    assert by_output["bronze$events"].inputs == []  # the dummy seed has no external source
    assert by_output["silver$features"].inputs[0].name == "bronze$events"
    assert by_output["gold$catalog"].inputs[0].name == "silver$features"
    # Every successful hop carries the real Lance version on its output (the WROTE edge the graph records).
    for name, ns in (("bronze$events", "bronze"), ("silver$features", "silver"), ("gold$catalog", "gold")):
        assert by_output[name].output_version(name) == str(lance.dataset(uris[ns]).version)


def test_source_rowid_traces_every_derived_row_back_to_its_exact_bronze_source_row(tmp_path: Any) -> None:
    """#38a ROOT PROVENANCE (re-rooted by R23): every silver/gold row carries ``source_rowid`` = the stable
    ``_rowid`` of the BRONZE row it descends from, so a gold row names its exact root in ONE join — not a
    hop-by-hop walk.

    Minted at the first derive (silver ← the bronze row's reserved ``_rowid`` metacolumn) and CARRIED
    UNCHANGED across gold (NOT re-set to each parent's ``_rowid``). The columnLineage edge tells the two
    apart: the first derive declares ``source_rowid ← bronze$events._rowid``, every carried stage
    ``source_rowid ← source_rowid``.
    """
    uris = {ns: str(tmp_path / ns) for ns in ("bronze", "silver", "gold")}
    dapr = _FakeDapr()
    producer = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": uris["bronze"]})
    asyncio.run(produce(cast(DaprClient, dapr), producer))

    # The bronze row identities the whole cascade must be able to name — captured AT seed time
    # (id → _rowid), because _rowid advances on any later overwrite, so only a snapshot taken now is
    # join-valid.
    bronze_tbl = lance.dataset(uris["bronze"]).to_table(with_row_id=True)
    root_rowid_by_id = dict(zip(bronze_tbl.column("id").to_pylist(), bronze_tbl.column("_rowid").to_pylist(), strict=True))
    assert "source_rowid" not in bronze_tbl.column_names  # bronze IS the root — it carries none of its own

    for op, from_ns, from_ds, to_ns, to_ds in _HOPS:
        settings = MedallionSettings.model_validate(
            {
                "compute_enabled": True,
                "from_uri": uris[from_ns],
                "to_uri": uris[to_ns],
                "from_namespace": from_ns,
                "from_dataset": from_ds,
                "to_namespace": to_ns,
                "to_dataset": to_ds,
                "operation": op,
                "pub_topic": "" if to_ns == "gold" else f"medallion.{to_ns}",
            }
        )
        assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}})) == {"status": "SUCCESS"}

    # DATA: every stage names the SAME root bronze row per id — silver AND gold, unchanged.
    for ns in ("silver", "gold"):
        tbl = lance.dataset(uris[ns]).to_table()
        assert "source_rowid" in tbl.column_names, f"{ns} dropped row-level provenance"
        by_id = dict(zip(tbl.column("id").to_pylist(), tbl.column("source_rowid").to_pylist(), strict=True))
        assert by_id == root_rowid_by_id, f"{ns} lost or rewrote the root source_rowid"

    # LINEAGE: the first derive MINTS the edge from the reserved _rowid; a carried stage declares IDENTITY.
    pubs = {p["data"]["outputs"][0]["name"]: p["data"] for p in dapr.published if p["topic"] == producer.lineage_topic}

    def _srid_edge(output_name: str) -> tuple[str, str, str]:
        out = Dataset.model_validate(pubs[output_name]["outputs"][0])
        edge = next(e for e in out.column_edges if e["out_field"] == "source_rowid")
        return edge["name"], edge["field"], edge["subtype"]

    assert _srid_edge("silver$features") == ("bronze$events", "_rowid", "IDENTITY")  # minted at the first derive
    assert _srid_edge("gold$catalog") == ("silver$features", "source_rowid", "IDENTITY")  # carried forward


# ── #84 per-tenant routing: project propagation, fail-closed drops, byte-identical default ─────────


@pytest.fixture(autouse=True)
def _fresh_registry_cache() -> Any:
    warehouse_registry.clear_cache()
    yield
    warehouse_registry.clear_cache()


def _provision(control: Path, project: str, root: Path) -> None:
    """One ACTIVE warehouse record for ``project``, in the catalog registry's on-disk shape."""
    registry = control / "_warehouses"
    registry.mkdir(parents=True, exist_ok=True)
    record = {"id": "wh1", "project": project, "root_uri": str(root), "status": "active"}
    (registry / "wh1.json").write_text(json.dumps(record))


def _mover_settings(hop: tuple[str, str, str, str, str], uris: dict[str, str], **extra: Any) -> MedallionSettings:
    op, from_ns, from_ds, to_ns, to_ds = hop
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_uri": uris[from_ns],
            "to_uri": uris[to_ns],
            "from_namespace": from_ns,
            "from_dataset": from_ds,
            "to_namespace": to_ns,
            "to_dataset": to_ds,
            "operation": op,
            "pub_topic": "" if to_ns == "gold" else f"medallion.{to_ns}",
            **extra,
        }
    )


def test_project_cascade_routes_into_the_project_warehouse_and_qualifies_lineage(tmp_path: Any) -> None:
    """#84 happy path, head→gold: /produce {project} seeds the PROJECT warehouse, /bronze-arrival copies
    the project onto the stage trigger, every mover resolves its URIs off the registry (the env decoy URIs
    are never touched), each next-trigger PROPAGATES the project, and the lineage chain is
    project-qualified end to end (distinct graph nodes per tenant)."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    # Env-derived defaults every stage would use WITHOUT the project — they must stay untouched.
    decoys = {ns: str(tmp_path / f"default-{ns}") for ns in ("bronze", "silver", "gold")}
    dapr = _FakeDapr()

    producer = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": decoys["bronze"], "control_root": str(control)})
    result = asyncio.run(produce(cast(DaprClient, dapr), producer, project="acme"))
    assert result["status"] == "produced" and result["dataset"] == "acme-bronze$events"
    assert lance.dataset(str(wh / "medallion" / "bronze")).to_table().num_rows > 0
    assert not Path(decoys["bronze"]).exists()  # fail-closed routing: the shared root was never written

    bronze_event = next(p["data"] for p in dapr.published if p["topic"] == producer.lineage_topic)
    assert bronze_event["outputs"][0]["namespace"] == "acme-bronze"
    assert bronze_event["outputs"][0]["name"] == "acme-bronze$events"
    assert bronze_event["run"]["facets"]["lance"]["project"] == "acme"

    # The cascade HEAD: the bronze-arrival subscription matches the QUALIFIED pair + propagates the project.
    assert asyncio.run(handle_bronze_arrival(cast(DaprClient, dapr), producer, {"data": bronze_event})) == {"status": "SUCCESS"}
    head = dapr.published[-1]
    assert head["topic"] == producer.bronze_topic and head["data"]["project"] == "acme"

    trigger = head["data"]
    for hop in _HOPS:
        to_ns = hop[3]
        settings = _mover_settings(hop, decoys, control_root=str(control))
        assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": trigger})) == {"status": "SUCCESS"}
        assert lance.dataset(str(wh / "medallion" / to_ns)).to_table().num_rows > 0
        assert not Path(decoys[to_ns]).exists()
        if settings.pub_topic:
            trigger = dapr.published[-1]["data"]
            assert trigger["project"] == "acme"  # propagated down the whole cascade

    gold = lance.dataset(str(wh / "medallion" / "gold")).to_table()
    assert set(gold.column("stage").to_pylist()) == {"gold"}

    events = [RunEvent.model_validate(p["data"]) for p in dapr.published if p["topic"] == producer.lineage_topic]
    by_output = {e.outputs[0].name: e for e in events}
    assert set(by_output) == {
        "acme-bronze$events",
        "acme-silver$features",
        "acme-gold$catalog",
    }
    assert by_output["acme-silver$features"].inputs[0].name == "acme-bronze$events"
    assert by_output["acme-gold$catalog"].inputs[0].name == "acme-silver$features"
    assert by_output["acme-gold$catalog"].outputs[0].namespace == "acme-gold"


def test_project_trigger_with_routing_disabled_is_dropped_fail_closed(tmp_path: Any) -> None:
    """No MEDALLION_CONTROL_ROOT + a project-carrying trigger → DROP with NOTHING emitted or written —
    falling back to the default roots would transform the wrong tenant's data under real-looking lineage."""
    uris = {ns: str(tmp_path / ns) for ns in ("bronze", "silver")}
    dapr = _FakeDapr()
    settings = _mover_settings(_HOPS[0], uris)  # control_root unset (the default)
    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t", "project": "acme"}}))
    assert status == {"status": "DROP"}
    assert dapr.published == []  # no lineage emit, no next trigger
    assert not Path(uris["silver"]).exists()  # and the default root was never written


def test_project_trigger_with_no_active_warehouse_records_fail_and_drops(tmp_path: Any) -> None:
    control = tmp_path / "control"
    (control / "_warehouses").mkdir(parents=True)  # registry exists, but no warehouse for the project
    uris = {ns: str(tmp_path / ns) for ns in ("bronze", "silver")}
    dapr = _FakeDapr()
    settings = _mover_settings(_HOPS[0], uris, control_root=str(control))
    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t", "project": "ghost"}}))
    assert status == {"status": "DROP"}
    assert not Path(uris["silver"]).exists()
    (fail,) = [p["data"] for p in dapr.published if p["topic"] == settings.lineage_topic]
    assert fail["eventType"] == "FAIL"  # the audit trail: the refused run is recorded, project-qualified
    assert fail["outputs"][0] == {"namespace": "ghost-silver", "name": "ghost-silver$features"}
    assert "no active warehouse" in fail["run"]["facets"]["errorMessage"]["message"]


def test_unsafe_project_in_trigger_is_dropped_without_any_emit(tmp_path: Any) -> None:
    dapr = _FakeDapr()
    settings = _mover_settings(_HOPS[0], {"bronze": str(tmp_path / "bronze"), "silver": str(tmp_path / "silver")})
    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t", "project": "../evil"}}))
    assert status == {"status": "DROP"} and dapr.published == []


def test_the_default_config_still_cascades_with_compute_off(tmp_path: Any) -> None:
    """The regression guard for the synthetic-emit change, on the CHART'S DEFAULT config.

    `chart/values.yaml` defaults `compute.enabled: false`, so the provenance-only path is what most
    deployments actually run. Marking those events synthetic made their output BARE — no version,
    stats, dataSource or schema facet — and `/bronze-arrival` decides whether to fire the cascade by
    matching `_bronze_write_dataset` against the event's outputs. Had that matcher depended on any
    facet rather than on namespace+name, this change would have silently stopped the entire default
    pipeline at its head: producer reports success, no trigger, no mover, empty graph, nothing logged
    above DEBUG anywhere in the chain.

    Asserted end to end (produce → arrival → trigger) rather than by reading the matcher, because the
    question is whether the two halves still agree, not what either one says in isolation.
    """
    dapr = _FakeDapr()
    producer = MedallionSettings.model_validate({"compute_enabled": False, "bronze_uri": str(tmp_path / "bronze")})

    assert asyncio.run(produce(cast(DaprClient, dapr), producer, token="t1"))["status"] != "publish_failed"
    bronze_event = next(p for p in dapr.published if p["topic"] == producer.lineage_topic)["data"]

    # The head event is synthetic and describes no data — and is still a COMPLETE naming its output.
    assert bronze_event["run"]["facets"]["lance"]["synthetic"] is True
    assert bronze_event["outputs"][0].get("facets", {}) == {}
    assert bronze_event["eventType"] == "COMPLETE"

    # …and the arrival handler still recognises it and fires the cascade.
    assert asyncio.run(handle_bronze_arrival(cast(DaprClient, dapr), producer, {"data": bronze_event})) == {"status": "SUCCESS"}
    trigger = next(p for p in dapr.published if p["topic"] == producer.bronze_topic)
    assert trigger["data"]["dataset"] == producer.bronze_dataset
    assert trigger["data"]["token"] == "t1"


def test_page_lane_arrival_does_not_fire_the_events_lane_mover(tmp_path: Any) -> None:
    """P7a lane isolation: a ``bronze$pages`` arrival must not drive the ``bronze$events`` mover.

    Both ingest lanes publish to the SAME ``medallion.bronze`` topic, so every mover subscribed to it
    sees every bronze arrival. ``ingest_trigger`` already put the discriminator on the wire — its own
    docstring says the returned name is "the one actually written, so the trigger tells the mover which
    lane fired" — and ``handle_stage`` read only ``token`` and ``project``, so the events mover woke on a
    page arrival and transformed ``bronze$events``: real-looking lineage for a run nothing asked for,
    attributed to the page cascade's token.

    A mismatch is deterministic — redelivery cannot make this the right mover — so it DROPs.
    """
    # bronze$events is seeded on purpose: without it the mover merely ERRORS on a missing dataset, which
    # would hide the actual defect behind an accident. Seeded, the spurious run SUCCEEDS end to end —
    # writing silver and emitting a COMPLETE — which is exactly the damage being fixed.
    seed_bronze(str(tmp_path / "bronze"), {}, rows=2)
    dapr = _FakeDapr()
    settings = _mover_settings(_HOPS[0], {"bronze": str(tmp_path / "bronze"), "silver": str(tmp_path / "silver")})
    trigger = {"data": {"token": "t", "dataset": "bronze$pages", "namespace": "bronze"}}

    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))

    assert status == {"status": "DROP"}
    assert dapr.published == [], f"the events-lane mover emitted on a page arrival: {dapr.published}"
    assert not (tmp_path / "silver").exists(), "the events-lane mover wrote silver from a page trigger"


def test_the_dropped_lane_is_observable(tmp_path: Any, caplog: Any) -> None:
    """Dropping quietly would delete the signal that the page lane has no consumer.

    A DROP is an ack — Dapr neither redelivers nor dead-letters — so if the app logs nothing and counts
    nothing, a completed IIIF ingest simply vanishes. That matters concretely: before the lane guard, a
    ``bronze$pages`` arrival drove the events mover into a deterministic FAIL, and
    ``docs/architecture/live-proof-2026-07-28.md`` used that FAIL as its evidence that the P7b page lane
    was unlanded. Asserted at INFO because the mover's logger is configured to INFO
    (``configure_app_logging``), so a DEBUG record would never be emitted at all.
    """
    seed_bronze(str(tmp_path / "bronze"), {}, rows=2)
    dapr = _FakeDapr()
    settings = _mover_settings(_HOPS[0], {"bronze": str(tmp_path / "bronze"), "silver": str(tmp_path / "silver")})

    with caplog.at_level(logging.INFO, logger="medallion.services.transform"):
        asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t", "dataset": "bronze$pages"}}))

    record = next((r for r in caplog.records if r.message == "medallion_stage_other_lane"), None)
    assert record is not None, f"the drop left no INFO record: {[r.message for r in caplog.records]}"
    assert record.arrived == "bronze$pages" and record.expects == "bronze$events"


def test_matching_lane_trigger_still_runs(tmp_path: Any) -> None:
    """The other half of the guard: the discriminator must not reject the lane it belongs to.

    Cheap to state and the reason a naive `if dataset != from_dataset: DROP` is not obviously safe —
    the trigger carries the RAW dataset name while the mover's own ``from_dataset`` is
    project-QUALIFIED, so comparing the wrong one silently drops every tenant trigger.
    """
    seed_bronze(str(tmp_path / "bronze"), {}, rows=2)
    dapr = _FakeDapr()
    settings = _mover_settings(_HOPS[0], {"bronze": str(tmp_path / "bronze"), "silver": str(tmp_path / "silver")})
    trigger = {"data": {"token": "t", "dataset": "bronze$events", "namespace": "bronze"}}

    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))

    assert status == {"status": "SUCCESS"}
    assert any(p["topic"] == "medallion.silver" for p in dapr.published)


def test_trigger_without_a_dataset_is_still_accepted(tmp_path: Any) -> None:
    """Absent discriminator → no claim → proceed. An external publisher (or a pre-P7a trigger still in
    a queue at rollout) omits ``dataset`` entirely; the guard rejects a WRONG lane, not an unstated one."""
    seed_bronze(str(tmp_path / "bronze"), {}, rows=2)
    dapr = _FakeDapr()
    settings = _mover_settings(_HOPS[0], {"bronze": str(tmp_path / "bronze"), "silver": str(tmp_path / "silver")})

    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}}))

    assert status == {"status": "SUCCESS"}


def test_produce_with_project_fails_closed_when_unresolvable(tmp_path: Any) -> None:
    dapr = _FakeDapr()
    # Routing disabled (no control_root) → refused before any write or publish.
    producer = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": str(tmp_path / "bronze")})
    with pytest.raises(UnresolvableProjectError):
        asyncio.run(produce(cast(DaprClient, dapr), producer, project="acme"))
    # Routing enabled but the project has no active warehouse → equally refused.
    (tmp_path / "control" / "_warehouses").mkdir(parents=True)
    configured = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": str(tmp_path / "bronze"), "control_root": str(tmp_path / "control")})
    with pytest.raises(UnresolvableProjectError):
        asyncio.run(produce(cast(DaprClient, dapr), configured, project="acme"))
    assert dapr.published == [] and not (tmp_path / "bronze").exists()


def test_projectless_cascade_is_byte_identical_even_with_routing_configured(tmp_path: Any) -> None:
    """The default-path regression: with MEDALLION_CONTROL_ROOT configured AND a warehouse provisioned, a
    project-LESS produce/trigger must behave exactly as today — env URIs, unqualified lineage, no project
    facet, and trigger payloads EXACTLY equal to the pre-#84 three-field shape."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    uris = {ns: str(tmp_path / ns) for ns in ("bronze", "silver")}
    dapr = _FakeDapr()

    producer = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": uris["bronze"], "control_root": str(control)})
    asyncio.run(produce(cast(DaprClient, dapr), producer))
    bronze_event = next(p["data"] for p in dapr.published if p["topic"] == producer.lineage_topic)
    assert bronze_event["outputs"][0]["namespace"] == "bronze"
    assert bronze_event["outputs"][0]["name"] == "bronze$events"
    assert "project" not in bronze_event["run"]["facets"]["lance"]
    token = bronze_event["run"]["facets"]["lance"]["token"]

    assert asyncio.run(handle_bronze_arrival(cast(DaprClient, dapr), producer, {"data": bronze_event})) == {"status": "SUCCESS"}
    # EXACT equality — the head trigger is the old three-field payload, no project key.
    assert dapr.published[-1]["data"] == {"token": token, "dataset": "bronze$events", "namespace": "bronze"}

    settings = _mover_settings(_HOPS[0], uris, control_root=str(control))
    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": dapr.published[-1]["data"]})) == {"status": "SUCCESS"}
    # The env URI was used; the provisioned project warehouse stayed untouched.
    assert lance.dataset(uris["silver"]).to_table().num_rows > 0
    assert not (wh / "medallion").exists()
    # EXACT equality on the next-stage trigger too.
    assert dapr.published[-1]["data"] == {"token": token, "dataset": "silver$features", "namespace": "silver"}
    silver_event = next(p["data"] for p in dapr.published if p["topic"] == settings.lineage_topic and p["data"]["outputs"][0]["name"] == "silver$features")
    assert silver_event["outputs"][0]["namespace"] == "silver"
    assert "project" not in silver_event["run"]["facets"]["lance"]


# ── gold serving warehouse: the terminal mover's tenant target root (DECISIONS "Medallion tiers") ──


def _provision_gold(control: Path, project: str, root: Path) -> None:
    """One ACTIVE gold SERVING warehouse record for ``project`` (the ``serving: gold`` registry shape)."""
    registry = control / "_warehouses"
    registry.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "gold1",
        "project": project,
        "root_uri": str(root),
        "status": "active",
        "serving": "gold",
    }
    (registry / "gold1.json").write_text(json.dumps(record))


def _seed_silver(work_wh: Path) -> None:
    """A tiny upstream silver dataset in the WORK warehouse for the silver→gold hop to read."""
    import pyarrow as pa

    table = pa.table({"id": [1, 2], "stage": ["silver", "silver"], "source_rowid": [0, 1]})
    lance.write_dataset(table, str(work_wh / "medallion" / "silver"), mode="overwrite")


@pytest.mark.parametrize(
    ("flag_on", "gold_present", "expect_gold_bucket"),
    [
        (True, True, True),  # the only combination that retargets
        (True, False, False),  # no serving warehouse → fall back to the work root, byte-identical
        (False, True, False),  # flag off → the gold record is ignored entirely
        (False, False, False),
    ],
)
def test_gold_mover_target_selection(tmp_path: Any, flag_on: bool, gold_present: bool, expect_gold_bucket: bool) -> None:
    """The silver→gold mover's TENANT target root: retargets to the project's gold serving warehouse
    ONLY when MEDALLION_GOLD_WAREHOUSE_ENABLED is on AND a serving=="gold" record exists — every other
    combination is byte-identical work-warehouse behavior (and the read side ALWAYS stays in work)."""
    control, work_wh, gold_wh = tmp_path / "control", tmp_path / "acme-wh", tmp_path / "acme-gold"
    _provision(control, "acme", work_wh)
    if gold_present:
        _provision_gold(control, "acme", gold_wh)
    _seed_silver(work_wh)
    decoys = {ns: str(tmp_path / f"default-{ns}") for ns in ("silver", "gold")}
    dapr = _FakeDapr()

    settings = _mover_settings(_HOPS[1], decoys, control_root=str(control), gold_warehouse_enabled=flag_on)
    trigger = {"data": {"token": "t", "project": "acme"}}
    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger)) == {"status": "SUCCESS"}

    gold_in_serving = gold_wh / "medallion" / "gold"
    gold_in_work = work_wh / "medallion" / "gold"
    if expect_gold_bucket:
        assert lance.dataset(str(gold_in_serving)).to_table().num_rows == 2
        assert not gold_in_work.exists()  # the work warehouse never got the serving data
    else:
        assert lance.dataset(str(gold_in_work)).to_table().num_rows == 2
        assert not gold_in_serving.exists()
    assert not Path(decoys["gold"]).exists()  # the shared env root is never touched on a tenant trigger

    # The lineage/FGA identities do not move with the physical root: still the project-qualified names.
    event = next(p["data"] for p in dapr.published if p["topic"] == settings.lineage_topic)
    assert event["outputs"][0]["namespace"] == "acme-gold"
    assert event["outputs"][0]["name"] == "acme-gold$catalog"
