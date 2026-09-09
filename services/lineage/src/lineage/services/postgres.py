"""Everything the lineage service sends its Postgres as plain SQL rather than as Cypher.

The second of the two storage surfaces ``repository.py`` mixed (F-LIN-03). AGE runs INSIDE this same
Postgres, so the service talks to one server through two dialects, and it was not visible that it did:
the graph goes through ``cypher``/``core.age``, while the durable ``public.lineage_events`` feed, the
``public.lineage_reads`` audit log, the graph-existence probe, the vertex-index DDL and the cluster-wide
advisory lock are ordinary relational SQL with ``%s`` binds.

Two dialects, two failure modes: a Cypher statement fails inside AGE's function call and a broken graph
name is a parse error, while these are plain statements whose DDL half must stay idempotent because every
replica runs it at boot. Naming them apart is what makes that difference readable.
"""

from __future__ import annotations

from typing import Final


# Durable events feed — a plain table in the SAME Postgres that hosts AGE (qualified ``public.`` so it
# never lands in AGE's ``ag_catalog`` schema on the search path). Replaces the in-memory deque so /events
# survives restart + is replica-shared, mirroring the durable /runs fold. (#22)
CREATE_EVENTS_TABLE: Final = (
    "CREATE TABLE IF NOT EXISTS public.lineage_events ("
    "seq bigserial PRIMARY KEY, run_id text, event_type text, event_time text, "
    "job text, author text, inputs jsonb, outputs jsonb, event jsonb, "
    # received_at is the ESTATE'S clock, and that is the whole reason the column exists rather than the
    # retention reading event_time. `event_time` is a producer-supplied string this service never parses
    # (`RunEvent.event_time: str`), so a skewed or malformed stamp would either strand a row forever or
    # delete a fresh one. now() is ours, monotone with arrival, and indexed below for the prune.
    "received_at timestamptz NOT NULL DEFAULT now())"
)
# The same column on a table created before it existed. Postgres backfills the DEFAULT for existing rows
# as a metadata-only rewrite (PG 11+), so an already-populated feed gets a received_at equal to the
# moment of the upgrade — which is honest: those rows' true arrival time was never recorded, and dating
# them "now" keeps the first retention pass after an upgrade from deleting history it cannot date.
ADD_EVENTS_RECEIVED_AT: Final = "ALTER TABLE public.lineage_events ADD COLUMN IF NOT EXISTS received_at timestamptz NOT NULL DEFAULT now()"
# The prune's index. Without it a time-bounded DELETE seq-scans the whole feed on every retention pass.
CREATE_EVENTS_RECEIVED_AT_INDEX: Final = "CREATE INDEX IF NOT EXISTS lineage_events_received_at ON public.lineage_events (received_at)"
# A pre-existing table (created before this index) may already hold redelivered duplicates that would make
# CREATE UNIQUE INDEX fail — remove them first, keeping the earliest row (min seq) per natural key, so the
# index can always be established. NULL event_type/event_time never match (SQL NULL ≠ NULL), matching the
# unique index's NULLs-are-distinct semantics. Idempotent (a no-op once deduped).
DEDUP_EVENTS: Final = (
    "DELETE FROM public.lineage_events a USING public.lineage_events b "
    "WHERE a.seq > b.seq AND a.run_id = b.run_id "
    "AND a.event_type = b.event_type AND a.event_time = b.event_time"
)
# A natural key over the OpenLineage lifecycle identity — so an at-least-once REDELIVERY of the same event
# (Dapr re-drives after a lost ack) doesn't append a duplicate /events row. Idempotent on existing tables.
CREATE_EVENTS_INDEX: Final = "CREATE UNIQUE INDEX IF NOT EXISTS lineage_events_natural_key ON public.lineage_events (run_id, event_type, event_time)"
# The 3-col key alone can't dedup a REDELIVERED TERMINAL event: a RETRY-after-partial-success re-emits the
# same run's COMPLETE/FAIL with a FRESH eventTime, so the triple differs and a duplicate row lands. A run
# has at most ONE terminal state, so a partial unique on (run_id, event_type) for terminal types dedups
# them REGARDLESS of eventTime — while RUNNING events keep only the 3-col key, so their progress trail
# (many RUNNINGs at different times) is preserved. NULL event_type is excluded by the WHERE (NULL IN → not
# true), so it falls back to the 3-col key. The INSERT uses a TARGETLESS ON CONFLICT so it fires on EITHER.
TERMINAL_TYPES: Final = "('COMPLETE','FAIL','ABORT','RECONCILED')"
DEDUP_TERMINAL: Final = (
    "DELETE FROM public.lineage_events a USING public.lineage_events b "
    "WHERE a.seq > b.seq AND a.run_id = b.run_id AND a.event_type = b.event_type "
    # Justified: TERMINAL_TYPES is a module-level Final literal, not user input — no external
    # data reaches this f-string, so S608's injection premise does not apply.
    f"AND a.event_type IN {TERMINAL_TYPES}"  # noqa: S608
)
CREATE_TERMINAL_INDEX: Final = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS lineage_events_terminal_key ON public.lineage_events (run_id, event_type) WHERE event_type IN {TERMINAL_TYPES}"  # noqa: S608
)
# DECIDED (2026-07-10, §7a design item): the feed KEEPS THE FIRST terminal row per (run_id, event_type) —
# a re-executed run (same deterministic run_id, e.g. RETRY-after-trigger-failure re-emitting COMPLETE with
# a new version) updates the GRAPH views last-wins (/runs, /producers, the WROTE edge) but never rewrites
# its /events row. That asymmetry is the contract: /events is the append-only observation log ("what
# arrived first"), the graph is current state — upsert-latest here would let a redelivery silently rewrite
# audit history. Pinned by the feed e2e (test_events_feed_and_read_audit_against_postgres).
INSERT_EVENT: Final = (
    "INSERT INTO public.lineage_events "
    "(run_id, event_type, event_time, job, author, inputs, outputs, event) "
    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) "
    "ON CONFLICT DO NOTHING"
)
# The stored payload for one feed row, used ONLY to tell a REPLAY from a new assertion (§ E2). Reads
# the same `(run_id, event_type)` the terminal unique index covers, so it is an index lookup rather than
# a scan. `ORDER BY seq` + LIMIT 1 because non-terminal types carry no unique constraint and may hold
# several rows; the FIRST is the one the feed's own "keeps the first terminal row" contract preserves.
RECORDED_EVENT: Final = "SELECT event FROM public.lineage_events WHERE run_id = %s AND event_type = %s ORDER BY seq LIMIT 1"
LIST_EVENTS: Final = "SELECT seq, event_type, event_time, job, author, inputs, outputs, event FROM public.lineage_events ORDER BY seq DESC LIMIT %s"
# Keyset variant (§2 perf, 2026-07-11): `seq < %s` walks older pages off the PK index — NEVER OFFSET
# (OFFSET re-scans and re-drops every skipped row, so page N costs O(N·page)). The summary variants
# skip the full-JSONB `event` column entirely — the feed's hot poll path doesn't pay for payloads it
# won't render.
LIST_EVENTS_AFTER: Final = (
    "SELECT seq, event_type, event_time, job, author, inputs, outputs, event FROM public.lineage_events WHERE seq < %s ORDER BY seq DESC LIMIT %s"
)
LIST_EVENTS_SUMMARY: Final = "SELECT seq, event_type, event_time, job, author, inputs, outputs FROM public.lineage_events ORDER BY seq DESC LIMIT %s"
LIST_EVENTS_SUMMARY_AFTER: Final = (
    "SELECT seq, event_type, event_time, job, author, inputs, outputs FROM public.lineage_events WHERE seq < %s ORDER BY seq DESC LIMIT %s"
)
# Retention prune — drop rows the estate RECEIVED longer ago than the configured window.
#
# TIME IS THE UNIT BECAUSE IT IS THE ONE THE CONSUMERS REASON IN. The notifications reconciler walks
# this feed from a persisted cursor as the estate's catch-up path after an outage, so the only question
# a retention knob has to answer is *how long an outage does the feed survive*.
#
# NEITHER A ROW COUNT NOR A SEQ WINDOW CAN ANSWER IT, and the seq window is the trap worth naming:
# `INSERT_EVENT` is ON CONFLICT DO NOTHING and Postgres consumes the sequence value BEFORE the conflict
# check, so a redelivery that leaves no row still advances `seq`. Measured on the deployed feed
# 2026-09-08 under a 20 000-seq bound: 101229…121228 held **3 133 rows**, 84% of the window spent on
# duplicates — a horizon that contracts precisely when redelivery rises, which is when a consumer is
# most likely to be behind. `notifications_feed_gaps_total` recorded 559 reconciler passes on
# 2026-08-29 and 1 537 on 2026-08-30 that found the feed pruned below their cursor.
#
# BATCHED for the same reason the graph's `PRUNE_OLD_RUNS_TEMPLATE` is: an all-or-nothing DELETE over a
# window enabled late or shortened would exceed the pool's statement_timeout, roll back, and never
# converge — each tick retrying the identical oversized delete.
PRUNE_EVENTS: Final = (
    "DELETE FROM public.lineage_events WHERE seq IN "
    "(SELECT seq FROM public.lineage_events WHERE received_at < now() - make_interval(days => %s) ORDER BY seq LIMIT %s)"
)
# The FLOOR the prune above leaves behind — the oldest row the feed can still serve.
#
# It exists because the prune knows about no consumer: a reader whose cursor has fallen below this
# number lost rows before it read them, and walking to the end of the feed would otherwise look exactly
# like being caught up. Lineage cannot detect that itself — the notifications
# reconciler's cursor lives in ITS Dapr state store, which lineage is not scoped to and must not be —
# so lineage publishes the floor and each consumer draws its own conclusion.
#
# MIN(seq) rides the primary-key index, so this is a cheap read even on a full retention window.
OLDEST_EVENT_SEQ: Final = "SELECT MIN(seq) FROM public.lineage_events"
# Read/access audit (#6) — a plain append log of WHO read WHICH dataset (public, like lineage_events; the
# write provenance lives in the AGE graph, this is the complementary read log).
CREATE_READS_TABLE: Final = (
    "CREATE TABLE IF NOT EXISTS public.lineage_reads ("
    "seq bigserial PRIMARY KEY, reader text NOT NULL, dataset text NOT NULL, "
    "read_at timestamptz NOT NULL DEFAULT now())"
)
INSERT_READ: Final = "INSERT INTO public.lineage_reads (reader, dataset) VALUES (%s, %s)"
# The read-audit QUERY (the #41 log was capture-only): who read a dataset, aggregated per principal with
# their last-read time + count, most-recent first. GROUP BY collapses the append log's repeat rows.
READERS: Final = (
    "SELECT reader, MAX(read_at) AS last_read, COUNT(*) AS reads FROM public.lineage_reads WHERE dataset = %s GROUP BY reader ORDER BY last_read DESC LIMIT %s"
)
# Does the AGE graph exist? ag_catalog.ag_graph is AGE's registry of graphs. Used to make create_graph
# idempotent + concurrency-safe (create_graph ERRORS if the graph already exists).
GRAPH_EXISTS: Final = "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s"

# Reconcile single-flight (B4 hardening) — a fixed session-level advisory-lock id. The cron reconcile fires
# on EVERY lineage replica's sidecar independently, and a sweep back-fills the graph; two overlapping sweeps
# would double-drive the same back-fill. pg_try_advisory_lock on this id serializes them CLUSTER-wide
# (Postgres is the one shared coordinator) without pinning replicas=1. Arbitrary stable bigint constant.
RECONCILE_LOCK_KEY: Final = 0x1A9CE_5EED

# Vertex-uniqueness (B4 hardening) — each AGE vertex label + the property key(s) its MERGE keys on. A UNIQUE
# index over those keys makes AGE's MATCH-then-CREATE MERGE safe under CONCURRENCY: two txns (a reconcile
# racing a live ingest, or two sweeps) that both miss and both CREATE would otherwise leave a DUPLICATE
# vertex — the index makes the loser's insert fail instead. Keys mirror the ``cypher`` module's ``MERGE_*`` statements.
VERTEX_UNIQUE_KEYS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("Run", ("run_id",)),
    ("Dataset", ("name",)),
    ("Job", ("namespace", "name")),
)

# Plain (NON-unique) lookup indexes — labels whose MERGE key needs index-speed MATCHes but must NOT get a
# uniqueness constraint. :Column keeps its deliberate no-unique-index design (duplicate vertices from a
# rare concurrent first-create are benign and collapsed by DISTINCT reads; a unique index would add
# abort/retry churn + a lock-ordering obligation to the hot column path — see the DATASET_COLUMN_NODES
# comment). Without ANY index though, every column MERGE seq-scans a label table that grows with the
# estate (§4) — this closes the perf half while preserving the concurrency semantics.
VERTEX_LOOKUP_KEYS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("Column", ("dataset", "field")),
    # `Run.event_time` is what the run queries SORT by — six `ORDER BY r.event_time DESC` sites in
    # `cypher.py`, the runs board among them — and without an index each one reads and sorts the whole
    # label table. Measured on the deployed graph 2026-09-07: 5,586 Run nodes, 4,176 kB, carrying only
    # `lineage_run_uniq` (the MERGE key).
    #
    # NOTHING BOUNDS THAT TABLE: `runRetentionDays` ships 0 and the deployed service carries
    # `LINEAGE_RUN_RETENTION_DAYS=0`, so the pruning mechanism exists and is off. And BOUNDING THE
    # RESPONSE DID NOT BOUND THE WORK — `/runs` was capped at 200 rows the same day, which limits what
    # crosses the wire while the ORDER BY still reads every row behind it.
    #
    # NON-UNIQUE, necessarily: many runs share an `event_time`, and it is not part of the MERGE key.
    ("Run", ("event_time",)),
)
