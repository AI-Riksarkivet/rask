# Where interactive state belongs — and why `setInterval` is the symptom, not the disease

Owner's question, asked three times and answered thinly each time until now: *"how come KV, cache, state
management or actors — any of it from Dapr — is not being used for this stuff? Lance is about OLAP and
storage, so how come."*

The short answer: **they are right.** The analytical plane is coherent; the *interactive* plane has no home
for its state, and the frontend's 15 polling timers are what that absence looks like from the outside.

## The causal chain, measured

| Layer | What we found | Evidence |
| ----- | ------------- | -------- |
| Frontend | 15 files poll with `setInterval`; `query.live` used in exactly **one** file; **zero** `EventSource`; no client query cache | `grep -rl setInterval` per zone; `admin.remote.ts` |
| Media-plane services | viewer / search / annotator: `publish:0 subscribe:0`, zero Dapr imports — sidecar and tracing only | vs medallion 4/7, catalog 2/2, lineage 0/3, compaction 1/2 |
| Dapr building blocks in use | pub/sub only | no state store, no actors, no workflow component in `chart/` |
| Store for operational state | none — so there is nowhere to put a task's state except Lance, which is the wrong shape | — |

Read it downwards and it is one fault, not four: **the UI polls because there is no event to subscribe to;
there is no event because those services publish nothing; they publish nothing because there is no
operational state model to publish *about*.**

## Why Lance must not hold this state

Lance is a columnar, immutable, **versioned** analytical format. Every commit is a new version with a
manifest and a transaction file — which is exactly why the git-like history in `#113` works so well.

That same property makes it wrong for interactive state. A per-task flip (`assigned → in progress →
submitted → reviewed`) is a small, frequent, single-entity write. In Lance each one would be a dataset
version: hundreds of manifests a minute, a version history that is noise rather than provenance, and
read-modify-write contention between annotators on a format with no row-level locking. It is not a
limitation of Lance; it is a category error to ask an OLAP format for OLTP semantics.

## Which store, decided — two, each chosen for a property it has (owner-approved 2026-07-26)

The owner's constraints: *"I'm fine with adding redis as well for cache etc. As long as cloud native and
makes sense together. However still want to use jetstream for event driven workflows of course, due to its
complex and high performance."* Both are satisfied, and the split is not arbitrary.

| Purpose | Component | Why this one and not the other |
| ------- | --------- | ------------------------------ |
| Expensive shared reads, hot ephemeral state | **`state.redis`** with `ttlInSeconds`, `actorStateStore: false` | A cache must *forget*. TTL eviction and memory-bound behaviour are what Redis is actually best at, and `ttlInSeconds` is a first-class metadata field |
| Actors, workflow, durable domain state (annotation tasks, the notification inbox) | **`state.postgresql`** on the already-deployed `lance-ns-age-0`, `actorStateStore: "true"` | Durable workflow that loses its state on a pod restart is not durable. Postgres is durable by default and already backed up and monitored; a Redis without AOF configured in kind loses everything on restart. Stable actor support in v1 and v2 |
| The event backbone | **`pubsub.jetstream`** — unchanged | Already deployed and already correct, including the broadcast variant. No change at all |

Two state stores is idiomatic Dapr, not a compromise: components are named by purpose, and a service asks
for the one whose guarantees it needs. What would be wrong is one store pretending to be both — a cache
that must not lose data, or a durable store that must evict.

**On the Redis image:** Dapr's Redis state store is tested against **Valkey 8.x and 9.x**, so Valkey works
and is BSD-licensed rather than Redis Ltd's RSALv2/SSPL. The one caveat from the component reference: stock
Valkey images bundle neither RediSearch nor RedisJSON, so the **Query API** and the `queryIndexes` metadata
field will not work. Neither is needed here — a cache and a KV inbox use get/set/TTL, transactions and
ETag — so record the constraint and do not reach for the Query API later expecting it to be there.

**JetStream stays, and Dapr Workflow does not replace it.** These are different tools and it matters:
the medallion cascade (`medallion.raw` → `medallion.bronze` → …), control events and lineage delivery are
**event-driven fan-out**, which is exactly what JetStream is for and is already high-performance and proven
here. Dapr Workflow is **orchestration with queryable status** — a named instance, a step counter, retries
and compensation. It is additive, for the `#122` publish saga and export jobs where a user needs "step 3 of
7" and a failed step must roll back. Using workflow to replace the cascade would be a regression; using
pub/sub to report progress is what forced 15 polling timers.

## The shape that fits, per building block

- **Lance** — published, immutable, versioned analytical data. Unchanged, and correct as it is.
- **Dapr state store (KV)** — split as decided above: annotation project/task state, assignments, review
  states and feed cursors on Postgres; caches of expensive shared reads on Redis with a TTL. Small frequent
  reads and writes, no version per keystroke.
- **Dapr actors** — one actor per task (or per project): single-threaded per entity, so two annotators
  cannot claim the same task, and a progress counter needs no lock. Actor **reminders** give claim leases
  for free — a task claimed and abandoned returns to the queue without a sweeper cron.
- **Dapr workflow** — the *sync* in "synced only when we choose to" (`#122`) is a saga: freeze the project,
  write the governed table, emit lineage, tag the version, mark published. Durable, retried, resumable.
  Note the skill's constraint: workflow uses the actor framework internally, so it needs that state store
  with `actorStateStore: "true"` — the state store is a prerequisite, not an alternative.
- **Dapr pub/sub** — the change signal. It already exists in the medallion plane and is absent in the media
  plane, which is why the annotator can save a label and nothing downstream reacts.
- **SvelteKit `query.live`** — the UI's subscription to that signal. The docs are explicit: live queries
  "do not have a `refresh()` method, **as they are self-updating**". Mutations use `command`/`form`, which
  invalidate dependent queries and return the refreshed data **in the same round trip** (single-flight), so
  after your own write you wait for nothing.

## What this replaces

Every `setInterval` in the frontend, and the idea that annotation state could live in the governed plane.
Neither is a small cleanup: the timers are a workaround for a missing subscription, and the state question
decides whether `#122` is buildable at all.

## Three corrections this document needed (design fan-out, 2026-07-26)

Written before a measured design pass, the plan below had three faults. All three are corrected here, and
the corrections are cheaper than what they replace.

**1. Do not put Dapr sidecars on the four zone pods.** This document implied the zones needed to reach a
state store themselves. They do not, and a sidecar there is the most expensive item on the list: +127 MiB
measured (4 × ~31.7 MiB working set — 1.4× the annotator zone's own memory); the zone base path makes the
Dapr app channel unreachable, so `/dapr/subscribe` is a 404 on `web-media` and programmatic pub/sub would
fail *silently*; `annotator` is already a Dapr app-id (`chart/values.yaml:789`), a direct collision; and the
zones are readiness-probed by a TCP dial while every backend probes its own sidecar's health, so a zone with
a sidecar would serve traffic before Dapr enrolled, on every rollout. Meanwhile `catalog`, `lineage`,
`viewer`, `search` and `annotator` are **already 2/2**. Anything needing a store, an actor or a workflow
lives in one of those, and the zone BFF reaches it by HTTP proxy — which is its entire job already.

**2. The cache belongs in the browser, not in the BFF.** This is the correction that dissolves the tenancy
risk. A BFF cache of authorized reads needs a key derived from the subject, and getting that key wrong is a
cross-user leak rather than a slow page. An in-app memo needs no such key: it lives in the user's own tab, so
it is per-user by construction, immune to the `replicas: 2` coherence problem (there is no session affinity
on the Ingress), and it has no failure mode when a component is down. The biggest measured waste in the
estate is `/media/api/atlas/points` at **6,679,228 bytes on every mount and every Text/Visual toggle** —
25.5 MiB in ~30 s of ordinary clicking, which OOM-killed the viewer mid-measurement and took the media plane
to 502. That is `#121` with a cause attached, and the fix is a memo keyed on the `v=6` token already in the
URL. Version tokens and content hashes make invalidation **free**: a new build changes the key and the old
entry is unreachable.

**3. A per-user change feed already exists, and it is not the catalog's.** The claim that `query.live` was
blocked on event scoping is true of the **catalog** control feed only (`can_observe_events`, estate admin).
The **lineage** service's `GET /events` is already per-subject governed — an event is shown only if the
caller `can_get_metadata` on *every* dataset it references — and already has a keyset cursor (`after`). The
TypeScript client implements it (`packages/api/src/lineage/client.ts:117-126`) and **no caller passes it**.
The lineage plane is where 8 of the 13 lakehouse pollers live, so a non-admin's live refresh is available
today with no backend change and no Dapr: `admin.remote.ts` pointed at lineage instead of the catalog.

## Order of work

**Step 0, prerequisites and free wins — before any `query.live` expansion.**

- `nginx.ingress.kubernetes.io/proxy-read-timeout` on the ingress. Confirmed live: the running controller
  has `proxy_read_timeout 60s`, there is no override annotation, and SvelteKit's SSE transport emits **no
  keepalive** (kit 2.70.1, `runtime/server/remote.js:90` is the only `enqueue`, no timer anywhere in
  `runtime/server`). So an idle live feed is severed every 60 s, and each reconnect re-primes the whole
  200-event window and writes an audit record. Replicating `query.live` 15× without this would make the
  estate **slower while looking faster**.
- Pass `summary: true` on the lineage jobs page — measured **464,318 → 46,980 bytes**, 10×, on a page
  currently moving 528 MB/hour to render one job. The flag is already passed one file over.
- One health fetcher per zone in media. **Done** — `7f688d6`, with the invariant now enforced by a test.

**Then, in impact order.**

1. **The browser memo** for the atlas projection, content-addressed thumbnails and the descriptor. Highest
   user-visible gain per unit of work in the whole inventory, and the fix for `#121`. No infra, no chart, no
   backend change.
2. **Adopt what SvelteKit already ships**: data `load` functions (deployed, used by exactly one page), and
   `query` in place of the 13 hand-rolled poll/loading/401/offline triples — whose four-way drift is
   currently a correctness bug, since two lineage pages keep rendering governed rows after the session dies.
3. **One `query.live` per feed on the lineage cursor** (correction 3). Deletes the remaining timers and
   fixes the opposite failure at the same time: four admin surfaces make **zero requests, ever**.
4. **`form` + single-flight + `withOverride`** at the six mutation sites, removing the trailing `await
   load()` round trip after every write.
5. **The two state store components** (Redis for cache, Postgres with `actorStateStore: "true"` for actors
   and workflow), then `#122`'s task state on Postgres, then publish-on-save so there is an event rather
   than a poll, then workflow for the publish saga and `#125`'s notification inbox.

Note the reordering: the store moved from first to last. Steps 0–4 need no new component at all, and they
carry most of the measured user-visible gain. The store is required for `#122` and `#125` — durable task
state and a notification inbox genuinely cannot be built without it — but it was never the prerequisite for
making the existing surfaces feel responsive, which is what the polling timers were about.
