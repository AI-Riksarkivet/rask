"""Lineage ingest + query end-to-end against a live Apache AGE database.

Skipped unless ``LINEAGE_DATABASE_URL`` points at a running AGE Postgres, e.g.:

    docker compose -f .docker/docker-compose.yml -f .docker/docker-compose.lineage.yml up -d lineage-postgres
    LINEAGE_DATABASE_URL=postgresql://lineage:lineage@localhost:5433/lineage \
        uv run pytest tests/e2e-py/test_lineage_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from lineage.schemas import Readers

DSN = os.environ.get("LINEAGE_DATABASE_URL", "")
_SAMPLE = Path(__file__).resolve().parents[2] / "services" / "lineage" / "src" / "lineage" / "sample_events.json"

#: The dataset names the shared sample writes — and the whole problem, because they are the SAME
#: generic names the production cascade writes. The fixture seeded them unprefixed into whatever graph
#: the DSN pointed at, then asserted over "every producer of silver$features" and "every column of
#: silver$features" — so on any estate that has ever run the cascade it measured the estate and called
#: it the fixture. It was a clean-room test wearing an e2e's name: red exactly where the code is
#: healthy, and proving nothing where it matters.
#:
#: These four are the ONLY strings `_Sample` rewrites, which is what makes the rewrite safe to do by
#: walking every `"name"` key: no column and no job in the sample is called any of them.
_SAMPLE_DATASETS = frozenset({"raw_events", "bronze$events", "silver$features", "gold$catalog"})


class _Sample:
    """The shared medallion sample, renamed into ONE run's own corner of the graph.

    Datasets AND run ids, because both collide and they collide with different things. The dataset
    names collide with PRODUCTION. The run ids collide with THIS SUITE'S OWN PREVIOUS RUNS — they are
    fixed uuids in the fixture, so a second run re-points the same `Run` node at a second set of
    datasets and any assertion of the form `run.outputs == [...]` starts accumulating.

    Run ids are derived with `uuid5` rather than randomised so they stay real uuids and stay stable
    within one module run, which is what lets the discovery test still look a known run up by name.
    """

    _NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

    def __init__(self) -> None:
        self._prefix = f"e2e{uuid.uuid4().hex[:8]}-"

    def ds(self, name: str) -> str:
        """A sample dataset's name in this run. Anything else passes through untouched."""
        return f"{self._prefix}{name}" if name in _SAMPLE_DATASETS else name

    def run_id(self, original: str) -> str:
        return str(uuid.uuid5(self._NS, self._prefix + original))

    def events(self) -> list[object]:
        def walk(node: object) -> object:
            if isinstance(node, dict):
                out = {}
                for key, value in node.items():
                    if key == "name" and isinstance(value, str):
                        out[key] = self.ds(value)
                    elif key == "runId" and isinstance(value, str):
                        out[key] = self.run_id(value)
                    else:
                        out[key] = walk(value)
                return out
            if isinstance(node, list):
                return [walk(x) for x in node]
            return node

        return [walk(e) for e in json.loads(_SAMPLE.read_text())]


@pytest.fixture(scope="module")
def sample() -> _Sample:
    """One namespace per module run — every sample-loading test below shares it."""
    return _Sample()


pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def dsn() -> str:
    if not DSN:
        pytest.skip("set LINEAGE_DATABASE_URL (a live Apache AGE Postgres) to run the lineage e2e")
    return DSN


def test_medallion_ingest_and_lineage_queries(dsn: str, sample: _Sample) -> None:
    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.schemas import LineageGraph, Neighbors, Producers
    from lineage.services.repository import LineageRepository

    events = [RunEvent.model_validate(e) for e in sample.events()]

    async def run() -> tuple[Neighbors, Neighbors, Producers, LineageGraph]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            for event in events:
                await repo.ingest_event(event)
            upstream = await repo.upstream(sample.ds("gold$catalog"))
            downstream = await repo.downstream(sample.ds("raw_events"))
            producers = await repo.producers(sample.ds("silver$features"))
            graph = await repo.graph(sample.ds("silver$features"))
            return upstream, downstream, producers, graph
        finally:
            await pool.close()

    upstream, downstream, producers, graph = asyncio.run(run())

    # gold derives (transitively) from silver, bronze and the raw source.
    assert {sample.ds("silver$features"), sample.ds("bronze$events"), sample.ds("raw_events")} <= {d.name for d in upstream.related}
    # raw_events flows downstream into bronze/silver/gold.
    assert {sample.ds("bronze$events"), sample.ds("silver$features"), sample.ds("gold$catalog")} <= {d.name for d in downstream.related}
    # silver was written by data_eng across all attempts (failed embed, embed v1, caption v2).
    # EQUALITY is the right relation and is now safe to assert: the dataset belongs to this run alone,
    # so "every producer" means the sample's three and nothing the estate did. See `_SAMPLE_DATASETS`.
    assert len(producers.producers) == 3, f"the sample's three silver writes should be the only ones: {producers.producers}"
    assert {p.author for p in producers.producers} == {"data_eng"}
    assert {p.dataset_version for p in producers.producers} >= {"1", "2"}
    # the failed embed attempt is recorded too: a FAIL run with an error and no produced version.
    failed = [p for p in producers.producers if p.event_type and "FAIL" in p.event_type.upper()]
    assert failed and failed[0].error_message and "OOM" in failed[0].error_message
    assert failed[0].dataset_version is None
    # the graph around silver spans the connected medallion DAG, with storage + tags on each node.
    node_ids = {n.id for n in graph.nodes}
    assert {sample.ds(n) for n in _SAMPLE_DATASETS} <= node_ids
    silver_node = next(n for n in graph.nodes if n.id == sample.ds("silver$features"))
    assert silver_node.source_uri == "s3://lakehouse/silver/features"
    assert "layer=silver" in silver_node.tags
    # silver derives from bronze, gold from silver; the in-place refine is NOT a self-derivation.
    edges = {(e.source, e.target) for e in graph.edges}
    assert (sample.ds("silver$features"), sample.ds("bronze$events")) in edges
    assert (sample.ds("gold$catalog"), sample.ds("silver$features")) in edges
    assert (sample.ds("silver$features"), sample.ds("silver$features")) not in edges


def test_run_inputs_pin_versions_against_age(dsn: str) -> None:
    """Reproducibility (#115 D1): a run's PINNED input versions round-trip through real AGE — the READ-edge
    ``version`` the ingest sets on ``(:Run)-[:READ {version}]->(:Dataset)``, read back by ``run_inputs``.

    Only unit-proven before (the endpoint test uses a fake repo); this drives the real Cypher
    (_LINK_READ + _SET_READ_VERSION on ingest, _RUN_INPUTS on read) so a graph quirk that dropped the pin
    — as the live graph once did (280 READ edges, zero versions) — fails HERE, not silently in prod."""
    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.services.repository import LineageRepository

    rid = "22222222-2222-5222-8222-222222222222"
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-13T09:00:00+00:00",
            "producer": "https://github.com/Borg93/lance-ns",
            "run": {"runId": rid, "facets": {}},
            "job": {"namespace": "ray-jobs", "name": "train.e2e_pins"},
            # one PINNED read (silver@28) + one floating read (gold, no pin) — the pin must survive, the
            # floating read must stay versionless (a fabricated pin would be worse than none).
            "inputs": [
                {
                    "namespace": "silver",
                    "name": "silver$features",
                    "facets": {"version": {"datasetVersion": "28"}},
                },
                {"namespace": "gold", "name": "gold$catalog", "facets": {}},
            ],
            "outputs": [
                {
                    "namespace": "models",
                    "name": "models$e2e_pins",
                    "facets": {"version": {"datasetVersion": "3"}},
                }
            ],
        }
    )

    async def run() -> list:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            await repo.ingest_event(event)
            return (await repo.run_inputs(rid)).inputs
        finally:
            await pool.close()

    inputs = {i.name: i.version for i in asyncio.run(run())}
    assert inputs.get("silver$features") == "28", f"pinned READ version lost through AGE: {inputs}"
    assert "gold$catalog" in inputs and inputs["gold$catalog"] is None, f"a floating read must stay versionless (no fabricated pin): {inputs}"


def test_ensure_graph_bootstraps_and_is_idempotent_against_age(dsn: str) -> None:
    """Prod-readiness P2: ensure_graph is the ONLY graph bootstrap on the external managed-PG path (the
    in-cluster age-postgres init SQL runs create_graph, a managed PG has none). Proven against real AGE: a
    FRESH graph name is CREATED, a second call is a no-op (a bare create_graph would ERROR 'already exists'),
    and the registry ends with exactly one. Concurrency-safety (the create race) is covered by the checked-
    then-created path; here we prove the create + idempotency end to end."""
    import uuid
    from contextlib import suppress

    from psycopg import sql

    from lineage.core.age import make_pool
    from lineage.services.repository import LineageRepository

    gname = f"e2e_ensure_{uuid.uuid4().hex[:8]}"

    async def run() -> int:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, gname)
            await repo.ensure_graph()  # absent → created
            await repo.ensure_graph()  # present → idempotent no-op (no 'already exists' raised)
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (gname,))
                row = await cur.fetchone()
                with suppress(Exception):  # best-effort cleanup so the throwaway graph doesn't accumulate
                    await conn.execute(sql.SQL("SELECT drop_graph({}, true)").format(sql.Literal(gname)))
            return row[0] if row else 0
        finally:
            await pool.close()

    assert asyncio.run(run()) == 1


def test_discovery_lists_against_age(dsn: str, sample: _Sample) -> None:
    """GOAL 4 A1/A2: the browse lists run their REAL Cypher against AGE (not a mocked ``run_cypher``).

    Ingest the medallion sample, then exercise ``list_datasets`` (all + namespace / tag filters) and
    ``list_jobs`` (the job -> written-datasets fold) — the discovery reads a normal ``uv run pytest``
    otherwise never executes against a database.
    """
    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.services.repository import LineageRepository

    events = [RunEvent.model_validate(e) for e in sample.events()]

    async def run() -> tuple[list, list, list, list, list]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            for event in events:
                await repo.ingest_event(event)
            all_ds = await repo.list_datasets()
            silver = await repo.list_datasets(namespace="silver")
            tagged = await repo.list_datasets(tag="layer=silver")
            jobs = await repo.list_jobs()
            runs = (await repo.list_runs()).runs
            return all_ds, silver, tagged, jobs, runs
        finally:
            await pool.close()

    all_ds, silver, tagged, jobs, runs = asyncio.run(run())

    # /datasets — every medallion dataset is listed, name-sorted, with its namespace + tags carried.
    names = [d.name for d in all_ds]
    assert {sample.ds(n) for n in _SAMPLE_DATASETS} <= set(names)
    assert names == sorted(names)
    # ?namespace= filter — only silver-namespace datasets (silver$features lives there).
    assert silver and all(d.namespace == "silver" for d in silver)
    assert sample.ds("silver$features") in {d.name for d in silver}
    # ?tag= filter — exact tag membership picks up the silver dataset.
    assert sample.ds("silver$features") in {d.name for d in tagged}
    assert all("layer=silver" in d.tags for d in tagged)
    # /jobs — a job's WROTE edges are folded into its output set; some job wrote silver$features.
    assert jobs and any(sample.ds("silver$features") in j.outputs for j in jobs)
    # /runs — pin the `cypher.LIST_RUNS` RETURN column ORDER against real AGE (§7a): the unit fold test mirrors
    # a hand-built row, so a reordered RETURN would pass unit and silently scramble every field in prod.
    # Typed field-by-field assertions on known sample runs catch any transposition.
    by_id = {r.run_id: r for r in runs}
    ingest = by_id[sample.run_id("11111111-1111-1111-1111-111111111111")]
    assert ingest.state == "COMPLETE"
    assert ingest.job == "ray-jobs/ingest_events"
    assert ingest.author == "alice"  # the ownership-facet fallback (column 2 — not the state/job slots)
    assert ingest.outputs == [sample.ds("bronze$events")]
    assert ingest.events >= 1
    assert ingest.started_at and ingest.updated_at  # timestamps landed in their own slots
    # promotion_status joined the projection LAST, so an ordinary run must read None rather than
    # picking up a neighbouring column's value — the transposition this block exists to catch.
    assert ingest.promotion_status is None
    failed = by_id[sample.run_id("22222222-2222-2222-2222-222222222220")]
    assert failed.state == "FAIL"
    assert failed.error_message and "OOM" in failed.error_message  # error slot, not swapped with a timestamp


def test_reconcile_backfills_a_dropped_write(dsn: str, tmp_path: Path) -> None:
    """B4: a write whose lineage event was LOST (storage ahead of the graph) is back-filled by reconcile.

    Simulates the outbox gap end-to-end against real AGE + real Lance: record a write at v1, land a second
    version on disk WITHOUT its lineage event, then reconcile — the graph must catch up to the on-disk v2.
    """
    import lance
    import pyarrow as pa

    from lineage.core.age import make_pool, run_cypher
    from lineage.core.reconcile import read_storage_version, reconcile_all
    from lineage.models import RunEvent
    from lineage.schemas import ReconcileState
    from lineage.services.repository import LineageRepository
    from service_kit.openlineage import run_id_for

    # The back-fill run id is now a deterministic UUID (spec fix), not the readable seed string.
    backfill_rid = run_id_for("reconcile-recon$t-v2")

    uri = str(tmp_path / "recon.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)  # storage v1
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-01T00:00:00Z",
            "run": {"runId": "recon-w1"},
            "job": {"namespace": "lance-medallion", "name": "recon_ingest"},
            "inputs": [],
            "outputs": [
                {
                    "namespace": "recon",
                    "name": "recon$t",
                    "facets": {"dataSource": {"uri": uri}, "version": {"datasetVersion": "1"}},
                }
            ],
        }
    )
    # A SECOND write lands on disk but its lineage event is DROPPED (the outbox gap): storage=2, graph=1.
    lance.write_dataset(pa.table({"id": [2]}), uri, mode="append")

    async def read_only_recon(u: str) -> int | None:
        return read_storage_version(u, {}) if u == uri else None  # skip other datasets' (s3) reads

    async def run() -> tuple[int | None, ReconcileState, int | None, list, list]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            await repo.ensure_events_table()  # the back-fill now writes a feed row too
            # The AGE graph persists across runs — clear this test's dataset + runs so it starts clean
            # (else a prior back-fill leaves recon$t at v2 and the "graph behind storage" premise breaks).
            async with pool.connection() as conn:
                await run_cypher(conn, "lineage", "MATCH (d:Dataset {name:'recon$t'}) DETACH DELETE d")
                await run_cypher(
                    conn,
                    "lineage",
                    "MATCH (r:Run) WHERE r.run_id IN ['recon-w1', $rid] DETACH DELETE r",
                    {"rid": backfill_rid},
                )
            await repo.ingest_event(event)
            before = await repo.latest_write_version("recon$t")
            statuses = await reconcile_all(repo, read_only_recon, backfill=True)
            after = await repo.latest_write_version("recon$t")
            recon = next(s for s in statuses if s.dataset == "recon$t")
            # Cross-view parity (#10): the back-fill run must carry job + outputs (so /runs sees it, not
            # just producers()), and land a feed row (so /events sees it too). Read both back.
            async with pool.connection() as conn:
                rows = await run_cypher(
                    conn,
                    "lineage",
                    "MATCH (r:Run {run_id:$rid}) RETURN r.job, r.outputs",
                    {"rid": backfill_rid},
                    columns=2,
                )
                feed = await conn.execute("SELECT event_type, outputs FROM public.lineage_events WHERE run_id = %s", (backfill_rid,))
                feed_rows = await feed.fetchall()
            return before, recon.status, after, rows, feed_rows
        finally:
            await pool.close()

    before, status, after, run_rows, feed_rows = asyncio.run(run())
    assert before == 1  # the graph recorded v1 from the event
    assert status == ReconcileState.STORAGE_AHEAD  # storage v2 > graph v1 — the dropped write
    assert after == 2  # reconcile back-filled the real on-disk version
    # The back-fill run is now consistent across views: job + outputs on the graph node, and a feed row.
    assert run_rows and run_rows[0][0] and run_rows[0][1] == "recon$t"
    assert feed_rows and feed_rows[0][0] == "RECONCILED"


def test_medallion_column_lineage(dsn: str, sample: _Sample) -> None:
    """#24: field-to-field lineage ingested into real AGE then traversed (the high-risk cypher path).

    Exercises the bits unit tests can't: the ``DERIVED_FROM_COLUMN*1..`` transitive walk across datasets
    AND within one (the same-dataset caption←embedding edge), the typed HAS_COLUMN inventory, and the
    bool/scalar edge props round-tripping through AGE.
    """
    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.schemas import ColumnGraph, ColumnNeighbors
    from lineage.services.repository import LineageRepository

    events = [RunEvent.model_validate(e) for e in sample.events()]

    async def run() -> tuple[ColumnNeighbors, ColumnNeighbors, ColumnGraph]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            for event in events:
                await repo.ingest_event(event)
            up = await repo.column_upstream(sample.ds("gold$catalog"), "caption")
            down = await repo.column_downstream(sample.ds("bronze$events"), "payload")
            cg = await repo.dataset_column_graph(sample.ds("silver$features"))
            return up, down, cg
        finally:
            await pool.close()

    up, down, cg = asyncio.run(run())

    # gold.caption traces back through silver.caption -> silver.embedding -> bronze.payload
    # (a cross-dataset AND a same-dataset column hop in one transitive walk).
    up_cols = {(c.dataset, c.field) for c in up.related}
    assert {
        (sample.ds("silver$features"), "caption"),
        (sample.ds("silver$features"), "embedding"),
        (sample.ds("bronze$events"), "payload"),
    } <= up_cols
    # bronze.payload flows downstream into silver.embedding and onward to gold.caption.
    down_cols = {(c.dataset, c.field) for c in down.related}
    assert {(sample.ds("silver$features"), "embedding"), (sample.ds("gold$catalog"), "caption")} <= down_cols
    # the silver column graph carries its full typed inventory + the in-place caption<-embedding edge.
    typed = {c.field: c.type for c in cg.columns if c.dataset == sample.ds("silver$features")}
    assert {"id", "payload_src", "embedding", "caption"} <= set(typed)
    assert typed["embedding"] == "array<float>"  # the Arrow type seeded from the schema facet
    same_ds = {
        (e.source_field, e.target_field)
        for e in cg.edges
        if e.source_dataset == sample.ds("silver$features") and e.target_dataset == sample.ds("silver$features")
    }
    assert ("embedding", "caption") in same_ds  # the in-place-refinement column edge
    # the embed transformation kind rode the edge as a scalar prop.
    emb = next(e for e in cg.edges if e.target_dataset == sample.ds("silver$features") and e.target_field == "embedding")
    assert emb.transformation_subtype == "TRANSFORMATION"


def test_run_retention_prune_and_schema_at_version(dsn: str) -> None:
    """§4 hardening, provable only against real AGE: (a) ``prune_runs`` DETACH-DELETEs runs older than
    the cutoff — its DELETE has no RETURN clause, an AGE-dialect shape a fake pool can't validate;
    (b) ``dataset_schema(version=N)`` resolves per-version against real agtype comparison (the documented
    int-vs-string ``$ver`` silent-miss quirk); (c) ``ensure_graph_constraints`` applies the Column
    LOOKUP index DDL (non-unique — the deliberate no-abort-churn design) without error."""
    import uuid
    from datetime import UTC, datetime

    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.services.repository import LineageRepository

    name = f"e2e$prune_{uuid.uuid4().hex[:8]}"

    def event(run_id: str, version: int, event_time: str, fields: list[dict[str, str]]) -> RunEvent:
        return RunEvent.model_validate(
            {
                "eventType": "COMPLETE",
                "eventTime": event_time,
                "producer": "e2e",
                "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
                "run": {"runId": run_id, "facets": {"lance": {"operation": "insert", "version": version}}},
                "job": {"namespace": "e2e", "name": f"write.{name}"},
                "inputs": [],
                "outputs": [
                    {
                        "namespace": "e2e",
                        "name": name,
                        "facets": {
                            "version": {"datasetVersion": str(version)},
                            "schema": {"fields": fields},
                        },
                    }
                ],
            }
        )

    from lineage.schemas import DatasetSchema

    old_rid, new_rid = str(uuid.uuid4()), str(uuid.uuid4())

    async def run() -> tuple[DatasetSchema, DatasetSchema, int, int, set[str]]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            await repo.ensure_graph_constraints()  # exercises the new Column lookup-index DDL too
            # v1 long ago (prunable), v2 now (kept) — with DIFFERENT schemas per version.
            await repo.ingest_event(event(old_rid, 1, "2000-01-01T00:00:00+00:00", [{"name": "id", "type": "int64"}]))
            await repo.ingest_event(
                event(
                    new_rid,
                    2,
                    datetime.now(UTC).isoformat(),
                    [{"name": "id", "type": "int64"}, {"name": "vec", "type": "array<float>"}],
                )
            )
            schema_v1 = await repo.dataset_schema(name, version=1)
            schema_v2 = await repo.dataset_schema(name, version=2)
            pruned = await repo.prune_runs("2010-01-01T00:00:00+00:00")
            pruned_again = await repo.prune_runs("2010-01-01T00:00:00+00:00")
            producers = await repo.producers(name)
            return schema_v1, schema_v2, pruned, pruned_again, {p.run_id for p in producers.producers}
        finally:
            await pool.close()

    schema_v1, schema_v2, pruned, pruned_again, run_ids = asyncio.run(run())

    # Per-version schema resolves against real AGE (the int-vs-string $ver quirk is exercised here).
    assert [f.name for f in schema_v1.fields] == ["id"]
    assert [f.name for f in schema_v2.fields] == ["id", "vec"]
    # The old run is pruned (edges included — DETACH), the fresh one survives; a re-run prunes nothing.
    assert pruned >= 1  # >= : a retained volume may carry old runs from prior local e2e invocations
    assert pruned_again == 0 or pruned_again < pruned  # idempotent for THIS cutoff once swept clean
    assert new_rid in run_ids and old_rid not in run_ids


def test_events_feed_and_read_audit_against_postgres(dsn: str) -> None:
    """§7: the durable /events feed + read-audit log against REAL Postgres — the DDL (idempotent on a
    populated DB), the at-least-once dedup (exact redelivery via the 3-col natural key; a redelivered
    TERMINAL with a FRESH eventTime via the partial index — while a RUNNING progress trail keeps every
    distinct time), the newest-first jsonb round-trip, the seq-window retention prune, and record_read.

    The dedup capture happens BEFORE any post-insert ``ensure_events_table`` (§7a: its DDL-time dedup
    DELETEs would silently repair a broken ON CONFLICT and make the assertion vacuous)."""
    import uuid

    # DESTRUCTIVE guard (§7a): the retention sub-test prunes public.lineage_events on WHATEVER DB the
    # DSN names, down to a ONE-ROW keep window.
    #
    # IT USED TO GATE ON THE DSN HOSTNAME, and that guard is worthless — it asks how you REACHED the
    # database, not which database it is. `kubectl port-forward` makes the production AGE Postgres
    # answer on `localhost`, which is exactly how anyone runs this suite against a real cluster. It
    # was not a theoretical hole: on 2026-08-30 this test ran that way, pruned the live estate's feed
    # to one row, and left the notifications reconciler logging `lineage_feed_pruned_below_cursor`
    # every thirty seconds for ten hours before anyone read the logs.
    #
    # So the gate is now an EXPLICIT opt-in. A env var cannot be produced by accident, cannot be
    # forged by a network path, and names the consequence in the variable itself — and the default is
    # to skip, so the suite stays runnable against a real estate with only this one test sitting out.
    from lineage.core.age import make_pool
    from lineage.services.repository import LineageRepository

    if os.environ.get("LINEAGE_E2E_DESTRUCTIVE") != "1":
        pytest.skip(
            "destructive: prunes public.lineage_events to a 1-row window on whatever DB "
            "LINEAGE_DATABASE_URL names. Set LINEAGE_E2E_DESTRUCTIVE=1 to allow it, and only "
            "against a throwaway database — a port-forwarded production DSN looks local."
        )

    rid, rid2 = str(uuid.uuid4()), str(uuid.uuid4())
    reader = f"user:analyst-{uuid.uuid4().hex[:8]}"  # unique per run — the audit log is a plain INSERT

    async def run() -> tuple[list, list, tuple[int, list], list, Readers]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            await repo.ensure_events_table()
            await repo.ensure_events_table()  # idempotent re-boot (DuplicateTable race path is swallowed)

            def kw(run_id: str) -> dict:
                return {
                    "run_id": run_id,
                    "job": "e2e/write.events_feed",
                    "author": "data_eng",
                    "inputs": ["bronze$events"],
                    "outputs": ["silver$features"],
                    "event": {"run": {"runId": run_id}},
                }

            await repo.record_event(event_type="RUNNING", event_time="2026-07-06T00:00:01+00:00", **kw(rid))
            # exact redelivery (same run/type/time — a Dapr re-drive after a lost ack) → deduped
            await repo.record_event(event_type="RUNNING", event_time="2026-07-06T00:00:01+00:00", **kw(rid))
            # a LATER RUNNING (fresh time) is real progress, not a duplicate → kept
            await repo.record_event(event_type="RUNNING", event_time="2026-07-06T00:00:02+00:00", **kw(rid))
            await repo.record_event(event_type="COMPLETE", event_time="2026-07-06T00:00:03+00:00", **kw(rid))
            # a redelivered TERMINAL re-emitted with a FRESH eventTime (retry-after-partial-success) —
            # the 3-col key can't catch this; the partial (run_id, event_type) terminal index must.
            await repo.record_event(event_type="COMPLETE", event_time="2026-07-06T00:00:09+00:00", **kw(rid))
            # Capture BEFORE any further DDL: this list must reflect what ON CONFLICT alone kept.
            records = [r for r in await repo.list_events() if r.event.get("run", {}).get("runId") == rid]
            # The DDL-time dedup DELETEs are valid SQL on a populated table too — and a re-boot must not
            # change what the INSERT-time dedup already settled.
            await repo.ensure_events_table()
            after_ddl = [r for r in await repo.list_events() if r.event.get("run", {}).get("runId") == rid]

            # Retention prunes by ARRIVAL time (`received_at`, the database's own clock), so ageing a row
            # means backdating that column — the producer-supplied event_time above is not what decides.
            #
            # THE WINDOW IS DELIBERATELY ABSURD. This suite points at whatever AGE the DSN names, which on
            # a developer machine is the live estate's, and `prune_events` is estate-wide by nature: a
            # realistic 7-day window would delete real feed rows as a side effect of a test. Backdating
            # this run's own rows past any age a real row can have, and pruning at ten years, exercises
            # the same predicate, batching and rowcount while being unable to touch anything else.
            await repo.record_event(event_type="START", event_time="2026-07-06T00:01:00+00:00", **kw(rid2))
            async with pool.connection() as conn:
                await conn.execute("UPDATE public.lineage_events SET received_at = now() - interval '4000 days' WHERE run_id = %s", (rid,))
            pruned = await repo.prune_events(3650)
            survivors = [r.event.get("run", {}).get("runId") for r in await repo.list_events() if r.event.get("run", {}).get("runId") in (rid, rid2)]

            await repo.ensure_reads_table()
            await repo.record_read(reader=reader, dataset="silver$features")
            await repo.record_read(reader=reader, dataset="silver$features")  # 2nd read → count aggregates
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT reader, dataset FROM public.lineage_reads WHERE reader = %s AND dataset = 'silver$features'",
                    (reader,),
                )
                reads = await cur.fetchall()
            # The QUERY surface (#41 was capture-only): who READ silver$features, aggregated per principal.
            audited = await repo.readers("silver$features")
            return records, after_ddl, (pruned, survivors), reads, audited
        finally:
            await pool.close()

    records, after_ddl, (pruned, survivors), reads, audited = asyncio.run(run())

    # 5 record_event calls → 3 rows AT INSERT TIME: both redeliveries (exact + fresh-time terminal) were
    # dropped by ON CONFLICT, the first COMPLETE won, and the RUNNING trail kept both distinct times.
    # Newest-first by seq.
    assert [(r.event_type, r.event_time) for r in records] == [
        ("COMPLETE", "2026-07-06T00:00:03+00:00"),
        ("RUNNING", "2026-07-06T00:00:02+00:00"),
        ("RUNNING", "2026-07-06T00:00:01+00:00"),
    ]
    assert records[0].inputs == ["bronze$events"]  # jsonb round-trip
    assert records[0].outputs == ["silver$features"]
    # a DDL re-run on the populated table is a no-op on already-settled rows (full model equality —
    # a DDL-time dedup that mangled payload/seq while preserving type+time would still be caught)
    assert after_ddl == records
    # the prune drops what was RECEIVED outside the window and keeps what was not: the backdated run's
    # three rows are gone, the row received just now survives.
    assert pruned == 3, f"expected the three backdated rows to be reclaimed, got {pruned}"
    assert survivors == [rid2], f"the prune took the wrong rows: {survivors}"
    assert reads == [
        (reader, "silver$features"),
        (reader, "silver$features"),
    ]  # both read-audit rows landed (unique reader → re-runnable)
    # the /readers QUERY aggregates the append log per principal: our unique reader, 2 reads, a last_read.
    mine = next((r for r in audited.readers if r.reader == reader), None)
    assert mine is not None and mine.reads == 2 and mine.last_read, audited


def test_terminal_lifecycle_and_column_gc_against_age(dsn: str) -> None:
    """Batch 5 (2026-07-11) — the three new Cypher shapes live on real AGE (unit tests only pin the
    issued query STRINGS; AGE 1.5.0 has a history of quirks only a live run catches).

    ASSERTS, in order: (1) after a successful drop_table event, ``dropped_at()`` returns the drop's
    event time (the read-time derivation over run history executes on AGE, incl. the
    event_type='COMPLETE' filter + ORDER BY/LIMIT); (2) after a later create_table on the same
    name, ``dropped_at()`` is None again (the recreate outranks the drop — no stored flag to
    clear); (3) an overwrite-shaped event whose schema facet replaces {a,b} with {x,y} leaves
    ``dataset_column_graph()`` listing ONLY the new fields (the NOT..IN list-param DELETE of
    HAS_COLUMN links executes on AGE), while a STALE redelivery of the old-schema event afterwards
    changes nothing (the recency gate consults the real WROTE version).
    """
    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.services.repository import LineageRepository

    # UNIQUE PER RUN, and this is the whole reason the test could only pass once. The name and the
    # three run ids were fixed literals, so a second run met its own `lc-3` recreate (09:10) already in
    # the graph — outranking the 09:05 drop this run had just written, making `dropped_at` correctly
    # return None and the assertion fail. The suite was self-poisoning, with no production collision
    # involved at all.
    unique = uuid.uuid4().hex[:8]
    name = f"e2e$lifecycle_gc_{unique}"

    def event(run_id: str, op: str, tm: str, fields: list[str] | None, version: str | None) -> RunEvent:
        facets: dict = {}
        if fields is not None:
            facets["schema"] = {"fields": [{"name": f, "type": "int64"} for f in fields]}
        if version is not None:
            facets["version"] = {"datasetVersion": version}
        return RunEvent.model_validate(
            {
                "eventType": "COMPLETE",
                "eventTime": tm,
                "run": {"runId": run_id, "facets": {"lance": {"operation": op}}},
                "job": {"namespace": "lance-catalog", "name": op},
                "outputs": [{"namespace": "e2e", "name": name, "facets": facets}],
            }
        )

    async def run() -> tuple[str | None, str | None, list[str], list[str]]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            # v1 with schema {a,b} → drop → dropped_at derives the drop
            await repo.ingest_event(event(f"lc-1-{unique}", "create_table", "2026-07-11T09:00:00Z", ["a", "b"], "1"))
            await repo.ingest_event(event(f"lc-2-{unique}", "drop_table", "2026-07-11T09:05:00Z", None, None))
            dropped = await repo.dropped_at(name)
            # recreate at v2 with the REPLACED schema {x,y} → alive again, inventory pruned to {x,y}
            await repo.ingest_event(event(f"lc-3-{unique}", "create_table", "2026-07-11T09:10:00Z", ["x", "y"], "2"))
            alive = await repo.dropped_at(name)
            inventory = [c.field for c in (await repo.dataset_column_graph(name)).columns]
            # STALE redelivery of the v1 old-schema event — the recency gate must not resurrect {a,b}
            await repo.ingest_event(event(f"lc-1-{unique}", "create_table", "2026-07-11T09:00:00Z", ["a", "b"], "1"))
            after_stale = [c.field for c in (await repo.dataset_column_graph(name)).columns]
            return dropped, alive, inventory, after_stale
        finally:
            await pool.close()

    dropped, alive, inventory, after_stale = asyncio.run(run())
    assert dropped == "2026-07-11T09:05:00Z"  # (1) the drop is derived from real run history
    assert alive is None  # (2) the recreate outranks it — nothing stored, nothing to clear
    assert inventory == ["x", "y"]  # (3) the replaced columns are GONE from the CURRENT inventory
    assert after_stale == ["x", "y"]  # …and a stale redelivery cannot bring them back


def test_a_run_state_is_decided_by_event_time_not_by_delivery_order(dsn: str) -> None:
    """A run's state must not be whichever event happened to be delivered last.

    `open_lakehouse_diff_left.md` § E3 ("run state regresses on out-of-order ingest"), whose close
    condition is *"Sticky terminal state; a START-after-COMPLETE test."*

    MEASURED ON THE DEPLOYED GRAPH 2026-09-07, and it is not the hypothetical the row describes — the
    regression has already landed. Run `f280fd32-617d-5292-9323-993d021bb79e`, the medallion cascade's
    `derive_media` writing `silver-media$features`, holds two terminal events in the durable feed::

        seq 118723   FAIL       2026-09-07T13:30:31.410352+00:00
        seq 118724   COMPLETE   2026-09-07T13:29:50.154780+00:00

    Delivered FAIL first, COMPLETE second — but the COMPLETE is stamped 41 seconds EARLIER. `MERGE_RUN`
    assigned `r.event_type=$et` unconditionally, so the graph reports that run COMPLETE while the newest
    event says it failed. Everything downstream reads the wrong answer from it: `_fold_writes` badges a
    dataset failed only from a producing run's FAIL/ABORT, and `LATEST_WRITE` filters
    `WHERE r.event_type = 'COMPLETE'`. It is also visible from the node alone — `event_time` (last
    delivery) sits 41 s BEFORE `started_at` (first delivery), which is a run that finished before it began.

    THE ESTATE ALREADY MADE THIS RULING ONE LAYER OVER. The column inventory has a recency gate, and the
    test above ends by proving a stale redelivery cannot resurrect a replaced schema. The run's own
    lifecycle had no such gate.

    TWO RULES, because neither covers the other. Newest-event-wins settles the measured case (both
    events terminal, so only time separates them). Terminal-stickiness settles the row's named case, a
    START arriving after a COMPLETE with a *fresher* stamp, which newest-event-wins would let through.

    DRIVEN THROUGH `ingest_event` AGAINST REAL AGE rather than asserted over the Cypher string: the
    guard is a `WITH`-bound predicate consumed by a `SET`, and AGE 1.5.0 has quirks in exactly that
    seam — it drops a `$param` in a `SET` fused to a MERGE-on-edge, which is why four statements in
    `cypher.py` are split out. A string assertion would pass on a guard AGE silently ignores.
    """
    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.services.repository import LineageRepository

    run_id = str(uuid.uuid4())
    name = f"e2e-order-{uuid.uuid4().hex[:8]}"

    def event(event_type: str, tm: str) -> RunEvent:
        return RunEvent.model_validate(
            {
                "eventType": event_type,
                "eventTime": tm,
                "run": {"runId": run_id, "facets": {}},
                "job": {"namespace": "medallion", "name": "derive_media"},
                "outputs": [{"namespace": "e2e", "name": name}],
            }
        )

    async def run() -> list[tuple[str | None, str | None, str | None]]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")
            seen: list[tuple[str | None, str | None, str | None]] = []

            async def state() -> tuple[str | None, str | None, str | None]:
                status = await repo.run_status(run_id)
                assert status is not None, f"run {run_id} was not stored at all"
                return status.state, status.updated_at, status.started_at

            # The live shape: a run that FAILED at :31, delivered before the COMPLETE stamped at :50.
            await repo.ingest_event(event("FAIL", "2026-09-07T13:30:31.410352+00:00"))
            seen.append(await state())
            await repo.ingest_event(event("COMPLETE", "2026-09-07T13:29:50.154780+00:00"))
            seen.append(await state())
            # The row's own case: a START arriving after the terminal, with a FRESHER stamp.
            await repo.ingest_event(event("START", "2026-09-07T14:00:00.000000+00:00"))
            seen.append(await state())
            # …and the direction that must still work: a genuinely newer terminal supersedes.
            await repo.ingest_event(event("ABORT", "2026-09-07T14:05:00.000000+00:00"))
            seen.append(await state())
            return seen
        finally:
            await pool.close()

    after_fail, after_stale_complete, after_late_start, after_newer_abort = asyncio.run(run())

    assert after_fail[0] == "FAIL", f"the first event did not land: {after_fail}"
    assert after_stale_complete[0] == "FAIL", (
        f"an OLDER COMPLETE overwrote a FAIL because it was delivered second — the graph now reports a "
        f"failed run as succeeded, which is the live defect: {after_stale_complete}"
    )
    assert after_late_start[0] == "FAIL", f"a START regressed a terminal run: {after_late_start}"
    assert after_newer_abort[0] == "ABORT", f"a genuinely newer terminal was refused: {after_newer_abort}"

    # event_time is `updated_at` and the runs board ORDERs BY it, so it must never move backwards…
    assert after_stale_complete[1] == "2026-09-07T13:30:31.410352+00:00", f"event_time regressed: {after_stale_complete}"
    # …and started_at must be the EARLIEST time seen, not the first DELIVERED one, or a run reads as
    # having finished before it began — exactly what the live node shows.
    assert after_stale_complete[2] == "2026-09-07T13:29:50.154780+00:00", (
        f"started_at kept the first-delivered time, so the run still finished before it began: {after_stale_complete}"
    )


def test_a_failed_feed_write_takes_the_graph_write_with_it(dsn: str) -> None:
    """`GET /events` must not be able to become a subset of the graph.

    `open_lakehouse_diff_left.md` § E4. The feed row used to be written by `record_event_best_effort` —
    a SECOND connection, opened after `ingest_event`'s transaction had already committed, catching every
    exception into a WARNING on the stated ground that *"a feed-write failure must never break ingest
    (the authoritative AGE graph write already succeeded)"*. Under that contract a connection error, an
    eviction or a statement timeout between the two writes left an event permanently in the graph and
    permanently absent from the feed. Nothing could detect it afterwards: the two stores have no shared
    key to reconcile on, and the only trace was a log line `kubectl logs` strips the `extra=` fields from.

    THE FEED IS NOT A DIAGNOSTIC. `services/notifications` walks it from a persisted cursor as the
    estate's catch-up path after an outage, because the bus alone provably misses ingest, Ray TRAIN and
    every external OpenLineage producer. A row missing from it is a person who is never told.

    THE TEST FORCES THE FAILURE AT THE WRITER, which is the only way to observe the boundary: the feed
    INSERT is `ON CONFLICT DO NOTHING` and takes no constraint a caller can violate, so no input makes it
    fail. A subclass whose `_insert_feed_row` raises puts the failure exactly where a dead connection or
    a cancelled statement would, and leaves every other statement genuine.

    BOTH ASSERTIONS ARE LOAD-BEARING and neither alone is the property. `raised` is what the old shape
    lost — the failure was caught and ingest reported success. `rolled_back` is what a feed write outside
    the transaction loses. Measured 2026-09-08: against an `ingest_event` with no feed write at all (the
    pre-fix body), `raised` is False; the fix makes both true.
    """
    import psycopg

    from lineage.core.age import make_pool
    from lineage.models import RunEvent
    from lineage.services.repository import LineageRepository

    class _FeedWriteFails(LineageRepository):
        """Everything the real repository does, except the feed row, which fails the way a dropped
        connection would — inside whatever transaction the caller has open."""

        async def _insert_feed_row(self, conn: psycopg.AsyncConnection, **columns: object) -> None:
            raise RuntimeError("feed write failed")

    run_id = str(uuid.uuid4())
    name = f"e2e-atomic-{uuid.uuid4().hex[:8]}"
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-08T10:00:00+00:00",
            "run": {"runId": run_id, "facets": {}},
            "job": {"namespace": "e2e", "name": "atomicity"},
            "outputs": [{"namespace": "e2e", "name": name}],
        }
    )

    async def run() -> tuple[bool, bool, bool]:
        pool = make_pool(dsn)
        await pool.open()
        try:
            repo = LineageRepository(pool, "lineage")

            async def stored() -> bool:
                return await repo.run_status(run_id) is not None

            raised = False
            try:
                await _FeedWriteFails(pool, "lineage").ingest_event(event)
            except RuntimeError:
                raised = True
            rolled_back = not await stored()
            # …and the same event through the real repository must still land — this guard is worthless
            # if all it proves is that a raising method raises.
            await repo.ingest_event(event)
            return raised, rolled_back, await stored()
        finally:
            await pool.close()

    raised, rolled_back, landed = asyncio.run(run())

    assert raised, "a feed-write failure was swallowed — ingest still reported success"
    assert rolled_back, (
        "the run reached the AGE graph while its /events row did not: the two writes are in separate "
        "transactions, so the feed is a subset of the graph by construction and nothing can detect it"
    )
    assert landed, "the unbroken ingest did not store the run — the test proves nothing"
