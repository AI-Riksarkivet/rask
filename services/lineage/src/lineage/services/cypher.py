"""The openCypher this service speaks to AGE — one statement per name, and nothing that executes them.

Split out of ``repository.py`` (F-LIN-03): the repository was a 1500-line module that DEFINED two query
languages and also ran them. Keeping the dialect in its own module means a statement can be read next to
its siblings, the graph's shape is legible in one place, and the executor is what the class is about.

Every constant is a ``LiteralString`` by construction, which is load-bearing: ``core.age._sql`` embeds the
Cypher as raw SQL inside AGE's ``$$ … $$`` quoting and its ``LiteralString`` parameter is what stops
caller data from ever reaching that slot. Row data flows through the ``$key`` agtype params instead.

The graph these statements build::

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

Datasets are MERGEd on ``{name}`` only (then ``namespace`` / ``source_uri`` / ``tags`` are SET) so a
dataset referenced by several runs is never duplicated.
"""

from __future__ import annotations

from typing import Final, LiteralString, cast


MERGE_JOB: Final = "MERGE (j:Job {namespace:$ns, name:$nm}) RETURN 1"
# Where the job's code lives (the standard sourceCodeLocation facet), as a JSON string scalar on the Job
# node — SET only when the event carries it, so an event that omits it never clobbers a prior value.
SET_JOB_SOURCE: Final = "MATCH (j:Job {namespace:$ns, name:$nm}) SET j.source_location=$src RETURN 1"
# The (:Run) node folds the whole lifecycle so /runs is durable (survives restart, replica-shared)
# instead of folding an in-memory buffer: event_type IS the current state and event_time IS
# updated_at (both last-event-wins via the repeated SET); started_at keeps the first event's time;
# events_count counts lifecycle events RECEIVED (incl. redeliveries — it is a delivery counter, not a
# distinct-event count; the graph nodes/edges are idempotent, the feed dedups). job is denormalised so
# /runs needs no OF_JOB join.
MERGE_RUN: Final = (
    "MERGE (r:Run {run_id:$rid}) "
    "SET r.event_type=$et, r.event_time=$tm, r.author=$au, r.producer=$pr, r.error_message=$err, "
    # operation is STICKY (like started_at): a later event of the same run that carries no lance facet
    # ($op='') must not erase the operation an earlier event declared (START stamps it, terminal may not).
    "r.job=$job, r.operation=(CASE WHEN $op = '' THEN r.operation ELSE $op END), "
    # source_run_id is STICKY for the same reason as operation: the producer stamps its own run id
    # on every event it emits, but a reconcile/backfill event for the same graph run carries none
    # and must not erase it. Empty string means the event did not say; only a non-empty value writes.
    "r.source_run_id=(CASE WHEN $srid = '' THEN r.source_run_id ELSE $srid END), "
    # promotion_status is STICKY for the same reason as the two above: a reconcile or backfill event
    # for the same graph run carries no lance facet, and clobbering the verdict to null would turn a
    # recorded hold back into an ordinary failure on the next tick.
    "r.promotion_status=(CASE WHEN $ps = '' THEN r.promotion_status ELSE $ps END), "
    # consumed_to_version is STICKY for the same reason as the three above, with a NUMERIC sentinel:
    # a version is an int, so the empty string those use is not available. -1 means "this event did not
    # say", which `RunEvent.consumed_to_version` refuses as producer data precisely so the two cannot
    # collide. Clobbering it would erase the cascade's delta boundary on the next reconcile tick — the
    # one fact the lag detector reads.
    "r.consumed_to_version=(CASE WHEN $ctv < 0 THEN r.consumed_to_version ELSE $ctv END), "
    # consumed_from_version is STICKY on the identical rule, and it is the one that must not be lost:
    # the floor is what makes a history CONTIGUOUS. Clobbered to null on a reconcile tick, a
    # gap-free chain of ranges reads as a gap and the loss detector becomes a permanent false alarm.
    "r.consumed_from_version=(CASE WHEN $cfv < 0 THEN r.consumed_from_version ELSE $cfv END), "
    "r.started_at=coalesce(r.started_at, $tm), r.events_count=coalesce(r.events_count, 0)+1 "
    "RETURN 1"
)
# Progress + outputs ride only some events (RUNNING carries progress; only the terminal event names
# the outputs), so they are SET in their own conditional statements — never clobbered back to null.
SET_RUN_PROGRESS: Final = "MATCH (r:Run {run_id:$rid}) SET r.progress_done=$pd, r.progress_total=$pt RETURN 1"
SET_RUN_OUTPUTS: Final = "MATCH (r:Run {run_id:$rid}) SET r.outputs=$outs RETURN 1"
# What a run has ALREADY recorded writing — the object `enforce_output_authz` authorizes a MUTATION
# of that run against. Empty (or no row) means the run does not exist yet, which is what keeps a
# START event able to open a run it cannot authorize.
RUN_OUTPUT_NAMES: Final = "MATCH (r:Run {run_id:$rid}) RETURN r.outputs"
LIST_RUNS: Final = (
    "MATCH (r:Run) RETURN r.run_id, r.job, r.author, r.event_type, r.progress_done, r.progress_total, "
    "r.error_message, r.started_at, r.event_time, r.events_count, r.outputs, r.operation, r.source_run_id, "
    "r.promotion_status, r.consumed_to_version, r.consumed_from_version"
)
# Discovery / browse — the "what exists?" lists. Like LIST_RUNS these fetch every node and are governed in
# Python, so a caller can browse the estate without already knowing an exact name.
#
# PAGINATION IS NOT UNIFORM, and this comment used to claim it was. Only `/datasets` takes offset/limit
# (`discovery.list_datasets`, capped at _MAX_LIMIT); `/runs`, `/jobs` and `/namespaces` take neither and
# return every row the FGA filter leaves. That is currently fine — the graph's node count is modest, and
# `/runs` measured 272 rows on the live estate 2026-08-23 — but it is a property of the data, not of the
# code, and nothing bounds it if the estate grows. Adding a bound to the other three is a wire-contract
# change and a decision; saying which of them have one is not. Tags ride the Dataset node as a comma-joined string (_tags_from splits them back).
LIST_DATASETS: Final = "MATCH (d:Dataset) RETURN d.name, d.namespace, d.tags"
# The full linked column inventory for /search (P1 Search tier 1, 2026-07-11) — HAS_COLUMN-scoped so
# only CURRENT inventory matches (pruned/overwritten columns don't resurrect via search).
LIST_ALL_COLUMNS: Final = "MATCH (:Dataset)-[:HAS_COLUMN]->(c:Column) RETURN c.dataset, c.field"
# One row per (job, written-dataset); d.name is null for a job that has only read (OPTIONAL MATCH keeps the
# job row). Folded into per-job output sets in Python — avoids parsing an agtype array from collect().
LIST_JOBS: Final = "MATCH (j:Job) OPTIONAL MATCH (j)<-[:OF_JOB]-(:Run)-[:WROTE]->(d:Dataset) RETURN j.namespace, j.name, d.name"

LINK_RUN_JOB: Final = "MATCH (r:Run {run_id:$rid}), (j:Job {namespace:$ns, name:$nm}) MERGE (r)-[:OF_JOB]->(j) RETURN 1"
MERGE_DATASET: Final = "MERGE (d:Dataset {name:$name}) SET d.namespace=$ns RETURN 1"
# Storage location is SET only when the event carries it; tags are UNIONed into the node's set (#49 —
# the property also holds human-curated governance tags, which a producer's facet must never clobber).
SET_DATASET_SRC: Final = "MATCH (d:Dataset {name:$name}) SET d.source_uri=$src RETURN 1"
# Terminal lifecycle (2026-07-11): dropped-ness is DERIVED AT READ TIME from run history — the most
# recent SUCCESSFUL run that wrote the dataset being a drop_table means "deliberately dropped", so
# the reconcile sweep skips it (absence on storage is the EXPECTED state, not storage loss — it
# previously WARNed missing_on_storage forever via the stale source_uri). Derivation instead of a
# mutable stamp is deliberate (review 2026-07-11): a stamped flag was last-DELIVERY-wins — a stale
# redelivered drop event after a recreate would re-stamp a LIVE dataset and silently remove it from
# the sweep. Run nodes MERGE idempotently on run_id, so ordering by their event_time at read time is
# redelivery-proof by construction. FAILed runs keep WROTE edges (producers() shows the attempt), so
# the event_type=COMPLETE filter is load-bearing: a failed drop asserts nothing.
DATASET_LAST_SUCCESS_OP: Final = (
    "MATCH (r:Run)-[:WROTE]->(d:Dataset {name:$name}) WHERE r.event_type = 'COMPLETE' RETURN r.operation, r.event_time ORDER BY r.event_time DESC LIMIT 1"
)
SET_DATASET_TAGS: Final = "MATCH (d:Dataset {name:$name}) SET d.tags=$tags RETURN 1"
# Governance metadata (#49) — human-curated tags + description on the Dataset node, with last-writer
# attribution per field family. Standalone MATCH…SET statements bind params fine on AGE 1.5.0 (only a
# post-MERGE SET drops them); tags stay the same comma-joined string the ingest path writes.
GET_DATASET_GOVERNANCE: Final = (
    "MATCH (d:Dataset {name:$name}) RETURN d.tags, d.description, d.tags_updated_by, d.tags_updated_at, d.description_updated_by, d.description_updated_at"
)
SET_GOVERNED_TAGS: Final = "MATCH (d:Dataset {name:$name}) SET d.tags=$tags, d.tags_updated_by=$by, d.tags_updated_at=$at RETURN 1"
SET_DESCRIPTION: Final = "MATCH (d:Dataset {name:$name}) SET d.description=$desc, d.description_updated_by=$by, d.description_updated_at=$at RETURN 1"
LINK_READ: Final = "MATCH (r:Run {run_id:$rid}), (d:Dataset {name:$name}) MERGE (r)-[:READ]->(d) RETURN 1"
# The READ edge carries the Lance version this run CONSUMED, when the producer pinned it (the Ray TRAIN
# job pins every feature — #115 D1). Same own-statement rule as SET_WROTE_VERSION below (AGE drops a
# $param in a SET that follows an edge MERGE in the same statement). Unpinned reads leave it absent.
SET_READ_VERSION: Final = "MATCH (r:Run {run_id:$rid})-[e:READ]->(d:Dataset {name:$name}) SET e.version=$ver RETURN 1"
# The WROTE edge carries the Lance dataset version this run produced (from the OpenLineage
# ``version`` facet), so two refinement passes over one table are distinguishable in producers().
LINK_WROTE: Final = "MATCH (r:Run {run_id:$rid}), (d:Dataset {name:$name}) MERGE (r)-[:WROTE]->(d) RETURN 1"
# AGE binds a ``$param`` in a standalone ``MATCH ... SET`` but silently drops one in a ``SET`` that
# follows ``MERGE`` on an edge in the *same* statement (verified on AGE 1.5.0/PG16), so the version
# is written in its own statement — mirroring how dataSource/tags are set on the Dataset node.
SET_WROTE_VERSION: Final = "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.version=$ver RETURN 1"
# Storage->graph reconciliation back-fill (B4) — a synthetic 'reconcile' run recording a Lance write whose
# lineage event was lost (the outbox gap). Idempotent (MERGE on the reconcile run id), so re-running the
# reconcile never duplicates; the WROTE version is stamped in its own statement (the AGE MERGE+SET quirk).
BACKFILL_RUN: Final = (
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
COUNT_OLD_RUNS: Final = "MATCH (r:Run) WHERE r.event_time < $cutoff RETURN count(r)"
# BATCHED (one transaction per batch): a single all-or-nothing DETACH DELETE over a large backlog
# would exceed the pool's statement_timeout → QueryCanceled → full rollback → retention never
# converges (each tick retries the identical oversized delete). Batches keep every statement far under
# the timeout and make partial progress durable tick over tick.
#
# AGE 1.5.0 does not bind SKIP/LIMIT as a param (every Cypher LIMIT in this file is a literal), so the
# batch size is interpolated into the query text at call time rather than passed through `params`. That
# makes PRUNE_BATCH_SIZE the SINGLE source for both the delete's LIMIT and the loop count in
# `prune_runs` — the two used to be a baked 500 literal and a separate Python constant that could drift.
# The interpolated value is a code-owned int constant, never caller input, so no injection surface.
PRUNE_OLD_RUNS_TEMPLATE: Final = "MATCH (r:Run) WHERE r.event_time < $cutoff WITH r LIMIT {limit} DETACH DELETE r"
PRUNE_BATCH_SIZE: Final = 500
# The per-version column schema rides the same WROTE edge as the version (#24 prerequisite). Stored as
# a JSON **string** scalar — params are JSON-encoded and ``_parse`` json.loads each cell, so a scalar
# round-trips cleanly; an array-in-SET is the risky path AGE 1.5.0 mishandles (same reason tags are a
# comma-joined string). Own statement, like the version (AGE drops a $param in a post-MERGE SET).
SET_WROTE_SCHEMA: Final = "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.schema=$schema RETURN 1"
# Runtime-measured output statistics ride the same WROTE edge (the rows + on-disk bytes the compute
# actually wrote, from the standard ``outputStatistics`` facet). Both are plain int scalars set in a
# standalone MATCH...SET (no MERGE-on-edge in this statement → AGE binds both $params, like SET_COL_EDGE).
SET_WROTE_STATS: Final = "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.row_count=$rows, w.size_bytes=$size RETURN 1"
# Quality-gate result rides the same WROTE edge: a ``quality_passed`` bool (the headline signal) + the
# full assertions as a JSON **string** scalar (same scalar-round-trips-cleanly reasoning as the schema).
# A passed=false edge with a real version is the auditable record of a batch the gate blocked.
SET_WROTE_QUALITY: Final = (
    "MATCH (r:Run {run_id:$rid})-[w:WROTE]->(d:Dataset {name:$name}) SET w.quality_passed=$passed, w.quality_assertions=$assertions RETURN 1"
)
DERIVED_FROM: Final = "MATCH (o:Dataset {name:$on}), (i:Dataset {name:$inp}) MERGE (o)-[:DERIVED_FROM]->(i) RETURN 1"

#: Widest hop count a caller may ask for. A bound this side of "the whole component" is the point of
#: the parameter, so an absurd number is refused rather than honoured — it is the unbounded walk
#: wearing a number, and the unbounded walk already has its own spelling (`depth=None`).
MAX_WALK_DEPTH: Final = 20


def bounded_walk(query: LiteralString, depth: object) -> LiteralString:
    """Bound every variable-length `DERIVED_FROM` hop in `query` to at most `depth` hops.

    `None` returns the query untouched — the unbounded walk, which is what the estate-wide read wants
    and is a deliberate answer rather than a missing bound.

    **Why the number is interpolated.** openCypher takes the hop range as SYNTAX (`*1..3`), not as a
    bind parameter: `*1..$depth` does not parse. So this formats it into the string, which is exactly
    the shape an injection takes — hence the coercion below happens BEFORE any formatting, and a value
    that is not a small positive integer is refused outright rather than clamped to something
    plausible. Clamping would run a query the caller never asked for and hide that they tried.

    `bool` is excluded explicitly: it is an `int` subclass in Python, and `True` would otherwise pass
    as depth 1.
    """
    if depth is None:
        return query
    if isinstance(depth, bool) or not isinstance(depth, int):
        raise TypeError(f"walk depth must be an int or None, got {type(depth).__name__}")
    if depth < 1 or depth > MAX_WALK_DEPTH:
        raise ValueError(f"walk depth must be between 1 and {MAX_WALK_DEPTH}, got {depth}")
    # EVERY hop, not the first: the rooted read runs an upstream and a downstream walk, and bounding
    # one would return a neighbourhood that is shallow in one direction and the whole component in the
    # other — which reads as a graph bug rather than as a missing bound.
    return cast("LiteralString", query.replace("*1..]", f"*1..{depth}]"))


UPSTREAM: Final = "MATCH (d:Dataset {name:$name})-[:DERIVED_FROM*1..]->(u:Dataset) RETURN DISTINCT u.name, u.namespace"
# One run's direct inputs + the version it PINNED on each (the READ-edge version — #115's reproducibility
# pin). Direct edges only (NOT the transitive DERIVED_FROM closure): "which versions did THIS run read"
# is a property of the run's own reads, not of the dataset ancestry where a version has no meaning.
RUN_INPUTS: Final = "MATCH (r:Run {run_id:$rid})-[e:READ]->(d:Dataset) RETURN DISTINCT d.name, e.version"
DOWNSTREAM: Final = "MATCH (d:Dataset {name:$name})<-[:DERIVED_FROM*1..]-(x:Dataset) RETURN DISTINCT x.name, x.namespace"
PRODUCERS: Final = (
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
LATEST_WRITE_VERSION: Final = (
    "MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:$name}) WHERE w.version IS NOT NULL RETURN w.version ORDER BY r.event_time DESC LIMIT 1"
)
SOURCE_URI: Final = "MATCH (d:Dataset {name:$name}) RETURN d.source_uri LIMIT 1"
# Per-version schema lookup (#24). Latest = the most-recent successful WROTE edge that carries a schema;
# at-version pins the edge whose version matches. Both return the schema JSON string + its version.
SCHEMA_LATEST: Final = (
    "MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:$name}) WHERE w.schema IS NOT NULL RETURN w.schema, w.version ORDER BY r.event_time DESC LIMIT 1"
)
SCHEMA_AT_VERSION: Final = (
    "MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:$name}) WHERE w.version=$ver AND w.schema IS NOT NULL "
    "RETURN w.schema, w.version ORDER BY r.event_time DESC LIMIT 1"
)
MERGE_USER: Final = "MERGE (u:User {name:$name}) RETURN 1"
# Latest-create-wins: the CREATED edge carries the create event_time so creator() is deterministic
# even when a table name is dropped+recreated by a different principal (the most recent create is
# authoritative). A re-create updates this principal; drop-lineage GC is future work.
LINK_CREATED: Final = "MATCH (u:User {name:$name}), (d:Dataset {name:$ds}) MERGE (u)-[c:CREATED]->(d) SET c.created_at=$tm RETURN 1"
CREATOR: Final = "MATCH (u:User)-[c:CREATED]->(d:Dataset {name:$name}) RETURN u.name ORDER BY c.created_at DESC LIMIT 1"

# AGE rejects zero-length variable paths (``*0..``), so the connected node set is
# assembled from the upstream + downstream traversals (``*1..``) plus the root itself,
# nodes are fetched in one shot (name set), and edges are filtered to that name set.
GRAPH_NODES: Final = "MATCH (d:Dataset) WHERE d.name IN $names RETURN d.name, d.namespace, d.source_uri, d.tags"
GRAPH_EDGES: Final = "MATCH (a:Dataset)-[:DERIVED_FROM]->(b:Dataset) WHERE a.name IN $names AND b.name IN $names RETURN DISTINCT a.name, b.name"
# The estate-wide variants: every dataset node / DERIVED_FROM edge in one read each, so the graph
# UI gets the whole picture in ONE request instead of recomposing it client-side from a
# per-dataset fan-out (which cost 2N+ HTTP calls per poll tick at N datasets).
ESTATE_NODES: Final = "MATCH (d:Dataset) RETURN d.name, d.namespace, d.source_uri, d.tags"
ESTATE_EDGES: Final = "MATCH (a:Dataset)-[:DERIVED_FROM]->(b:Dataset) RETURN DISTINCT a.name, b.name"
# Per-node write rollup for the estate read: written versions + any-failed, folded in Python
# (_fold_writes). Keeps the graph UI's node badges (versions, failed ring) at ONE request instead
# of a per-dataset /producers fan-out.
ESTATE_WRITES: Final = "MATCH (r:Run)-[w:WROTE]->(d:Dataset) RETURN d.name, w.version, r.event_type"
# The same rollup, scoped to a rooted neighbourhood. It exists because the badges must not depend on
# WHICH read the UI happens to use: a card sourced from the rooted graph and the same card sourced
# from the estate graph have to say the same thing, and an unscoped read here would fold the whole
# estate's writes to answer a question about a handful of datasets.
GRAPH_WRITES: Final = "MATCH (r:Run)-[w:WROTE]->(d:Dataset) WHERE d.name IN $names RETURN d.name, w.version, r.event_type"

# Column-level lineage (#24). A (:Column {dataset, field}) is MERGEd on the 2-tuple of SCALAR props
# (no concatenated id — dataset names contain '$', so any delimiter could collide). ``dataset`` is the
# owning :Dataset.name (also the governance handle, denormalised so a query never joins via HAS_COLUMN).
# The typed seed sets ``type`` from the schema facet; the stub (for an input column whose dataset isn't
# ingested yet) sets ONLY namespace — never ``type`` — so it can't clobber a real type with null.
MERGE_COLUMN: Final = "MERGE (c:Column {dataset:$ds, field:$fld}) SET c.namespace=$ns RETURN 1"
MERGE_COLUMN_TYPED: Final = "MERGE (c:Column {dataset:$ds, field:$fld}) SET c.namespace=$ns, c.type=$type RETURN 1"
LINK_HAS_COLUMN: Final = "MATCH (d:Dataset {name:$ds}),(c:Column {dataset:$ds, field:$fld}) MERGE (d)-[:HAS_COLUMN]->(c) RETURN 1"
# Column-inventory GC (2026-07-11): a schema facet is the COMPLETE current column set by contract, so
# after seeding it, HAS_COLUMN links to fields outside it are STALE inventory (an overwrite replaced
# the schema — {a,b}→{x,y} used to leave a,b listed forever). Only the LINK is deleted: the :Column
# node and its COL_DERIVED_FROM edges stay, so historical column lineage (and per-version schemas on
# WROTE) are untouched — this prunes what dataset_column_graph() presents as CURRENT.
UNLINK_STALE_COLUMNS: Final = "MATCH (d:Dataset {name:$ds})-[r:HAS_COLUMN]->(c:Column) WHERE NOT c.field IN $fields DELETE r RETURN 1"
# DISTINCT label (NOT the dataset-level DERIVED_FROM): AGE's *1.. constrains only path ENDPOINTS, not
# intermediate edge labels, so reusing DERIVED_FROM would let a column traversal silently cross onto the
# dataset plane if the two ever connect. Direction output→input, mirroring dataset DERIVED_FROM.
COL_DERIVED_FROM: Final = "MATCH (o:Column {dataset:$ods, field:$ofld}),(i:Column {dataset:$ids, field:$ifld}) MERGE (o)-[:DERIVED_FROM_COLUMN]->(i) RETURN 1"
# Edge props are SET in their own statement (AGE 1.5.0 drops a $param in a SET fused to a MERGE-on-edge).
# All scalars — masking is a plain bool; the multi-valued transformations[] is collapsed to type/subtype
# at parse time precisely to avoid an array-in-SET (the path AGE mishandles).
SET_COL_EDGE: Final = (
    "MATCH (o:Column {dataset:$ods, field:$ofld})-[e:DERIVED_FROM_COLUMN]->"
    "(i:Column {dataset:$ids, field:$ifld}) "
    "SET e.transformation_type=$tt, e.transformation_subtype=$st, e.masking=$mask, e.description=$desc, "
    "e.run_id=$rid, e.output_version=$ver RETURN 1"
)
COL_UPSTREAM: Final = (
    "MATCH (c:Column {dataset:$ds, field:$fld})-[:DERIVED_FROM_COLUMN*1..]->(u:Column) RETURN DISTINCT u.dataset, u.field, u.namespace, u.type"
)
COL_DOWNSTREAM: Final = (
    "MATCH (c:Column {dataset:$ds, field:$fld})<-[:DERIVED_FROM_COLUMN*1..]-(x:Column) RETURN DISTINCT x.dataset, x.field, x.namespace, x.type"
)
# Per-dataset column view: the dataset's OWN columns (complete typed inventory via HAS_COLUMN, incl.
# columns with no declared lineage) + every column edge touching the dataset (either endpoint).
# DISTINCT: :Column has no UNIQUE index (unlike Run/Dataset/Job — deliberately, since duplicate column
# vertices from a rare concurrent first-create are benign and an index would add abort/retry churn to the
# hot column path). Two concurrent ingests that first-touch the same (dataset, field) can each MATCH-miss
# and CREATE, leaving a duplicate :Column + duplicate HAS_COLUMN; DISTINCT collapses them so the inventory
# lists each field once regardless. The upstream/downstream column walks already RETURN DISTINCT.
DATASET_COLUMN_NODES: Final = "MATCH (d:Dataset {name:$ds})-[:HAS_COLUMN]->(c:Column) RETURN DISTINCT c.field, c.type ORDER BY c.field"
# The FRONTIER form, taking a list of datasets rather than one, so the column graph can be walked
# outward a table at a time. The frontier is a BIND PARAMETER — unlike the table-level walk, whose
# hop range is Cypher syntax and has to be interpolated, there is no string to sanitise here.
DATASET_COLUMN_EDGES: Final = (
    "MATCH (o:Column)-[e:DERIVED_FROM_COLUMN]->(i:Column) WHERE o.dataset IN $dss OR i.dataset IN $dss "
    "RETURN DISTINCT o.dataset, o.field, i.dataset, i.field, "
    "e.transformation_type, e.transformation_subtype, e.masking, e.description"
)

#: How many DATASET hops out from the root the column graph may be walked. Small on purpose: the
#: expansion is breadth-first over a connected estate, so each hop can multiply the payload, and the
#: view draws one container per table — past a handful of tables it stops being a graph you can read.
MAX_COLUMN_DEPTH: Final = 5
