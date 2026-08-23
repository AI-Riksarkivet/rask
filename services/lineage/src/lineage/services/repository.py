"""Data-access layer for the lineage graph — all openCypher lives here.

The repository owns the two halves of the AGE graph: the **write** path (ingest an
OpenLineage run event → MERGE Run/Job/Dataset nodes + edges) and the **read** path
(traverse provenance / impact / producers). Endpoints depend on this class and never
see raw Cypher — per the layered architecture (handlers → repository → AGE).

Graph shape::

    (:Job {namespace, name})
    (:Run {run_id, author, event_type, event_time, producer, error_message})
    (:Dataset {name, namespace, source_uri, tags})  # name = catalog table id
    (:User {name})                            # an OIDC sub (the verified principal)
    (:Run)-[:OF_JOB]->(:Job)
    (:Run)-[:READ]->(:Dataset)                # inputs
    (:Run)-[:WROTE {version, schema}]->(:Dataset)  # outputs (version = Lance version; schema per version)
    (:Dataset)-[:DERIVED_FROM]->(:Dataset)    # output <- input (dataset lineage)
    (:User)-[:CREATED]->(:Dataset)            # who created the table (catalog create event)
    (:Column {dataset, field, namespace, type})    # a column; dataset = owning :Dataset.name (#24)
    (:Dataset)-[:HAS_COLUMN]->(:Column)            # the dataset's columns (complete typed inventory)
    (:Column)-[:DERIVED_FROM_COLUMN {...}]->(:Column)  # output_col <- input_col (field-to-field lineage)

A **successful** run (``COMPLETE``) asserts data: it gets a versioned ``WROTE`` edge plus
``DERIVED_FROM`` (and ``CREATED`` on a catalog create). A **failed** run (``FAIL``/``ABORT``)
is still recorded — its ``Run`` carries the ``error_message`` and it keeps a ``WROTE`` edge so
``producers()`` surfaces the attempt — but with **no version** and **no ``DERIVED_FROM``**: a
failed run produced no data, so it must not assert lineage.

Datasets are MERGEd on ``{name}`` only (then ``namespace`` / ``source_uri`` / ``tags`` are SET)
so a dataset referenced by several runs is never duplicated.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any, Final

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from lineage.core.age import fetch, run_cypher
from lineage.models import Dataset, RunEvent, vertex_name_for
from lineage.schemas import (
    ColumnEdge,
    ColumnGraph,
    ColumnNeighbors,
    ColumnNode,
    ColumnRef,
    Creator,
    DatasetGovernance,
    DatasetRef,
    DatasetSchema,
    DatasetSummary,
    EstateGraph,
    EventRecord,
    GraphEdge,
    GraphNode,
    JobSummary,
    LineageGraph,
    Neighbors,
    ProducerInfo,
    Producers,
    ReaderInfo,
    Readers,
    RunInput,
    RunInputs,
    Runs,
    RunStatus,
    SchemaField,
)
from service_kit.lakehouse.schema import SchemaFields
from service_kit.openlineage import RUN_EVENT_SCHEMA_URL, custom_facet, run_id_for


log = logging.getLogger(__name__)

# Ops that bring a table into existence in the catalog → each keys a (:User)-[:CREATED]->(:Dataset) edge.
# Must match the catalog.core.lineage_emit markers (wire contract): a plain create, a register of an existing
# location, and a declare of an empty id are all "someone originated this table" events; every other op
# (insert/update/drop/index/…) is a WROTE, not a CREATED.
_CREATE_OPS: Final = frozenset({"create_table", "register_table", "declare_table"})

_MERGE_JOB: Final = "MERGE (j:Job {namespace:$ns, name:$nm}) RETURN 1"
# Where the job's code lives (the standard sourceCodeLocation facet), as a JSON string scalar on the Job
# node — SET only when the event carries it, so an event that omits it never clobbers a prior value.
_SET_JOB_SOURCE: Final = "MATCH (j:Job {namespace:$ns, name:$nm}) SET j.source_location=$src RETURN 1"
# The (:Run) node folds the whole lifecycle so /runs is durable (survives restart, replica-shared)
# instead of folding an in-memory buffer: event_type IS the current state and event_time IS
# updated_at (both last-event-wins via the repeated SET); started_at keeps the first event's time;
# events_count counts lifecycle events RECEIVED (incl. redeliveries — it is a delivery counter, not a
# distinct-event count; the graph nodes/edges are idempotent, the feed dedups). job is denormalised so
# /runs needs no OF_JOB join.
_MERGE_RUN: Final = (
    "MERGE (r:Run {run_id:$rid}) "
    "SET r.event_type=$et, r.event_time=$tm, r.author=$au, r.producer=$pr, r.error_message=$err, "
    # operation is STICKY (like started_at): a later event of the same run that carries no lance facet
    # ($op='') must not erase the operation an earlier event declared (START stamps it, terminal may not).
    "r.job=$job, r.operation=(CASE WHEN $op = '' THEN r.operation ELSE $op END), "
    # source_run_id is STICKY for the same reason as operation: the producer stamps its own run id
    # on every event it emits, but a reconcile/backfill event for the same graph run carries none
    # and must not erase it. Empty string means the event did not say; only a non-empty value writes.
    "r.source_run_id=(CASE WHEN $srid = '' THEN r.source_run_id ELSE $srid END), "
    "r.started_at=coalesce(r.started_at, $tm), r.events_count=coalesce(r.events_count, 0)+1 "
    "RETURN 1"
)
# Progress + outputs ride only some events (RUNNING carries progress; only the terminal event names
# the outputs), so they are SET in their own conditional statements — never clobbered back to null.
_SET_RUN_PROGRESS: Final = "MATCH (r:Run {run_id:$rid}) SET r.progress_done=$pd, r.progress_total=$pt RETURN 1"
_SET_RUN_OUTPUTS: Final = "MATCH (r:Run {run_id:$rid}) SET r.outputs=$outs RETURN 1"
# What a run has ALREADY recorded writing — the object `enforce_output_authz` authorizes a MUTATION
# of that run against. Empty (or no row) means the run does not exist yet, which is what keeps a
# START event able to open a run it cannot authorize.
_RUN_OUTPUT_NAMES: Final = "MATCH (r:Run {run_id:$rid}) RETURN r.outputs"
_LIST_RUNS: Final = (
    "MATCH (r:Run) RETURN r.run_id, r.job, r.author, r.event_type, r.progress_done, r.progress_total, "
    "r.error_message, r.started_at, r.event_time, r.events_count, r.outputs, r.operation, r.source_run_id"
)
# Discovery / browse — the "what exists?" lists. Like _LIST_RUNS these fetch every node and are governed in
# Python, so a caller can browse the estate without already knowing an exact name.
#
# PAGINATION IS NOT UNIFORM, and this comment used to claim it was. Only `/datasets` takes offset/limit
# (`discovery.list_datasets`, capped at _MAX_LIMIT); `/runs`, `/jobs` and `/namespaces` take neither and
# return every row the FGA filter leaves. That is currently fine — the graph's node count is modest, and
# `/runs` measured 272 rows on the live estate 2026-08-23 — but it is a property of the data, not of the
# code, and nothing bounds it if the estate grows. Adding a bound to the other three is a wire-contract
# change and a decision; saying which of them have one is not. Tags ride the Dataset node as a comma-joined string (_tags_from splits them back).
_LIST_DATASETS: Final = "MATCH (d:Dataset) RETURN d.name, d.namespace, d.tags"
# The full linked column inventory for /search (P1 Search tier 1, 2026-07-11) — HAS_COLUMN-scoped so
# only CURRENT inventory matches (pruned/overwritten columns don't resurrect via search).
_LIST_ALL_COLUMNS: Final = "MATCH (:Dataset)-[:HAS_COLUMN]->(c:Column) RETURN c.dataset, c.field"
# One row per (job, written-dataset); d.name is null for a job that has only read (OPTIONAL MATCH keeps the
# job row). Folded into per-job output sets in Python — avoids parsing an agtype array from collect().
_LIST_JOBS: Final = "MATCH (j:Job) OPTIONAL MATCH (j)<-[:OF_JOB]-(:Run)-[:WROTE]->(d:Dataset) RETURN j.namespace, j.name, d.name"

# Durable events feed — a plain table in the SAME Postgres that hosts AGE (qualified ``public.`` so it
# never lands in AGE's ``ag_catalog`` schema on the search path). Replaces the in-memory deque so /events
# survives restart + is replica-shared, mirroring the durable /runs fold. (#22)
_CREATE_EVENTS_TABLE: Final = (
    "CREATE TABLE IF NOT EXISTS public.lineage_events ("
    "seq bigserial PRIMARY KEY, run_id text, event_type text, event_time text, "
    "job text, author text, inputs jsonb, outputs jsonb, event jsonb)"
)
# A pre-existing table (created before this index) may already hold redelivered duplicates that would make
# CREATE UNIQUE INDEX fail — remove them first, keeping the earliest row (min seq) per natural key, so the
# index can always be established. NULL event_type/event_time never match (SQL NULL ≠ NULL), matching the
# unique index's NULLs-are-distinct semantics. Idempotent (a no-op once deduped).
_DEDUP_EVENTS: Final = (
    "DELETE FROM public.lineage_events a USING public.lineage_events b "
    "WHERE a.seq > b.seq AND a.run_id = b.run_id "
    "AND a.event_type = b.event_type AND a.event_time = b.event_time"
)
# A natural key over the OpenLineage lifecycle identity — so an at-least-once REDELIVERY of the same event
# (Dapr re-drives after a lost ack) doesn't append a duplicate /events row. Idempotent on existing tables.
_CREATE_EVENTS_INDEX: Final = "CREATE UNIQUE INDEX IF NOT EXISTS lineage_events_natural_key ON public.lineage_events (run_id, event_type, event_time)"
# The 3-col key alone can't dedup a REDELIVERED TERMINAL event: a RETRY-after-partial-success re-emits the
# same run's COMPLETE/FAIL with a FRESH eventTime, so the triple differs and a duplicate row lands. A run
# has at most ONE terminal state, so a partial unique on (run_id, event_type) for terminal types dedups
# them REGARDLESS of eventTime — while RUNNING events keep only the 3-col key, so their progress trail
# (many RUNNINGs at different times) is preserved. NULL event_type is excluded by the WHERE (NULL IN → not
# true), so it falls back to the 3-col key. The INSERT uses a TARGETLESS ON CONFLICT so it fires on EITHER.
_TERMINAL_TYPES: Final = "('COMPLETE','FAIL','ABORT','RECONCILED')"
_DEDUP_TERMINAL: Final = (
    "DELETE FROM public.lineage_events a USING public.lineage_events b "
    "WHERE a.seq > b.seq AND a.run_id = b.run_id AND a.event_type = b.event_type "
    # noqa-justified: _TERMINAL_TYPES is a module-level Final literal, not user input — no external
    # data reaches this f-string, so S608's injection premise does not apply.
    f"AND a.event_type IN {_TERMINAL_TYPES}"  # noqa: S608
)
_CREATE_TERMINAL_INDEX: Final = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS lineage_events_terminal_key ON public.lineage_events (run_id, event_type) WHERE event_type IN {_TERMINAL_TYPES}"  # noqa: S608
)
# DECIDED (2026-07-10, §7a design item): the feed KEEPS THE FIRST terminal row per (run_id, event_type) —
# a re-executed run (same deterministic run_id, e.g. RETRY-after-trigger-failure re-emitting COMPLETE with
# a new version) updates the GRAPH views last-wins (/runs, /producers, the WROTE edge) but never rewrites
# its /events row. That asymmetry is the contract: /events is the append-only observation log ("what
# arrived first"), the graph is current state — upsert-latest here would let a redelivery silently rewrite
# audit history. Pinned by the feed e2e (test_events_feed_and_read_audit_against_postgres).
_INSERT_EVENT: Final = (
    "INSERT INTO public.lineage_events "
    "(run_id, event_type, event_time, job, author, inputs, outputs, event) "
    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) "
    "ON CONFLICT DO NOTHING"
)
_LIST_EVENTS: Final = "SELECT seq, event_type, event_time, job, author, inputs, outputs, event FROM public.lineage_events ORDER BY seq DESC LIMIT %s"
# Keyset variant (§2 perf, 2026-07-11): `seq < %s` walks older pages off the PK index — NEVER OFFSET
# (OFFSET re-scans and re-drops every skipped row, so page N costs O(N·page)). The summary variants
# skip the full-JSONB `event` column entirely — the feed's hot poll path doesn't pay for payloads it
# won't render.
_LIST_EVENTS_AFTER: Final = (
    "SELECT seq, event_type, event_time, job, author, inputs, outputs, event FROM public.lineage_events WHERE seq < %s ORDER BY seq DESC LIMIT %s"
)
_LIST_EVENTS_SUMMARY: Final = "SELECT seq, event_type, event_time, job, author, inputs, outputs FROM public.lineage_events ORDER BY seq DESC LIMIT %s"
_LIST_EVENTS_SUMMARY_AFTER: Final = (
    "SELECT seq, event_type, event_time, job, author, inputs, outputs FROM public.lineage_events WHERE seq < %s ORDER BY seq DESC LIMIT %s"
)
# Retention prune — keep the most-recent N rows (by the monotonic seq), drop older. Cheap (PK-indexed seq).
_PRUNE_EVENTS: Final = "DELETE FROM public.lineage_events WHERE seq <= (SELECT COALESCE(MAX(seq), 0) FROM public.lineage_events) - %s"
# The FLOOR the prune above leaves behind — the oldest row the feed can still serve.
#
# It exists because the prune runs on EVERY ingest and knows about no consumer: a reader whose cursor
# has fallen below this number lost rows before it read them, and walking to the end of the feed would
# otherwise look exactly like being caught up. Lineage cannot detect that itself — the notifications
# reconciler's cursor lives in ITS Dapr state store, which lineage is not scoped to and must not be —
# so lineage publishes the floor and each consumer draws its own conclusion.
#
# MIN(seq) rides the primary-key index, so this is a cheap read even on a full retention window.
_OLDEST_EVENT_SEQ: Final = "SELECT MIN(seq) FROM public.lineage_events"
# Read/access audit (#6) — a plain append log of WHO read WHICH dataset (public, like lineage_events; the
# write provenance lives in the AGE graph, this is the complementary read log).
_CREATE_READS_TABLE: Final = (
    "CREATE TABLE IF NOT EXISTS public.lineage_reads ("
    "seq bigserial PRIMARY KEY, reader text NOT NULL, dataset text NOT NULL, "
    "read_at timestamptz NOT NULL DEFAULT now())"
)
_INSERT_READ: Final = "INSERT INTO public.lineage_reads (reader, dataset) VALUES (%s, %s)"
# The read-audit QUERY (the #41 log was capture-only): who read a dataset, aggregated per principal with
# their last-read time + count, most-recent first. GROUP BY collapses the append log's repeat rows.
_READERS: Final = (
    "SELECT reader, MAX(read_at) AS last_read, COUNT(*) AS reads FROM public.lineage_reads WHERE dataset = %s GROUP BY reader ORDER BY last_read DESC LIMIT %s"
)
# Does the AGE graph exist? ag_catalog.ag_graph is AGE's registry of graphs. Used to make create_graph
# idempotent + concurrency-safe (create_graph ERRORS if the graph already exists).
_GRAPH_EXISTS: Final = "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s"

# Reconcile single-flight (B4 hardening) — a fixed session-level advisory-lock id. The cron reconcile fires
# on EVERY lineage replica's sidecar independently, and a sweep back-fills the graph; two overlapping sweeps
# would double-drive the same back-fill. pg_try_advisory_lock on this id serializes them CLUSTER-wide
# (Postgres is the one shared coordinator) without pinning replicas=1. Arbitrary stable bigint constant.
_RECONCILE_LOCK_KEY: Final = 0x1A9CE_5EED

# Vertex-uniqueness (B4 hardening) — each AGE vertex label + the property key(s) its MERGE keys on. A UNIQUE
# index over those keys makes AGE's MATCH-then-CREATE MERGE safe under CONCURRENCY: two txns (a reconcile
# racing a live ingest, or two sweeps) that both miss and both CREATE would otherwise leave a DUPLICATE
# vertex — the index makes the loser's insert fail instead. Keys mirror the _MERGE_* Cypher above.
_VERTEX_UNIQUE_KEYS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("Run", ("run_id",)),
    ("Dataset", ("name",)),
    ("Job", ("namespace", "name")),
)

# Plain (NON-unique) lookup indexes — labels whose MERGE key needs index-speed MATCHes but must NOT get a
# uniqueness constraint. :Column keeps its deliberate no-unique-index design (duplicate vertices from a
# rare concurrent first-create are benign and collapsed by DISTINCT reads; a unique index would add
# abort/retry churn + a lock-ordering obligation to the hot column path — see the _DATASET_COLUMN_NODES
# comment). Without ANY index though, every column MERGE seq-scans a label table that grows with the
# estate (§4) — this closes the perf half while preserving the concurrency semantics.
_VERTEX_LOOKUP_KEYS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (("Column", ("dataset", "field")),)

_LINK_RUN_JOB: Final = "MATCH (r:Run {run_id:$rid}), (j:Job {namespace:$ns, name:$nm}) MERGE (r)-[:OF_JOB]->(j) RETURN 1"
_MERGE_DATASET: Final = "MERGE (d:Dataset {name:$name}) SET d.namespace=$ns RETURN 1"
# Storage location is SET only when the event carries it; tags are UNIONed into the node's set (#49 —
# the property also holds human-curated governance tags, which a producer's facet must never clobber).
_SET_DATASET_SRC: Final = "MATCH (d:Dataset {name:$name}) SET d.source_uri=$src RETURN 1"
# Terminal lifecycle (2026-07-11): dropped-ness is DERIVED AT READ TIME from run history — the most
# recent SUCCESSFUL run that wrote the dataset being a drop_table means "deliberately dropped", so
# the reconcile sweep skips it (absence on storage is the EXPECTED state, not storage loss — it
# previously WARNed missing_on_storage forever via the stale source_uri). Derivation instead of a
# mutable stamp is deliberate (review 2026-07-11): a stamped flag was last-DELIVERY-wins — a stale
# redelivered drop event after a recreate would re-stamp a LIVE dataset and silently remove it from
# the sweep. Run nodes MERGE idempotently on run_id, so ordering by their event_time at read time is
# redelivery-proof by construction. FAILed runs keep WROTE edges (producers() shows the attempt), so
# the event_type=COMPLETE filter is load-bearing: a failed drop asserts nothing.
_DATASET_LAST_SUCCESS_OP: Final = (
    "MATCH (r:Run)-[:WROTE]->(d:Dataset {name:$name}) WHERE r.event_type = 'COMPLETE' RETURN r.operation, r.event_time ORDER BY r.event_time DESC LIMIT 1"
)
_SET_DATASET_TAGS: Final = "MATCH (d:Dataset {name:$name}) SET d.tags=$tags RETURN 1"
# Governance metadata (#49) — human-curated tags + description on the Dataset node, with last-writer
# attribution per field family. Standalone MATCH…SET statements bind params fine on AGE 1.5.0 (only a
# post-MERGE SET drops them); tags stay the same comma-joined string the ingest path writes.
_GET_DATASET_GOVERNANCE: Final = (
    "MATCH (d:Dataset {name:$name}) RETURN d.tags, d.description, d.tags_updated_by, d.tags_updated_at, d.description_updated_by, d.description_updated_at"
)
_SET_GOVERNED_TAGS: Final = "MATCH (d:Dataset {name:$name}) SET d.tags=$tags, d.tags_updated_by=$by, d.tags_updated_at=$at RETURN 1"
_SET_DESCRIPTION: Final = "MATCH (d:Dataset {name:$name}) SET d.description=$desc, d.description_updated_by=$by, d.description_updated_at=$at RETURN 1"
_LINK_READ: Final = "MATCH (r:Run {run_id:$rid}), (d:Dataset {name:$name}) MERGE (r)-[:READ]->(d) RETURN 1"
# The READ edge carries the Lance version this run CONSUMED, when the producer pinned it (the Ray TRAIN
# job pins every feature — #115 D1). Same own-statement rule as _SET_WROTE_VERSION below (AGE drops a
# $param in a SET that follows an edge MERGE in the same statement). Unpinned reads leave it absent.
_SET_READ_VERSION: Final = "MATCH (r:Run {run_id:$rid})-[e:READ]->(d:Dataset {name:$name}) SET e.version=$ver RETURN 1"
# The WROTE edge carries the Lance dataset version this run produced (from the OpenLineage
# ``version`` facet), so two refinement passes over one table are distinguishable in producers().
_LINK_WROTE: Final = "MATCH (r:Run {run_id:$rid}), (d:Dataset {name:$name}) MERGE (r)-[:WROTE]->(d) RETURN 1"
# AGE binds a ``$param`` in a standalone ``MATCH ... SET`` but silently drops one in a ``SET`` that
# follows ``MERGE`` on an edge in the *same* statement (verified on AGE 1.5.0/PG16), so the version
# is written in its own statement — mirroring how dataSource/tags are set on the Dataset node.
_SET_WROTE_VERSION: Final = "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.version=$ver RETURN 1"
# OpenLineage ``producer`` URI for the back-fill's synthetic event — spec-required, and what a Marquez-style
# consumer records as the event source (here: the lineage service repairing its own graph, not a producer).
_RECONCILE_PRODUCER: Final = "https://github.com/Borg93/lance-ns/tree/main/services/lineage"
# Storage->graph reconciliation back-fill (B4) — a synthetic 'reconcile' run recording a Lance write whose
# lineage event was lost (the outbox gap). Idempotent (MERGE on the reconcile run id), so re-running the
# reconcile never duplicates; the WROTE version is stamped in its own statement (the AGE MERGE+SET quirk).
_BACKFILL_RUN: Final = (
    "MERGE (r:Run {run_id:$rid}) SET r.event_type='RECONCILED', r.author='reconcile', r.event_time=$tm, "
    "r.job=$job, r.outputs=$outs, "
    "r.started_at=coalesce(r.started_at, $tm), r.events_count=coalesce(r.events_count, 0)+1 RETURN 1"
)
# Run retention (§4) — Run nodes (and their READ/WROTE/OF_JOB edges) otherwise grow forever. Opt-in
# (LINEAGE_RUN_RETENTION_DAYS, 0 = off); the reconcile cron prunes under its cluster-wide advisory lock.
# ISO-8601 UTC timestamps compare lexicographically, so the string comparison IS a time comparison here
# (every in-repo producer stamps ``datetime.now(UTC).isoformat()``). Pruning deletes the run's WROTE
# edges — that is what retention means (per-version schema/stats history goes with it); a dataset whose
# only runs were pruned reads latest_write_version=None and the next sweep back-fills a fresh reconcile
# run at the on-disk version, so the graph converges instead of dangling.
_COUNT_OLD_RUNS: Final = "MATCH (r:Run) WHERE r.event_time < $cutoff RETURN count(r)"
# BATCHED (LIMIT 500, one transaction per batch): a single all-or-nothing DETACH DELETE over a large
# backlog would exceed the pool's statement_timeout → QueryCanceled → full rollback → retention never
# converges (each tick retries the identical oversized delete). Batches keep every statement far under
# the timeout and make partial progress durable tick over tick.
_PRUNE_OLD_RUNS_BATCH: Final = "MATCH (r:Run) WHERE r.event_time < $cutoff WITH r LIMIT 500 DETACH DELETE r"
_PRUNE_BATCH_SIZE: Final = 500
# The per-version column schema rides the same WROTE edge as the version (#24 prerequisite). Stored as
# a JSON **string** scalar — params are JSON-encoded and ``_parse`` json.loads each cell, so a scalar
# round-trips cleanly; an array-in-SET is the risky path AGE 1.5.0 mishandles (same reason tags are a
# comma-joined string). Own statement, like the version (AGE drops a $param in a post-MERGE SET).
_SET_WROTE_SCHEMA: Final = "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.schema=$schema RETURN 1"
# Runtime-measured output statistics ride the same WROTE edge (the rows + on-disk bytes the compute
# actually wrote, from the standard ``outputStatistics`` facet). Both are plain int scalars set in a
# standalone MATCH...SET (no MERGE-on-edge in this statement → AGE binds both $params, like _SET_COL_EDGE).
_SET_WROTE_STATS: Final = "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.row_count=$rows, w.size_bytes=$size RETURN 1"
# Quality-gate result rides the same WROTE edge: a ``quality_passed`` bool (the headline signal) + the
# full assertions as a JSON **string** scalar (same scalar-round-trips-cleanly reasoning as the schema).
# A passed=false edge with a real version is the auditable record of a batch the gate blocked.
_SET_WROTE_QUALITY: Final = (
    "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.quality_passed=$passed, w.quality_assertions=$assertions RETURN 1"
)
_DERIVED_FROM: Final = "MATCH (o:Dataset {name:$on}), (i:Dataset {name:$inp}) MERGE (o)-[:DERIVED_FROM]->(i) RETURN 1"

_UPSTREAM: Final = "MATCH (d:Dataset {name:$name})-[:DERIVED_FROM*1..]->(u:Dataset) RETURN DISTINCT u.name, u.namespace"
# One run's direct inputs + the version it PINNED on each (the READ-edge version — #115's reproducibility
# pin). Direct edges only (NOT the transitive DERIVED_FROM closure): "which versions did THIS run read"
# is a property of the run's own reads, not of the dataset ancestry where a version has no meaning.
_RUN_INPUTS: Final = "MATCH (r:Run {run_id:$rid})-[e:READ]->(d:Dataset) RETURN DISTINCT d.name, e.version"
_DOWNSTREAM: Final = "MATCH (d:Dataset {name:$name})<-[:DERIVED_FROM*1..]-(x:Dataset) RETURN DISTINCT x.name, x.namespace"
_PRODUCERS: Final = (
    "MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:$name}) "
    "RETURN r.run_id, r.author, r.event_time, r.event_type, w.version, r.producer, r.error_message, "
    "w.row_count, w.size_bytes, w.quality_passed, w.quality_assertions, r.operation "
    # NEWEST FIRST: AGE returns rows in physical order otherwise, so a consumer taking "the latest run"
    # (e.g. the #82 quality-gate badge) could read a STALE earlier verdict — an older `passed` masking the
    # current `blocked`. Sort here so every consumer sees the current run first. (audit 2026-07-20)
    "ORDER BY r.event_time DESC"
)
# Reconcile (#23): the version the graph believes is current = the version on the most-recent
# *successful* WROTE edge (failed runs carry a WROTE edge with no version, so the IS NOT NULL guard
# skips them). Most-recent by run event_time, since Lance versions are monotonic per dataset.
_LATEST_WRITE_VERSION: Final = (
    "MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:$name}) WHERE w.version IS NOT NULL RETURN w.version ORDER BY r.event_time DESC LIMIT 1"
)
_SOURCE_URI: Final = "MATCH (d:Dataset {name:$name}) RETURN d.source_uri LIMIT 1"
# Per-version schema lookup (#24). Latest = the most-recent successful WROTE edge that carries a schema;
# at-version pins the edge whose version matches. Both return the schema JSON string + its version.
_SCHEMA_LATEST: Final = (
    "MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:$name}) WHERE w.schema IS NOT NULL RETURN w.schema, w.version ORDER BY r.event_time DESC LIMIT 1"
)
_SCHEMA_AT_VERSION: Final = (
    "MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:$name}) WHERE w.version=$ver AND w.schema IS NOT NULL "
    "RETURN w.schema, w.version ORDER BY r.event_time DESC LIMIT 1"
)
_MERGE_USER: Final = "MERGE (u:User {name:$name}) RETURN 1"
# Latest-create-wins: the CREATED edge carries the create event_time so creator() is deterministic
# even when a table name is dropped+recreated by a different principal (the most recent create is
# authoritative). A re-create updates this principal; drop-lineage GC is future work.
_LINK_CREATED: Final = "MATCH (u:User {name:$name}), (d:Dataset {name:$ds}) MERGE (u)-[c:CREATED]->(d) SET c.created_at=$tm RETURN 1"
_CREATOR: Final = "MATCH (u:User)-[c:CREATED]->(d:Dataset {name:$name}) RETURN u.name ORDER BY c.created_at DESC LIMIT 1"

# AGE rejects zero-length variable paths (``*0..``), so the connected node set is
# assembled from the upstream + downstream traversals (``*1..``) plus the root itself,
# nodes are fetched in one shot (name set), and edges are filtered to that name set.
_GRAPH_NODES: Final = "MATCH (d:Dataset) WHERE d.name IN $names RETURN d.name, d.namespace, d.source_uri, d.tags"
_GRAPH_EDGES: Final = "MATCH (a:Dataset)-[:DERIVED_FROM]->(b:Dataset) WHERE a.name IN $names AND b.name IN $names RETURN DISTINCT a.name, b.name"
# The estate-wide variants: every dataset node / DERIVED_FROM edge in one read each, so the graph
# UI gets the whole picture in ONE request instead of recomposing it client-side from a
# per-dataset fan-out (which cost 2N+ HTTP calls per poll tick at N datasets).
_ESTATE_NODES: Final = "MATCH (d:Dataset) RETURN d.name, d.namespace, d.source_uri, d.tags"
_ESTATE_EDGES: Final = "MATCH (a:Dataset)-[:DERIVED_FROM]->(b:Dataset) RETURN DISTINCT a.name, b.name"
# Per-node write rollup for the estate read: written versions + any-failed, folded in Python
# (_fold_writes). Keeps the graph UI's node badges (versions, failed ring) at ONE request instead
# of a per-dataset /producers fan-out.
_ESTATE_WRITES: Final = "MATCH (r:Run)-[w:WROTE]->(d:Dataset) RETURN d.name, w.version, r.event_type"

# Column-level lineage (#24). A (:Column {dataset, field}) is MERGEd on the 2-tuple of SCALAR props
# (no concatenated id — dataset names contain '$', so any delimiter could collide). ``dataset`` is the
# owning :Dataset.name (also the governance handle, denormalised so a query never joins via HAS_COLUMN).
# The typed seed sets ``type`` from the schema facet; the stub (for an input column whose dataset isn't
# ingested yet) sets ONLY namespace — never ``type`` — so it can't clobber a real type with null.
_MERGE_COLUMN: Final = "MERGE (c:Column {dataset:$ds, field:$fld}) SET c.namespace=$ns RETURN 1"
_MERGE_COLUMN_TYPED: Final = "MERGE (c:Column {dataset:$ds, field:$fld}) SET c.namespace=$ns, c.type=$type RETURN 1"
_LINK_HAS_COLUMN: Final = "MATCH (d:Dataset {name:$ds}),(c:Column {dataset:$ds, field:$fld}) MERGE (d)-[:HAS_COLUMN]->(c) RETURN 1"
# Column-inventory GC (2026-07-11): a schema facet is the COMPLETE current column set by contract, so
# after seeding it, HAS_COLUMN links to fields outside it are STALE inventory (an overwrite replaced
# the schema — {a,b}→{x,y} used to leave a,b listed forever). Only the LINK is deleted: the :Column
# node and its COL_DERIVED_FROM edges stay, so historical column lineage (and per-version schemas on
# WROTE) are untouched — this prunes what dataset_column_graph() presents as CURRENT.
_UNLINK_STALE_COLUMNS: Final = "MATCH (d:Dataset {name:$ds})-[r:HAS_COLUMN]->(c:Column) WHERE NOT c.field IN $fields DELETE r RETURN 1"
# DISTINCT label (NOT the dataset-level DERIVED_FROM): AGE's *1.. constrains only path ENDPOINTS, not
# intermediate edge labels, so reusing DERIVED_FROM would let a column traversal silently cross onto the
# dataset plane if the two ever connect. Direction output→input, mirroring dataset DERIVED_FROM.
_COL_DERIVED_FROM: Final = "MATCH (o:Column {dataset:$ods, field:$ofld}),(i:Column {dataset:$ids, field:$ifld}) MERGE (o)-[:DERIVED_FROM_COLUMN]->(i) RETURN 1"
# Edge props are SET in their own statement (AGE 1.5.0 drops a $param in a SET fused to a MERGE-on-edge).
# All scalars — masking is a plain bool; the multi-valued transformations[] is collapsed to type/subtype
# at parse time precisely to avoid an array-in-SET (the path AGE mishandles).
_SET_COL_EDGE: Final = (
    "MATCH (o:Column {dataset:$ods, field:$ofld})-[e:DERIVED_FROM_COLUMN]->"
    "(i:Column {dataset:$ids, field:$ifld}) "
    "SET e.transformation_type=$tt, e.transformation_subtype=$st, e.masking=$mask, e.description=$desc, "
    "e.run_id=$rid, e.output_version=$ver RETURN 1"
)
_COL_UPSTREAM: Final = (
    "MATCH (c:Column {dataset:$ds, field:$fld})-[:DERIVED_FROM_COLUMN*1..]->(u:Column) RETURN DISTINCT u.dataset, u.field, u.namespace, u.type"
)
_COL_DOWNSTREAM: Final = (
    "MATCH (c:Column {dataset:$ds, field:$fld})<-[:DERIVED_FROM_COLUMN*1..]-(x:Column) RETURN DISTINCT x.dataset, x.field, x.namespace, x.type"
)
# Per-dataset column view: the dataset's OWN columns (complete typed inventory via HAS_COLUMN, incl.
# columns with no declared lineage) + every column edge touching the dataset (either endpoint).
# DISTINCT: :Column has no UNIQUE index (unlike Run/Dataset/Job — deliberately, since duplicate column
# vertices from a rare concurrent first-create are benign and an index would add abort/retry churn to the
# hot column path). Two concurrent ingests that first-touch the same (dataset, field) can each MATCH-miss
# and CREATE, leaving a duplicate :Column + duplicate HAS_COLUMN; DISTINCT collapses them so the inventory
# lists each field once regardless. The upstream/downstream column walks already RETURN DISTINCT.
_DATASET_COLUMN_NODES: Final = "MATCH (d:Dataset {name:$ds})-[:HAS_COLUMN]->(c:Column) RETURN DISTINCT c.field, c.type ORDER BY c.field"
_DATASET_COLUMN_EDGES: Final = (
    "MATCH (o:Column)-[e:DERIVED_FROM_COLUMN]->(i:Column) WHERE o.dataset=$ds OR i.dataset=$ds "
    "RETURN DISTINCT o.dataset, o.field, i.dataset, i.field, "
    "e.transformation_type, e.transformation_subtype, e.masking, e.description"
)


def _tags_from(value: object) -> list[str]:
    """Split the comma-joined ``tags`` node property back into a list (``None``/"" → [])."""
    return value.split(",") if isinstance(value, str) and value else []


_NO_WRITES: Final[tuple[list[str], bool]] = ([], False)


def _fold_writes(rows: list[list[Any]]) -> dict[str, tuple[list[str], bool]]:
    """Fold ``_ESTATE_WRITES`` rows (dataset, version, event_type) into per-dataset node badges:
    the distinct written versions (sorted) and whether any producing run failed/aborted."""
    folded: dict[str, tuple[set[str], bool]] = {}
    for dataset, version, event_type in rows:
        versions, failed = folded.setdefault(dataset, (set(), False))
        if version:
            versions.add(str(version))
        if not failed and isinstance(event_type, str) and event_type.upper() in ("FAIL", "ABORT"):
            folded[dataset] = (versions, True)
    return {name: (sorted(versions), failed) for name, (versions, failed) in folded.items()}


class LineageRepository:
    """Reads and writes the OpenLineage graph in one Apache AGE database."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        graph: str,
        events_retention: int = 0,
        statement_timeout_seconds: float = 30.0,
    ) -> None:
        self._pool = pool
        self._graph = graph
        self._events_retention = events_retention
        # The same configured value make_pool sets session-wide on every pooled connection — used to
        # bound the first-boot DDL with a transaction-scoped SET LOCAL (see ensure_events_table).
        self._statement_timeout_seconds = statement_timeout_seconds

    async def ingest_event(self, event: RunEvent) -> None:
        """Upsert the run, its job, its datasets, and their edges in one transaction."""
        async with self._pool.connection() as conn, conn.transaction():
            await run_cypher(
                conn,
                self._graph,
                _MERGE_JOB,
                {"ns": event.job.namespace, "nm": event.job.name},
            )
            source_location = event.job.source_location
            if source_location:
                await run_cypher(
                    conn,
                    self._graph,
                    _SET_JOB_SOURCE,
                    {"ns": event.job.namespace, "nm": event.job.name, "src": json.dumps(source_location)},
                )
            await run_cypher(
                conn,
                self._graph,
                _MERGE_RUN,
                {
                    "rid": event.run.run_id,
                    "et": event.event_type,
                    "tm": event.event_time,
                    "au": event.author or "",
                    "pr": event.producer or "",
                    "err": event.error_message or "",
                    "job": f"{event.job.namespace}/{event.job.name}",
                    # The catalog operation (create_table/drop_table/rename_table/…) as a first-class Run
                    # property so /producers + /runs can name a drop/rename as such (not just a versionless
                    # WROTE); "" for events that carry no lance-facet operation (external producers).
                    "op": event.operation or "",
                    "srid": event.source_run_id or "",
                },
            )
            progress = event.progress
            if progress is not None:
                await run_cypher(
                    conn,
                    self._graph,
                    _SET_RUN_PROGRESS,
                    {"rid": event.run.run_id, "pd": progress[0], "pt": progress[1]},
                )
            output_names = [ds.name for ds in event.outputs]
            if output_names:
                await run_cypher(
                    conn,
                    self._graph,
                    _SET_RUN_OUTPUTS,
                    {"rid": event.run.run_id, "outs": ",".join(output_names)},
                )
            await run_cypher(
                conn,
                self._graph,
                _LINK_RUN_JOB,
                {"rid": event.run.run_id, "ns": event.job.namespace, "nm": event.job.name},
            )
            # Merge EVERY dataset vertex this event touches in ONE name-sorted, property-bearing pass —
            # the ONLY place in this transaction that inserts or updates Dataset rows. Both lock kinds
            # need the total order: the unique-index INSERT lock (concurrent first-create of a shared
            # dataset blocks the loser on the winner) AND the row-UPDATE lock the SET takes on an
            # already-existing vertex. Two separately-sorted loops were not total (an input of one event
            # can be an output of the other), and a bare pre-create pass orders only the inserts — a
            # matching bare MERGE takes no lock, so unsorted SETs behind it still deadlock on tuple
            # locks. One sorted pass makes the acquisition order total, eliminating deadlocks between
            # overlapping ingests; a concurrent same-row update can still abort one side (AGE surfaces
            # Postgres's concurrent-update error), which self-heals via Dapr redelivery. For a name that
            # is both input and output, the input ref merges first so the output's SETs win — the same
            # precedence the old input-then-output loops applied. Column-lineage upstreams (facet-only
            # references _ingest_columns links against) join the same pass, success-gated so a failed
            # run grows no stub vertices; the loops below only link edges and never touch Dataset rows.
            plan: dict[str, list[Dataset]] = {}
            for ds in [*event.inputs, *event.outputs]:
                plan.setdefault(ds.vertex_name, []).append(ds)
            stub_ns: dict[str, str] = {}
            if event.is_success:
                for out in event.outputs:
                    for edge in out.column_edges:
                        if edge["name"] not in plan:
                            stub_ns.setdefault(vertex_name_for(edge["namespace"], edge["name"]), edge["namespace"])
            for name in sorted(plan.keys() | stub_ns.keys()):
                for ds in plan.get(name, []):
                    await self._merge_dataset(conn, ds)
                if name in stub_ns:
                    await run_cypher(conn, self._graph, _MERGE_DATASET, {"name": name, "ns": stub_ns[name]})
            for ds in event.inputs:
                await run_cypher(
                    conn,
                    self._graph,
                    _LINK_READ,
                    {"rid": event.run.run_id, "name": ds.vertex_name},
                )
                # A PINNED read records which version it consumed (the TRAIN job's feature pins — #115's
                # reproducibility claim). Unlike the WROTE version this is NOT gated on is_success: a FAILed
                # run still truthfully read those versions, and the pin is what makes the failure diagnosable.
                in_version = event.input_version(ds.name)
                if in_version:
                    await run_cypher(
                        conn,
                        self._graph,
                        _SET_READ_VERSION,
                        {"rid": event.run.run_id, "name": ds.vertex_name, "ver": in_version},
                    )
            for ds in event.outputs:
                # A failed run keeps a WROTE edge (so producers() shows the attempt) but no version —
                # it produced no data, so it must not claim to have written a Lance version.
                await run_cypher(conn, self._graph, _LINK_WROTE, {"rid": event.run.run_id, "name": ds.vertex_name})
                version = event.output_version(ds.name) if event.is_success else None
                if version:
                    await run_cypher(
                        conn,
                        self._graph,
                        _SET_WROTE_VERSION,
                        {"rid": event.run.run_id, "name": ds.vertex_name, "ver": version},
                    )
                    # Persist the column schema AT this version onto the same edge (#24 prerequisite):
                    # a successful versioned write's schema is the dataset's schema at that Lance version.
                    if ds.fields:
                        await run_cypher(
                            conn,
                            self._graph,
                            _SET_WROTE_SCHEMA,
                            {"rid": event.run.run_id, "name": ds.vertex_name, "schema": json.dumps(ds.fields)},
                        )
                    # Runtime-measured output statistics (rows + on-disk bytes) onto the same edge — present
                    # only when the compute actually measured the write (a dummy emit omits the facet).
                    stats = ds.statistics
                    if stats is not None:
                        await run_cypher(
                            conn,
                            self._graph,
                            _SET_WROTE_STATS,
                            {
                                "rid": event.run.run_id,
                                "name": ds.vertex_name,
                                "rows": stats[0],
                                "size": stats[1],
                            },
                        )
                    # Quality-gate result onto the same edge — present only when the quality gate validated
                    # the write. A passed=false edge (with a real version) records a batch the gate blocked.
                    assertions = ds.quality_assertions
                    if assertions:
                        await run_cypher(
                            conn,
                            self._graph,
                            _SET_WROTE_QUALITY,
                            {
                                "rid": event.run.run_id,
                                "name": ds.vertex_name,
                                "passed": all(a["success"] for a in assertions),
                                "assertions": json.dumps(assertions),
                            },
                        )
            # Only a successful run asserts lineage: a failed run derived nothing.
            if event.is_success:
                for out in event.outputs:
                    for inp in event.inputs:
                        # An in-place refinement (reads and writes the same table — e.g. add a column)
                        # bumps the version via WROTE; it is NOT a self-DERIVED_FROM edge.
                        if out.name == inp.name:
                            continue
                        await run_cypher(
                            conn,
                            self._graph,
                            _DERIVED_FROM,
                            {"on": out.name, "inp": inp.name},
                        )
                await self._ingest_columns(conn, event)
            # A successful table-origination event (create/register/declare) carries the verified author →
            # record who created the table as a first-class (:User)-[:CREATED]->(:Dataset) edge.
            if event.is_success and event.operation in _CREATE_OPS and event.author:
                await run_cypher(conn, self._graph, _MERGE_USER, {"name": event.author})
                for ds in event.outputs:
                    await run_cypher(
                        conn,
                        self._graph,
                        _LINK_CREATED,
                        {"name": event.author, "ds": ds.name, "tm": event.event_time},
                    )

    async def _schema_is_current(self, conn, name: str, version: str) -> bool:
        """True when ``version`` is at least the newest WROTE version the graph records for ``name``
        — the recency gate that makes the column-inventory seeding AND prune idempotent under
        redelivery reordering. Unparseable versions → False (never touch the inventory on
        uncertain ordering)."""
        rows = await run_cypher(conn, self._graph, _LATEST_WRITE_VERSION, {"name": name}) or []
        if not rows or rows[0][0] is None:
            return True  # nothing recorded yet — this event defines the inventory
        try:
            return int(version) >= int(rows[0][0])
        except (TypeError, ValueError):
            return False

    async def _merge_dataset(self, conn, ds: Dataset) -> None:
        await run_cypher(
            conn,
            self._graph,
            _MERGE_DATASET,
            {"name": ds.vertex_name, "ns": ds.namespace},
        )
        if ds.source_uri:
            await run_cypher(conn, self._graph, _SET_DATASET_SRC, {"name": ds.vertex_name, "src": ds.source_uri})
        if ds.tags:
            # Union into the existing set, never overwrite (#49): the node also carries human-curated
            # governance tags now, and a producer refreshing its facet must not wipe them. A user-removed
            # producer tag therefore returns on that producer's next tagged run — honest behavior (the
            # producer keeps asserting it). Curated tags survive any SINGLE ingest; a concurrent
            # curation and ingest remain last-writer-wins (millisecond window, human edits are rare).
            # Facet labels are unvalidated producer strings: one containing the comma JOIN separator
            # would splinter on read and then re-append on every run (unbounded growth — audit
            # 2026-07-16), so commas are stripped here and the merge dedupes order-preservingly.
            rows = await run_cypher(conn, self._graph, _GET_DATASET_GOVERNANCE, {"name": ds.vertex_name}, columns=6)
            existing = _tags_from(rows[0][0]) if rows else []
            sanitized = [tag.replace(",", "_") for tag in ds.tags]
            merged = list(dict.fromkeys(existing + sanitized))
            await run_cypher(conn, self._graph, _SET_DATASET_TAGS, {"name": ds.vertex_name, "tags": ",".join(merged)})

    async def _ingest_columns(self, conn, event: RunEvent) -> None:
        """Materialise column nodes + field-to-field edges from each output's schema/columnLineage (#24).

        Caller guarantees ``event.is_success`` (a failed run asserts no data). Per output dataset: (1) seed
        the FULL typed column set from the ``schema`` facet so even columns with no declared lineage exist
        with their Arrow type; (2) for each declared ``out_field ← input`` dependency, create the column
        edge — defensively ensuring the input's dataset + columns exist (its type stays null until that
        dataset is itself ingested as an output, so the stub never clobbers a real type).
        """
        for out in event.outputs:
            # Recency-gate the ENTIRE schema seeding, not just the prune (live-AGE CI catch,
            # 2026-07-11: the first cut gated only the unlink, so a STALE redelivered event
            # re-ADDED its old columns via the grow-only MERGEs — ['a','b','x','y'] on real AGE).
            # A schema facet is only allowed to touch the CURRENT inventory when its version is at
            # least the newest the graph knows: same-version redeliveries (the common ackWait case)
            # replay idempotently; strictly-older events add NOTHING and prune NOTHING (convergent).
            # A version-LESS schema event (external producers) keeps the legacy grow-only seeding —
            # ordering is unknowable there, and losing their inventory would be a regression.
            version = event.output_version(out.name) or ""
            seed_and_prune = bool(out.fields) and (not version or await self._schema_is_current(conn, out.name, version))
            if seed_and_prune:
                for col in out.fields:
                    await run_cypher(
                        conn,
                        self._graph,
                        _MERGE_COLUMN_TYPED,
                        {
                            "ds": out.name,
                            "fld": col["name"],
                            "ns": out.namespace,
                            "type": col.get("type", ""),
                        },
                    )
                    await run_cypher(conn, self._graph, _LINK_HAS_COLUMN, {"ds": out.name, "fld": col["name"]})
            if seed_and_prune and version:
                # The schema facet is the COMPLETE current schema — unlink inventory entries outside
                # it (∪ the column-edge out_fields ingested below, which are also current columns) so
                # an overwrite's replaced columns stop being listed as CURRENT. Never pruned for a
                # version-less event: partial ordering knowledge must never unlink live columns.
                current = sorted({col["name"] for col in out.fields} | {e["out_field"] for e in out.column_edges})
                await run_cypher(conn, self._graph, _UNLINK_STALE_COLUMNS, {"ds": out.name, "fields": current})
            for edge in out.column_edges:
                in_ds, in_fld, out_fld = edge["name"], edge["field"], edge["out_field"]
                # Skip ONLY a true identity self-loop (same dataset AND same field — a no-op carry-forward).
                # Same-dataset *different*-field edges (caption ← embedding) are the in-place-refinement
                # column flow that is the core value here, so they are KEPT (unlike the dataset self-skip).
                if in_ds == out.name and in_fld == out_fld:
                    continue
                # The input dataset may not appear in event.inputs (facet-only reference) — its vertex is
                # guaranteed by ingest_event's sorted merge pass (which includes column-edge upstreams), so
                # no Dataset-row write happens here: a merge in facet order would break the total lock order.
                await run_cypher(conn, self._graph, _MERGE_COLUMN, {"ds": in_ds, "fld": in_fld, "ns": edge["namespace"]})
                await run_cypher(conn, self._graph, _LINK_HAS_COLUMN, {"ds": in_ds, "fld": in_fld})
                await run_cypher(conn, self._graph, _MERGE_COLUMN, {"ds": out.name, "fld": out_fld, "ns": out.namespace})
                await run_cypher(conn, self._graph, _LINK_HAS_COLUMN, {"ds": out.name, "fld": out_fld})
                await run_cypher(
                    conn,
                    self._graph,
                    _COL_DERIVED_FROM,
                    {"ods": out.name, "ofld": out_fld, "ids": in_ds, "ifld": in_fld},
                )
                await run_cypher(
                    conn,
                    self._graph,
                    _SET_COL_EDGE,
                    {
                        "ods": out.name,
                        "ofld": out_fld,
                        "ids": in_ds,
                        "ifld": in_fld,
                        "tt": edge["type"],
                        "st": edge["subtype"],
                        "mask": edge["masking"],
                        "desc": edge.get("description", ""),
                        "rid": event.run.run_id,
                        "ver": version,
                    },
                )

    async def upstream(self, name: str) -> Neighbors:
        """Datasets ``name`` is (transitively) derived from — its provenance."""
        rows = await fetch(self._pool, self._graph, _UPSTREAM, {"name": name}, columns=2)
        return Neighbors(dataset=name, related=[DatasetRef(name=r[0], namespace=r[1]) for r in rows])

    async def downstream(self, name: str) -> Neighbors:
        """Datasets that are (transitively) derived from ``name`` — its impact."""
        rows = await fetch(self._pool, self._graph, _DOWNSTREAM, {"name": name}, columns=2)
        return Neighbors(dataset=name, related=[DatasetRef(name=r[0], namespace=r[1]) for r in rows])

    async def run_inputs(self, run_id: str) -> RunInputs:
        """One run's direct inputs with the pinned version it read on each (the READ-edge version).

        For a training run this is *which feature versions produced this model* — #115 D1's
        reproducibility claim, previously reachable only by Cypher. Ungoverned here (name+version only);
        the endpoint drops inputs the caller can't see. ``e.version`` is ``""``/absent → ``None`` (an
        unpinned floating read, e.g. a mover reading its upstream stage without a pin)."""
        rows = await fetch(self._pool, self._graph, _RUN_INPUTS, {"rid": run_id}, columns=2)
        return RunInputs(
            run_id=run_id,
            inputs=[RunInput(name=r[0], version=(r[1] or None)) for r in rows if r[0]],
        )

    async def column_upstream(self, dataset: str, field: str) -> ColumnNeighbors:
        """The columns ``(dataset, field)`` was (transitively) derived from — field-level provenance. (#24)

        Every related column carries its owning ``dataset`` so the endpoint can drop columns the caller
        may not see. The distinct ``DERIVED_FROM_COLUMN`` label keeps the walk on the column plane.
        """
        rows = await fetch(self._pool, self._graph, _COL_UPSTREAM, {"ds": dataset, "fld": field}, columns=4)
        return ColumnNeighbors(
            dataset=dataset,
            field=field,
            related=[ColumnRef(dataset=r[0], field=r[1], namespace=r[2], type=r[3]) for r in rows],
        )

    async def column_downstream(self, dataset: str, field: str) -> ColumnNeighbors:
        """The columns (transitively) derived from ``(dataset, field)`` — field-level impact. (#24)"""
        rows = await fetch(self._pool, self._graph, _COL_DOWNSTREAM, {"ds": dataset, "fld": field}, columns=4)
        return ColumnNeighbors(
            dataset=dataset,
            field=field,
            related=[ColumnRef(dataset=r[0], field=r[1], namespace=r[2], type=r[3]) for r in rows],
        )

    async def dataset_column_graph(self, name: str) -> ColumnGraph:
        """The column-level subgraph around ``name``: its own columns + every edge touching them. (#24)

        Nodes = ``name``'s complete typed column inventory (via HAS_COLUMN) plus the neighbour columns on
        the far end of each edge (untyped — they belong to other datasets). Edges flow source→target where
        ``target`` is the derived column. Owning ``dataset`` rides every node/edge for the visibility drop.
        """
        node_rows = await fetch(self._pool, self._graph, _DATASET_COLUMN_NODES, {"ds": name}, columns=2)
        edge_rows = await fetch(self._pool, self._graph, _DATASET_COLUMN_EDGES, {"ds": name}, columns=8)
        nodes = [ColumnNode(dataset=name, field=r[0], type=r[1]) for r in node_rows]
        seen = {(name, r[0]) for r in node_rows}
        edges: list[ColumnEdge] = []
        for o_ds, o_fld, i_ds, i_fld, tt, st, mask, desc in edge_rows:
            edges.append(
                ColumnEdge(
                    source_dataset=i_ds,
                    source_field=i_fld,
                    target_dataset=o_ds,
                    target_field=o_fld,
                    transformation_type=tt or "",
                    transformation_subtype=st or "",
                    masking=bool(mask),
                    description=desc or "",
                )
            )
            for ds, fld in ((o_ds, o_fld), (i_ds, i_fld)):
                if (ds, fld) not in seen:
                    seen.add((ds, fld))
                    nodes.append(ColumnNode(dataset=ds, field=fld))
        return ColumnGraph(root=name, columns=nodes, edges=edges)

    async def producers(self, name: str) -> Producers:
        """The runs that wrote (or failed to write) ``name`` — who / when / how / version / error."""
        rows = await fetch(self._pool, self._graph, _PRODUCERS, {"name": name}, columns=12)
        return Producers(
            dataset=name,
            producers=[
                ProducerInfo(
                    run_id=r[0],
                    author=r[1],
                    event_time=r[2],
                    event_type=r[3],
                    dataset_version=(r[4] or None),
                    producer=(r[5] or None),
                    error_message=(r[6] or None),
                    row_count=(int(r[7]) if r[7] is not None else None),
                    size_bytes=(int(r[8]) if r[8] is not None else None),
                    # NOT `r[9] or None` — quality_passed is a bool, and `False or None` would silently
                    # drop a failed-quality signal. _parse already json.loads it to a real Python bool.
                    quality_passed=(r[9] if r[9] is not None else None),
                    # Stored as a JSON string (like schema); _parse unwraps one agtype layer, so json.loads
                    # the remaining string back into the assertions list.
                    quality_assertions=(json.loads(r[10]) if r[10] else []),
                    # The catalog op (drop_table/rename_table/create_table/…) so a reader can tell a drop from
                    # a plain versionless write without cross-referencing /events; "" maps back to None.
                    operation=(r[11] or None),
                )
                for r in rows
            ],
        )

    async def latest_write_version(self, name: str) -> int | None:
        """The Lance version on the most-recent successful WROTE edge for ``name`` (the version the
        lineage graph believes is current), or ``None`` if ``name`` was never successfully written. (#23)"""
        rows = await fetch(self._pool, self._graph, _LATEST_WRITE_VERSION, {"name": name}, columns=1)
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    async def dropped_at(self, name: str) -> str | None:
        """When ``name`` is TERMINALLY dropped — the event time of its most recent SUCCESSFUL run
        being a ``drop_table`` — else None.

        DERIVED from run history at read time, never a stored flag (review 2026-07-11: a mutable
        stamp was last-delivery-wins under at-least-once redelivery — a stale redelivered drop
        after a recreate would remove a live dataset from the reconcile sweep). A recreate is
        simply a newer successful non-drop run, so the derivation flips back automatically. The
        reconcile sweep skips dropped datasets: after a deliberate drop, absence on storage is the
        EXPECTED state — flagging it missing_on_storage forever was the false-alarm bug.
        """
        rows = await fetch(self._pool, self._graph, _DATASET_LAST_SUCCESS_OP, {"name": name}, columns=2)
        if rows and rows[0][0] == "drop_table":
            return rows[0][1] or ""
        return None

    async def source_uri(self, name: str) -> str | None:
        """The storage location (``dataSource`` URI) recorded for ``name``, or ``None`` if unknown. (#23)"""
        rows = await fetch(self._pool, self._graph, _SOURCE_URI, {"name": name}, columns=1)
        return rows[0][0] if rows and rows[0][0] is not None else None

    async def dataset_schema(self, name: str, version: int | None = None) -> DatasetSchema:
        """The persisted column schema for ``name`` — at ``version`` if given, else the latest. (#24)

        Empty ``fields`` (and ``version=None``) means no schema has been persisted for that dataset/
        version yet. The schema is stored as a JSON string on the ``WROTE`` edge, so ``_parse`` returns
        the string and we ``json.loads`` it back into the field list.
        """
        # WROTE.version is stored as the OpenLineage datasetVersion *string* ("1"), so the at-version
        # match must compare strings — an int $ver silently matches nothing (the bug #23's int() coercion
        # on read papered over). The version we return is still coerced back to int below.
        query, params = (_SCHEMA_AT_VERSION, {"name": name, "ver": str(version)}) if version is not None else (_SCHEMA_LATEST, {"name": name})
        rows = await fetch(self._pool, self._graph, query, params, columns=2)
        if not rows or rows[0][0] is None:
            return DatasetSchema(dataset=name, version=version, fields=[])
        raw, ver = rows[0]
        fields = [SchemaField.model_validate(f) for f in json.loads(raw)] if isinstance(raw, str) else []
        return DatasetSchema(dataset=name, version=int(ver) if ver is not None else None, fields=fields)

    async def list_runs(self) -> Runs:
        """Every run's current lifecycle state, folded onto its ``(:Run)`` node in AGE.

        Durable replacement for the in-memory fold: survives a restart and is shared across replicas.
        ``event_type``/``event_time`` are the last-event-wins state/updated_at; ``""`` maps back to None.
        """
        rows = await fetch(self._pool, self._graph, _LIST_RUNS, columns=13)
        runs = [
            RunStatus(
                run_id=r[0],
                job=(r[1] or None),
                author=(r[2] or None),
                state=(r[3] or None),
                progress_done=r[4],
                progress_total=r[5],
                error_message=(r[6] or None),
                started_at=(r[7] or None),
                updated_at=(r[8] or None),
                events=int(r[9] or 0),
                outputs=_tags_from(r[10]),
                operation=(r[11] or None),
                source_run_id=(r[12] or None),
            )
            for r in rows
        ]
        runs.sort(key=lambda run: run.updated_at or "", reverse=True)
        return Runs(runs=runs)

    async def list_all_columns(self) -> list[tuple[str, str]]:
        """Every (dataset, field) in the CURRENT column inventory — the /search column tier."""
        rows = await fetch(self._pool, self._graph, _LIST_ALL_COLUMNS, columns=2)
        return [(r[0], r[1]) for r in rows if r[0] and r[1]]

    async def list_datasets(self, namespace: str | None = None, tag: str | None = None) -> list[DatasetSummary]:
        """Every dataset node (optionally filtered by namespace / tag), name-sorted — the browse list.

        Fetch-all + filter/sort in Python, mirroring :meth:`list_runs` (the graph's dataset count is
        modest). Governance and pagination are applied by the endpoint over this full list, so a page is
        taken from the *visible* set rather than truncating before the visibility filter has run.
        """
        rows = await fetch(self._pool, self._graph, _LIST_DATASETS, columns=3)
        out: list[DatasetSummary] = []
        for name, ns, raw_tags in rows:
            if not name:
                continue
            tags = _tags_from(raw_tags)
            if namespace is not None and (ns or None) != namespace:
                continue
            if tag is not None and tag not in tags:
                continue
            out.append(DatasetSummary(name=name, namespace=(ns or None), tags=tags))
        out.sort(key=lambda d: d.name)
        return out

    async def governance(self, name: str) -> DatasetGovernance | None:
        """The dataset's governance metadata (tags + description + attribution), or ``None`` if unknown."""
        rows = await fetch(self._pool, self._graph, _GET_DATASET_GOVERNANCE, {"name": name}, columns=6)
        if not rows:
            return None
        tags, description, tags_by, tags_at, desc_by, desc_at = rows[0]
        return DatasetGovernance(
            name=name,
            tags=_tags_from(tags),
            description=description or None,
            tags_updated_by=tags_by or None,
            tags_updated_at=tags_at or None,
            description_updated_by=desc_by or None,
            description_updated_at=desc_at or None,
        )

    async def set_tag(self, name: str, tag: str, *, present: bool, updated_by: str) -> DatasetGovernance | None:
        """Add (``present=True``) or remove a governance tag; returns the updated metadata, ``None`` if the
        dataset is unknown.

        Read-modify-write inside one transaction: the comma-joined ``tags`` string has no in-place list
        ops (the AGE array hazard), so concurrent curations are last-writer-wins per commit — the same
        semantics the ingest path's tag SET already has, acceptable for low-frequency human edits.
        Producer-set order is preserved; an added tag appends.
        """
        stamp = datetime.now(UTC).isoformat()
        async with self._pool.connection() as conn, conn.transaction():
            rows = await run_cypher(conn, self._graph, _GET_DATASET_GOVERNANCE, {"name": name}, columns=6)
            if not rows:
                return None
            tags = _tags_from(rows[0][0])
            if present and tag not in tags:
                tags.append(tag)
            elif not present and tag in tags:
                tags.remove(tag)
            await run_cypher(
                conn,
                self._graph,
                _SET_GOVERNED_TAGS,
                {"name": name, "tags": ",".join(tags), "by": updated_by, "at": stamp},
            )
            _, description, _, _, desc_by, desc_at = rows[0]
        log.info(
            "governance_tags_updated",
            extra={"dataset": name, "tag": tag, "present": present, "by": updated_by},
        )
        # The response is built from the values THIS transaction wrote — a post-commit re-read could
        # reflect a concurrent writer's state and make a successful PUT look like it did nothing.
        return DatasetGovernance(
            name=name,
            tags=tags,
            description=description or None,
            tags_updated_by=updated_by,
            tags_updated_at=stamp,
            description_updated_by=desc_by or None,
            description_updated_at=desc_at or None,
        )

    async def set_description(self, name: str, description: str, *, updated_by: str) -> DatasetGovernance | None:
        """Set (or clear, with ``""``) the dataset's description; ``None`` if the dataset is unknown."""
        stamp = datetime.now(UTC).isoformat()
        async with self._pool.connection() as conn, conn.transaction():
            rows = await run_cypher(conn, self._graph, _GET_DATASET_GOVERNANCE, {"name": name}, columns=6)
            if not rows:
                return None
            await run_cypher(
                conn,
                self._graph,
                _SET_DESCRIPTION,
                {"name": name, "desc": description, "by": updated_by, "at": stamp},
            )
            tags, _, tags_by, tags_at, _, _ = rows[0]
        log.info("governance_description_updated", extra={"dataset": name, "by": updated_by})
        return DatasetGovernance(
            name=name,
            tags=_tags_from(tags),
            description=description or None,
            tags_updated_by=tags_by or None,
            tags_updated_at=tags_at or None,
            description_updated_by=updated_by,
            description_updated_at=stamp,
        )

    async def list_jobs(self) -> list[JobSummary]:
        """Every job node with the set of datasets it wrote (its governance handle), name-sorted.

        One row per (job, written-dataset) is folded into per-job output sets; a job that has only read
        has an empty output set (and is dropped by :func:`governed` when auth is on, like a dataset-less
        ``/events`` row).
        """
        rows = await fetch(self._pool, self._graph, _LIST_JOBS, columns=3)
        outputs: dict[tuple[str | None, str], set[str]] = {}
        for ns, name, out_ds in rows:
            if not name:
                continue
            outs = outputs.setdefault((ns or None, name), set())
            if out_ds:
                outs.add(out_ds)
        jobs = [JobSummary(namespace=ns, name=name, outputs=sorted(outs)) for (ns, name), outs in outputs.items()]
        jobs.sort(key=lambda j: j.name)
        return jobs

    async def backfill_write(self, name: str, version: int, schema: SchemaFields | None = None) -> None:
        """Stamp the actual on-disk version onto the graph when a write's lineage event was lost (B4).

        The buildable half of the outbox problem: a crash between a Lance write and the sidecar publish drops
        the event, so the graph under-counts real writes. Reconciliation reads storage and, on drift, MERGEs
        a synthetic ``reconcile-<name>-v<version>`` run + a versioned ``WROTE`` edge to the dataset —
        idempotent (MERGE on the run id), so re-running never duplicates. The recovered provenance is minimal
        (``author='reconcile'``, no inputs): it records THAT the write happened + its version, not the lost
        details. ``schema`` (the on-disk column schema reconciliation read) rides the same edge so the
        recovered version carries its per-version schema (#24). The dataset node must already exist (it has
        the dataSource URI reconciliation read from).
        """
        # Spec-valid UUID runId, deterministic on the (name, version) seed so re-running reconcile MERGEs
        # the same (:Run) instead of duplicating it — the readable seed is not the id.
        rid = run_id_for(f"reconcile-{name}-v{version}")
        tm = datetime.now(UTC).isoformat()
        job = f"lance-reconcile/reconcile.{name}"
        params = {"rid": rid, "name": name}
        async with self._pool.connection() as conn, conn.transaction():
            # ONE transaction (like ingest_event) — on autocommit these were 4 independent statements, so a
            # crash mid-back-fill left a RECONCILED Run with no WROTE/version visible to /runs until the
            # NEXT sweep re-ran the idempotent MERGEs (§4). Atomic: no half-written window between sweeps.
            # Stamp job + outputs on the run so it appears CONSISTENTLY across views — /runs (governed by
            # the run's outputs) showed nothing for a job/outputs-less run while producers() showed it.
            await run_cypher(conn, self._graph, _BACKFILL_RUN, {"rid": rid, "tm": tm, "job": job, "outs": name})
            await run_cypher(conn, self._graph, _LINK_WROTE, params)
            await run_cypher(conn, self._graph, _SET_WROTE_VERSION, {**params, "ver": str(version)})
            # Recover the per-version schema onto the same edge when reconciliation could read it off storage.
            if schema:
                await run_cypher(conn, self._graph, _SET_WROTE_SCHEMA, {**params, "schema": json.dumps(schema)})
        # A feed row too, so /events also knows the reconcile (the third view) — the repair is auditable
        # next to the ingested writes it recovered.
        #
        # The blob is a REAL OpenLineage RunEvent, so a Marquez-style consumer replaying /events ingests it
        # unchanged. Two fidelity rules the first cut broke (found 2026-07-26 by validating the live feed
        # against https://openlineage.io/spec/2-0-2/OpenLineage.json — 14 of 200 events failed):
        #   * ``eventType`` must be in the spec enum. ``RECONCILED`` is not; ``OTHER`` is the spec's own slot
        #     for "additional metadata added to the same run [after it completed]", which is exactly what a
        #     back-fill is. The ``lance.operation="reconcile"`` marker (below) is what names it precisely.
        #   * every facet — custom ones included — must carry ``_producer`` + ``_schemaURL`` (BaseFacet
        #     ``required``). The bare ``{"operation": ...}`` dict didn't; ``custom_facet`` is the helper that
        #     stamps both, and is what every other emitter already uses.
        # ``event_type=RECONCILED`` stays on the ROW (and on the ``(:Run)`` node): it is our own storage
        # marker — the /events + /runs views and the ``_TERMINAL_TYPES`` dedup index key off it, and it must
        # stay distinguishable from a producer-sent OTHER.
        synthetic = {
            "eventType": "OTHER",
            "eventTime": tm,
            "producer": _RECONCILE_PRODUCER,
            "schemaURL": RUN_EVENT_SCHEMA_URL,
            "run": {
                "runId": rid,
                "facets": {"lance": custom_facet(_RECONCILE_PRODUCER, operation="reconcile", version=version)},
            },
            "job": {"namespace": "lance-reconcile", "name": f"reconcile.{name}"},
            "inputs": [],
            "outputs": [{"namespace": "", "name": name}],
        }
        await self.record_event(
            run_id=rid,
            event_type="RECONCILED",
            event_time=tm,
            job=job,
            author="reconcile",
            inputs=[],
            outputs=[name],
            event=synthetic,
        )

    async def prune_runs(self, cutoff_iso: str) -> int:
        """DETACH DELETE runs older than ``cutoff_iso`` in LIMIT-bounded batches (opt-in retention, §4).

        Called by the reconcile cron under its cluster-wide advisory lock (single-flight). Batched, one
        transaction per batch, so a large backlog (retention enabled late on a grown graph) can never
        push a single statement past the pool's statement_timeout — an all-or-nothing delete would be
        cancelled, rolled back, and retried identically forever. Returns the up-front count of prunable
        runs (approximate under concurrent ingest — the log signal, not an exactness contract).
        """
        async with self._pool.connection() as conn:
            rows = await run_cypher(conn, self._graph, _COUNT_OLD_RUNS, {"cutoff": cutoff_iso})
            count = int(rows[0][0]) if rows and rows[0] else 0
            batches = -(-count // _PRUNE_BATCH_SIZE)  # ceil; 0 batches when nothing to prune
            for _ in range(batches):
                async with conn.transaction():
                    await run_cypher(conn, self._graph, _PRUNE_OLD_RUNS_BATCH, {"cutoff": cutoff_iso})
        return count

    async def ensure_events_table(self) -> None:
        """Create the durable events-feed table if absent (idempotent; called once at startup).

        ``CREATE TABLE IF NOT EXISTS`` is not atomic against itself: two replicas booting at once can
        both pass the existence check and then race the create, so the loser raises ``DuplicateTable``.
        That's the success case — the table exists — so we swallow it. (#22 audit)

        The DDL runs in one transaction opened with ``SET LOCAL statement_timeout`` at the configured
        pool value, so its bound is carried by the transaction itself, not inherited from how the
        connection was made (prod-readiness P6): the dedup DELETEs + CREATE UNIQUE INDEX scan the whole
        table, and on a wedged/locked Postgres each statement is cancelled instead of hanging boot
        forever. ``SET LOCAL`` dies with the transaction, so nothing leaks into the pooled session; a
        timeout raises out of the lifespan (fail-closed boot → crash-loop), never a half-built feed.
        """
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(int(self._statement_timeout_seconds * 1000))))
                await conn.execute(_CREATE_EVENTS_TABLE)
                # Remove any pre-existing redelivered duplicates BEFORE each unique index, else CREATE UNIQUE
                # INDEX fails on a table populated before the dedup landed (the events feed is a diagnostic
                # projection, so dropping duplicate rows loses nothing but the duplication).
                await conn.execute(_DEDUP_EVENTS)
                await conn.execute(_CREATE_EVENTS_INDEX)
                await conn.execute(_DEDUP_TERMINAL)
                await conn.execute(_CREATE_TERMINAL_INDEX)
        except psycopg.errors.DuplicateTable:
            pass

    async def ensure_graph(self) -> None:
        """Create the AGE graph if it does not exist — idempotent, self-healing, concurrency-safe.

        The in-cluster ``age-postgres`` init SQL runs ``create_graph`` once, but the EXTERNAL managed-PG path
        (``age.externalHost``, ``age.enabled=false``) has NO such init, so without this the graph is never
        created and every ingest fails silently. Unlike :meth:`ensure_graph_constraints` (best-effort
        hardening), this is a HARD boot dependency — the graph IS lineage's storage — so a create that
        genuinely fails (AGE extension absent, no DDL grant) must fail the pod loudly, complementing the
        ``/readyz`` graph check. Checked-then-created (``create_graph`` errors if the graph exists); a
        concurrent replica winning the race between our check and create is benign (a re-check confirms it now
        exists), any other failure re-raises. Runs autocommit like the rest (the pool's ``configure``)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(_GRAPH_EXISTS, (self._graph,))
            if await cur.fetchone():
                return
            try:
                await conn.execute(sql.SQL("SELECT create_graph({})").format(sql.Literal(self._graph)))
                log.info("age_graph_created", extra={"graph": self._graph})
            except Exception:
                cur = await conn.execute(_GRAPH_EXISTS, (self._graph,))
                if not await cur.fetchone():
                    raise  # AGE missing / no DDL grant / wrong DB — a real boot failure, not the create race
                log.info("age_graph_create_raced", extra={"graph": self._graph})

    async def ensure_graph_constraints(self) -> None:
        """Add the per-label indexes: UNIQUE on each ``_VERTEX_UNIQUE_KEYS`` MERGE key (a CONCURRENT MERGE
        can't slip in a duplicate vertex, item 6) + plain LOOKUP on ``_VERTEX_LOOKUP_KEYS`` (index-speed
        MATCHes without the uniqueness churn — :Column, §4). Idempotent + safe on every replica boot:
        ``create_vlabel`` materializes
        the label's table (suppressed if it already exists), then ``CREATE UNIQUE INDEX IF NOT EXISTS`` on
        the property-access expression. Best-effort — a per-label failure (e.g. pre-existing dup rows on an
        already-populated graph, or an AGE build without the index recipe) is logged, not fatal, so the
        graph keeps ingesting; the guarantee holds wherever the index took. The pool's ``configure`` runs
        each statement autocommit with AGE loaded, so a raised ``create_vlabel`` never poisons the next."""
        plans = [(label, keys, True) for label, keys in _VERTEX_UNIQUE_KEYS] + [(label, keys, False) for label, keys in _VERTEX_LOOKUP_KEYS]
        async with self._pool.connection() as conn:
            for label, keys, unique in plans:
                with suppress(Exception):  # label already exists (a prior MERGE created it lazily) → fine
                    await conn.execute(sql.SQL("SELECT create_vlabel({}, {})").format(sql.Literal(self._graph), sql.Literal(label)))
                # ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties, '"<key>"'::agtype]) is AGE's
                # documented immutable property-access expression for a functional index (one term per key).
                exprs = sql.SQL(", ").join(
                    sql.SQL("ag_catalog.agtype_access_operator(VARIADIC ARRAY[properties, {}::agtype])").format(sql.Literal(f'"{key}"')) for key in keys
                )
                index = sql.SQL("CREATE {} INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                    sql.SQL("UNIQUE") if unique else sql.SQL(""),
                    sql.Identifier(f"{self._graph}_{label.lower()}_{'uniq' if unique else 'lookup'}"),
                    sql.Identifier(self._graph),
                    sql.Identifier(label),
                    exprs,
                )
                try:
                    await conn.execute(index)
                except Exception as exc:
                    log.warning("age_vertex_constraint_skipped", extra={"label": label, "error": str(exc)})

    @asynccontextmanager
    async def reconcile_lock(self) -> AsyncIterator[bool]:
        """Single-flight guard for the reconcile sweep (item 6): yields ``True`` if this caller acquired the
        cluster-wide advisory lock, ``False`` if another sweep already holds it. The cron fires on every
        replica's sidecar independently, so without this two sweeps would back-fill the graph in parallel.
        ``pg_try_advisory_lock`` is non-blocking (a busy tick simply skips, the next tick retries) and
        session-scoped — held on this dedicated connection for the sweep's duration, released in ``finally``
        (or automatically if the connection dies mid-sweep)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s)", (_RECONCILE_LOCK_KEY,))
            row = await cur.fetchone()
            acquired = bool(row and row[0])
            try:
                yield acquired
            finally:
                if acquired:
                    with suppress(Exception):
                        await conn.execute("SELECT pg_advisory_unlock(%s)", (_RECONCILE_LOCK_KEY,))

    async def record_event(
        self,
        *,
        run_id: str,
        event_type: str | None,
        event_time: str | None,
        job: str | None,
        author: str | None,
        inputs: list[str],
        outputs: list[str],
        event: dict[str, Any],
    ) -> None:
        """Append one ingested OpenLineage event to the durable feed (survives restart)."""
        async with self._pool.connection() as conn:
            await conn.execute(
                _INSERT_EVENT,
                (
                    run_id,
                    event_type,
                    event_time,
                    job,
                    author,
                    json.dumps(inputs),
                    json.dumps(outputs),
                    json.dumps(event),
                ),
            )
            if self._events_retention:
                await conn.execute(_PRUNE_EVENTS, (self._events_retention,))

    async def ensure_reads_table(self) -> None:
        """Create the read-audit log table if absent (idempotent, called on boot)."""
        async with self._pool.connection() as conn:
            await conn.execute(_CREATE_READS_TABLE)

    async def record_read(self, *, reader: str, dataset: str) -> None:
        """Append one read-audit row — who (``reader``) read which ``dataset`` (#6)."""
        async with self._pool.connection() as conn:
            await conn.execute(_INSERT_READ, (reader, dataset))

    async def readers(self, name: str, limit: int = 200) -> Readers:
        """Who READ ``name`` — aggregated per principal (last read + count), newest first (#41).

        The query surface for the read-audit log that :meth:`record_read` appends to; the log was
        capture-only until now. Safe when auditing is/was off: the table is created at startup, so this
        just returns an empty list rather than erroring.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(_READERS, (name, limit))
            rows = await cur.fetchall()
        return Readers(
            dataset=name,
            readers=[ReaderInfo(reader=r[0], last_read=r[1].isoformat() if r[1] else None, reads=r[2]) for r in rows],
        )

    async def list_events(self, limit: int = 500, *, after: int | None = None, summary: bool = False) -> list[EventRecord]:
        """The most-recent ingested events, newest first (durable — read from Postgres, not memory).

        ``after`` = keyset cursor (rows with ``seq < after`` — the previous page's last seq);
        ``summary`` skips the full-JSONB ``event`` column at the SQL layer (projection, not
        post-fetch stripping) for pollers that render only the summary fields.
        """
        if after is not None:
            query = _LIST_EVENTS_SUMMARY_AFTER if summary else _LIST_EVENTS_AFTER
            params: tuple[int, ...] = (after, limit)
        else:
            query = _LIST_EVENTS_SUMMARY if summary else _LIST_EVENTS
            params = (limit,)
        async with self._pool.connection() as conn:
            cur = await conn.execute(query, params)
            rows = await cur.fetchall()
        return [
            EventRecord(
                seq=r[0],
                event_type=r[1],
                event_time=r[2],
                job=r[3],
                author=r[4],
                inputs=r[5] or [],
                outputs=r[6] or [],
                event=(r[7] or {}) if not summary else {},
            )
            for r in rows
        ]

    async def oldest_event_seq(self) -> int | None:
        """The oldest seq the feed still holds, or ``None`` when it holds nothing.

        ``None`` rather than ``0`` on an empty feed: zero would read as "everything since the
        beginning is still here", which is the exact opposite of what an empty feed means, and would
        suppress the gap report a consumer needs most after a wipe.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(_OLDEST_EVENT_SEQ)
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def run_output_names(self, run_id: str) -> list[str]:
        """The datasets this run has ALREADY recorded writing — empty when the run does not exist.

        Read by `enforce_output_authz` to authorize MUTATING an existing run. The empty answer is the
        load-bearing one: it means "no run to protect", which is what lets a START event open a run it
        could never authorize (ingest's names only an external S3 prefix).
        """
        rows = await fetch(self._pool, self._graph, _RUN_OUTPUT_NAMES, {"rid": run_id}, columns=1)
        if not rows:
            return []
        raw = rows[0][0]
        if isinstance(raw, str):
            return [n for n in raw.split(",") if n]
        return [str(n) for n in raw] if isinstance(raw, list) else []

    async def creator(self, name: str) -> Creator:
        """Who created ``name`` — the verified principal on the catalog create event."""
        rows = await fetch(self._pool, self._graph, _CREATOR, {"name": name}, columns=1)
        return Creator(dataset=name, creator=rows[0][0] if rows else None)

    async def graph(self, name: str) -> LineageGraph:
        """The connected dataset-lineage subgraph around ``name`` (nodes + edges).

        Each node carries its storage location (``source_uri``) and governance ``tags`` so a
        DAG view can show *where* each table lives and *how* it is classified, not just its name.
        """
        up = await self.upstream(name)
        down = await self.downstream(name)
        names = list(dict.fromkeys([name, *(r.name for r in up.related), *(r.name for r in down.related)]))
        prop_rows = await fetch(self._pool, self._graph, _GRAPH_NODES, {"names": names}, columns=4)
        props = {r[0]: r for r in prop_rows}
        edge_rows = await fetch(self._pool, self._graph, _GRAPH_EDGES, {"names": names}, columns=2)
        return LineageGraph(
            root=name,
            nodes=[
                GraphNode(
                    id=n,
                    namespace=(props[n][1] if n in props else None),
                    source_uri=(props[n][2] if n in props else None),
                    tags=_tags_from(props[n][3] if n in props else None),
                )
                for n in names
            ],
            edges=[GraphEdge(source=r[0], target=r[1]) for r in edge_rows],
        )

    async def estate_graph(self) -> EstateGraph:
        """Every dataset node + ``DERIVED_FROM`` edge, ungoverned — the ``/graph`` bulk read.

        The endpoint owns governance (drop non-visible nodes, then edges touching them) and the
        honest node cap; this layer just reads the whole graph in two statements.
        """
        node_rows = await fetch(self._pool, self._graph, _ESTATE_NODES, columns=4)
        edge_rows = await fetch(self._pool, self._graph, _ESTATE_EDGES, columns=2)
        writes = _fold_writes(await fetch(self._pool, self._graph, _ESTATE_WRITES, columns=3))
        nodes = [
            GraphNode(
                id=r[0],
                namespace=r[1],
                source_uri=r[2],
                tags=_tags_from(r[3]),
                versions=writes.get(r[0], _NO_WRITES)[0],
                failed=writes.get(r[0], _NO_WRITES)[1],
            )
            for r in node_rows
        ]
        return EstateGraph(
            nodes=nodes,
            edges=[GraphEdge(source=r[0], target=r[1]) for r in edge_rows],
            total=len(nodes),
        )
