# open-notifications — the notification plane: a user-targeted inbox, project watches, channel fan-out, and the demotion of the activity bell

Working plan, **2026-08-08**, against `HEAD 53440d3`. Unsettled work; this file is deleted when
it lands. `docs/` is for settled architecture only.

**Evidence convention.** Every claim carries one of three markers, and they are not
interchangeable:

- `path:line` — **read from source** this pass (directly or via the three targeted audit sweeps
  that fed this doc). Read, not executed.
- `(measured <date>)` — observed against a running cluster or browser. Claims of this class in
  this doc are inherited from `open_dapr.md` and `docs/DECISIONS.md` and say so.
- `UNVERIFIED` — an inference, an estimate, or a choice not yet exercised. Named inline, never
  buried.

**Structure.** One section per question, each **leading with the decision**. Every section names
its FGA doors explicitly. Cross-references to `open_dapr.md` §-numbers are to that file at the
same HEAD — several of its confirmed defects are *design inputs* here (they are the traps this
plane must not re-dig).

**The eight decisions in one table.**

| Question | Decision |
| --- | --- |
| **D1** — what is a notification | Three planes, kept apart: **ops alerting** (vmalert→Alertmanager, operators) untouched; **activity** (the `/runs` projection) kept but **off the badge**; **notifications** (targeted, per-subject, durable read-state) built new. Telemetry is never a transport. |
| **D2** — where it lives | A new deployable, **`services/notifications`**, app-id `notifications`. Not parked in `annotator` or `catalog` — the inbox is its own bounded context, and both would-be hosts' docstrings say authorization/state cohesion is per-domain. |
| **D3** — ingress | Subscribe `lineage.events.v1` + `catalog.control.v1` on **own per-app components** (`deliverPolicy: new` + durable), **plus** a `GET /events?after=<seq>` reconciler — the bus provably does not carry the ingest/HTTP lanes. |
| **D4** — targeting | v1 **authorship** (the run's verified `author` facet). v2 **project watch** (explicit, gated on `project#member`). v3 **governance events naming you** (`grant_added`/`grant_revoked` `extra.subject`). Never "everyone sees everything" on the badge again. |
| **D5** — the inbox | **One `InboxActor` per subject** on `lance-statestore` (single-activation is the lock — no etags needed), storing **pointers only** (claim-check), TTL backstop + compaction reminder. Closes `OPEN-WORK.md` B2. |
| **D6** — channels | Dapr **output bindings** (SMTP, Slack webhook), per-user prefs, delivery idempotent by `(event_id, subject, channel)`. **No Dapr Workflow** (the idempotency criterion holds); the **outbox** is documented as the future dual-component path, not adopted. |
| **D7** — FGA | No new object type. Watching requires `project#member`; every render/delivery re-checks `can_get_metadata` on the run's outputs — the same rule `governed()` already enforces on `/runs`. Refusal is a 403, never an empty 200. |
| **D8** — the bell | Badge = **inbox unread only**. The panel splits **Inbox / Activity**. The component's existing `onseen`/`ondismiss` seam (built for exactly this) gets its backend. |

---

## 0. Baseline — what exists at `HEAD 53440d3`, and what it is not

**The bell is an estate-wide activity projection, not a notification surface.** Every zone mounts
the shared `NotificationCenter` (`frontend/packages/ui/src/lib/shell/notification-center.svelte:15-27`),
fed per zone by a four-line `feeds.remote.ts` over the shared generator
(`frontend/packages/api/src/runs-feed.ts:4-22`); both halves are gated estate-wide by
`frontend/packages/zone-contract/src/notification-surface.test.ts:23-48`. It renders `GET /runs`
re-read on every lineage-cursor move — nothing is stored anywhere. Read/dismiss state is
**per-tab**, and the component says so: `seen`/`dismissed` are bindable with `onseen`/`ondismiss`
documented as "the persistence seam" (`notification-center.svelte:44-48`). `OPEN-WORK.md:72-81`
(B2) names the missing half: per-subject read state needs an actor. (B1's "no actor type is
registered" is **stale** — the annotator registers three, `services/annotator/src/annotator/main.py:91-93`.)

**The events invariant this plane must preserve** (`docs/DECISIONS.md:493-499`): an event is a
refresh hint, never authoritative data; consumers re-read through the FGA-governed path; emitters
swallow publish errors because the audit trail is the durable record. The notification plane
*adds* durable per-subject state without breaking that: what it stores are **pointers plus the
subject's relationship to them** (seen/dismissed), never payload copies.

**The ops plane is separate and stays separate.** vmalert evaluates proven rules against
GreptimeDB and hands firing alerts to Alertmanager (`chart/templates/alerting.yaml:6-13`);
`webhookUrl` → Slack/PagerDuty is the *operator* channel. The otel skill's rule (ra-skills
`skills/otel`): telemetry is not an application transport and not a user-facing feature. Both
planes may end in Slack; they share nothing else — different audience, different routing,
different loss semantics.

---

## 1. The service — a 13th fleet member (D2)

**Decision: `services/notifications`, app-id `notifications`, port `8850`.** The fleet already
runs sixteen app-ids (`open_dapr.md` §1.1) including single-purpose ones (`flows`, `maintenance`,
`search`); the estate's own naming lesson (`lance-ray`, "named for the branch it arrived on" —
`open_dapr.md` §1.3.2) says: bare name, equal to the service directory, from day one. Port 8850
follows `ingest` 8830 / `flows` 8840 (`services/gateway/src/gateway/__init__.py:129,132`).

Why not host the inbox actor in an existing app:

- The annotator's actor doctrine is per-domain on purpose — its actors hold *annotation* state and
  its module header pins "Authorization is NOT performed here; the HTTP layer checks FGA"
  (`services/annotator/src/annotator/projects/actor.py:1-24`). An inbox aggregates runs (lineage),
  governance (catalog) and later annotation events — it belongs to none of its producers.
- `lance-statestore` **cannot hot-reload** (`open_dapr.md` §2.21, measured 2026-08-07): any scope
  change is a coordinated rollout of every scoped app. That argues for touching the scope list
  **once** — adding `notifications` — rather than repeatedly widening an existing app's remit.

**Workspace + entrypoint contract** (the `rask-architecture` invariants):

- `services/notifications/pyproject.toml` — picked up by the `services/*` uv glob. The dependency
  MUST be `service-kit[governed]`, not bare `service-kit`: the bare form resolves locally but
  crash-loops in the image whose venv is `uv sync --package notifications` alone
  (`services/flows/pyproject.toml:6-19` records the trap).
- App factory via `make_service_app(title=, routers=, lifespan=)`
  (`packages/service-kit/src/service_kit/__init__.py:90-145`); actors registered **in the
  lifespan, never at import** (`services/annotator/src/annotator/main.py:79-100`), failure logged
  non-fatal so the read plane survives an actor-plane outage.
- `.docker/notifications.dockerfile` — `uv sync --frozen --package notifications` against the
  root lock, Dagger-built like every image.
- Root `pyproject.toml` `testpaths` gets `services/notifications/tests` — **omit it and the suite
  silently never runs** (`pyproject.toml:183-203`).

**Gateway row** (`rask-services-fleet`): copy the flows precedent — the prefix-tracking form so
public and upstream cannot drift (`services/gateway/src/gateway/__init__.py:156-164`):

```py
notifications = ("notifications", os.environ.get("RASK_NOTIFICATIONS_URL", "http://127.0.0.1:8850"))
...
(f"{prefix}/notifications", f"{prefix}/notifications", *notifications),
```

plus the three gateway test shapes (`services/gateway/tests/test_lance_routes.py:41-94`:
presence/ordering, rewrite table, env override — the mock transport must return
`stream=httpx.ByteStream(...)`, `:32-34`). Chart: `RASK_NOTIFICATIONS_URL` in
`chart/templates/configmap.yaml` (the comment at `:30-32` names the failure of omitting it — the
gateway falls back to itself) and a `services.notifications` block in `chart/values.yaml`;
`chart/templates/fleet.yaml:11-47` then renders the Deployment with **both** the Dapr annotations
and the injector webhook **label** — annotation-without-label silently yields no sidecar
(`chart/templates/_helpers.tpl:184-208`, gated by
`tests/unit/test_invariants.py::test_every_dapr_annotated_pod_carries_the_injector_webhook_label`).
Dev fleet: three `PORT_OFFSET`-aware edits in `scripts/dev-micro.sh:34-50,74-82`.

---

## 2. Ingress — two topics, one reconciler (D3)

**Decision: subscribe the bus for latency, reconcile the feed for completeness.** The bus alone
is provably insufficient: the **ingest service emits lineage over HTTP only and refuses the
topic** (`services/ingest/src/ingest/lineage.py:133-158`), as do Ray TRAIN and any external
OpenLineage producer at `POST /api/v1/lineage` — those lanes reach lineage's durable feed
(`public.lineage_events`, `services/lineage/src/lineage/services/repository.py:133-137`) but never
`lineage.events.v1`. The only place both paths converge is lineage's `GET /events`
(`services/lineage/src/lineage/api/v1/endpoints/runs.py:81-130`). So:

1. **Bus subscription — run lifecycle.** Append `notifications` to the `$subscribers` list at
   `chart/templates/dapr-component.yaml:102-108`, minting `lineage-pubsub-notifications` with
   `queueGroupName: notifications`. **`deliverPolicy: new` + `durableName:
   notifications-durable`** — non-negotiable: lineage's own `deliverPolicy: all` + ephemeral
   consumer replays the whole 168 h backlog on every pod restart (`open_dapr.md` §2.10,
   CONFIRMED); for lineage that rebuilds a graph, for notifications it would **re-notify a week
   of history on every rollout**. The durable-drift reconcile loop
   (`chart/templates/nats-stream-job.yaml:142-154`) covers the new durable automatically.
2. **Bus subscription — governance.** A **separate** `catalog-control-pubsub-notifications`
   component cloned from the `lance-ray` precedent (`chart/templates/dapr-component.yaml:68-84`:
   own queue group + durable) — never a new scope on the broadcast component, whose whole point
   is every-replica delivery (`:32-52`).
3. **The reconciler.** A `bindings.cron` component (the `maintenance` precedent,
   `chart/templates/maintenance.yaml:9-20`) ticks the service to poll
   `GET /events?after=<cursor>&summary=true` with the service-door headers
   (`dapr-api-token` + `x-lance-service-identity`,
   `services/lineage/src/lineage/api/security.py:156`), cursor persisted in the service's own
   state partition. This closes the HTTP-only lanes at ≤ tick latency and doubles as the
   catch-up path after a notifications outage. `UNVERIFIED`: tick period — start at `@every 30s`,
   tune against the feed's retention (20 000 rows, `repository.py:192`).

**Handler doctrine** (every subscription): `dapr.ext.fastapi.DaprApp` registered from `main`
after `app` exists (`services/catalog/src/catalog/api/dapr.py:13-15`); handler param typed
`event: dict[str, Any]` (an `Any` param becomes a query param → 422,
`services/medallion/src/medallion/api/events.py:49-51`); `Depends(require_dapr_token)` — and the
token proves **"arrived via Dapr", never "trusted caller"**, because the gateway itself invokes
through Dapr (`packages/service-kit/src/service_kit/governed/dapr_auth.py:33-45`); unwrap
`body["data"]`, validate-or-**DROP** (`docs/DATA-CONTRACT.md:154-160` — a raising handler poisons
the subscription); return `{"status": "SUCCESS" | "RETRY" | "DROP"}`.

**Dedupe** reuses lineage's natural keys: `(run_id, event_type)` for terminal states,
`(run_id, event_type, event_time)` otherwise (`repository.py:149,156-166`); control events dedupe
on `event_id` (`packages/service-kit/src/service_kit/control_events.py:87`). Correlation: the
graph `runId` is a derived uuid5 — the human-facing id is `run.facets.lance.run_id`
(surfaced as `RunStatus.source_run_id`, `services/lineage/src/lineage/schemas.py:352-383`); link
with the latter.

**DLQ:** own topic **`dlq.notifications`**, declared per-subscription
(`dead_letter_topic=`), parked by the shared route factory
(`services/medallion/src/medallion/api/dlq.py:27-51`), with the service's own app-id as the
label — the mover trap of a shared per-subTopic DLQ where two apps park and count each other
(`open_dapr.md` §2.11, CONFIRMED) is exactly what per-app naming avoids. The subject already
matches the `DLQ` stream's `dlq.>` binding (`chart/templates/nats-stream-job.yaml:103`) — **no
new NATS stream is needed anywhere in this plan.** Register the app in
`chart/templates/dapr-resiliency.yaml:46-52` or the subscription gets no sidecar retry/DLT.

Any publish this service ever does goes through the one wrapper
`service_kit.dapr_publish.publish_event` (claim-check guard at 900 KiB,
`packages/service-kit/src/service_kit/dapr_publish.py:38-70`), and every topic literal it
introduces is pinned in `tests/unit/test_invariants.py:444-458`.

---

## 3. Targeting and the FGA doors (D4, D7)

**Decision: three targeting sources, all resolvable to a subject list without new FGA types.**

| Source | Recipient derivation | FGA door |
| --- | --- | --- |
| **v1 — authorship** | `run.facets.author.sub` — verified: overwritten with the token sub on the HTTP door (`services/lineage/src/lineage/api/fga_deps.py:96-104`), catalog-stamped on the bus door (`services/lineage/src/lineage/services/consumer.py:17-19`) | none extra — you may always be told about your own run |
| **v2 — project watch** | the watch registry (§4), tenant from `run.facets.lance.project` | watch **create** requires `project:<t>#member` (create-on-parent doctrine, `services/annotator/src/annotator/api/v1/endpoints/projects.py:3-9,80-84`); watch **delivery** re-checks it |
| **v3 — governance** | `CatalogControlEvent.extra.subject` on `grant_added`/`grant_revoked` (`control_events.py:98-100`) | none extra — being named is the targeting |

**The visibility invariant — the load-bearing one.** The bus is **ungoverned**: a subscriber sees
every tenant's runs. Per-recipient visibility is therefore re-derived twice: at **delivery**
(before an inbox pointer is written or a channel fires) and at **render** (the panel resolves
pointers through the governed read path). The rule is the one `/runs` already enforces —
`can_get_metadata` on `table:<output>` batch-checked, dataset-less rows dropped
(`fga_deps.py:206-252`). Two consequences, stated rather than discovered later:

- **A "run started" notification cannot be sourced from `/runs` under FGA** — `governed()` drops
  runs with an empty output set (`fga_deps.py:246-247`), and START events typically name no
  outputs. v1 therefore notifies on **terminal states** (COMPLETE / FAIL / ABORT) and treats
  start-visibility as a v-later question. `UNVERIFIED` whether authorship alone should bypass the
  output check for the author's *own* runs — argument for: you started it, you know it exists;
  argument against: one rule everywhere is auditable. Default: one rule everywhere.
- A **revoked** user's stale inbox pointers degrade at render (the re-read 403s → the row is
  dropped), and the FGA outage behaviour is inherited fail-closed: enabled-but-unwired → 503,
  never permissive (`packages/service-kit/src/service_kit/governed/deps.py:109-125`).

**Auth on the service's own API**: `make_auth_deps` from `service_kit.governed.deps:71` — the
subject is `token.sub` with **deliberately no header fallback** (`deps.py:99-108`), the module
must not use `from __future__ import annotations` (`deps.py:20-35`,
`tests/unit/test_auth_deps_resolve.py`). Refusal is a 403 with a reason, never an empty 200
(`projects.py:141-146` doctrine). FGA additions land as `check:` cases in
`packages/service-kit/src/service_kit/governed/auth/model.fga.yaml` (run by the `ms-authz` CI
job, `.github/workflows/ci.yml:190-216`; `model.json` regenerated, drift-diffed).

---

## 4. The inbox — one actor per subject (D5)

**Decision: `InboxActor`, actor-id = base64url(subject), on `lance-statestore`; a
`WatchIndexActor` per project for fan-out.** This is `OPEN-WORK.md` B2's design, built where B1's
infrastructure already runs.

**Doctrines inherited, each with its anchor:**

- **Single-activation is the lock.** Turn-based concurrency replaces etags/OCC for unread counts
  (`services/annotator/src/annotator/projects/actor.py:1-24`). The etag machinery
  (Dapr state docs) stays the fallback only if a non-actor bulk path ever appears — the shared-
  document race it would prevent is exactly `open_dapr.md` §2.7.
- **Identity is derived, never accepted.** No subject in a path param or body; actor-id from the
  verified sub via `encode_subject` (base64url — Dapr reserves `||`,
  `packages/service-kit/src/service_kit/governed/user_state.py:124-153`); stored records carry
  their own `subject` as a second lock, mismatch → unreadable, **not** absent
  (`user_state.py:197-228`).
- **Authorization is NOT performed in the actor** — the HTTP layer checks FGA first; the actor
  stays testable without OpenFGA (`actor.py` header doctrine).
- **Reminder ordering:** arm the safety reminder **before** persisting the transition
  (`project_actor.py:252-255` states why); disarm **after** (§2.6's confirmed defect is the
  reverse order). The inbox's compaction reminder follows the same rule.
- **Wire names:** interface with `@actormethod(name=...)`, calls only through `typed_proxy`
  (`services/annotator/src/annotator/projects/proxies.py:26-38,88-98`) — the sweep test banning
  raw `ActorProxy.create` (`tests/unit/test_actor_proxy_names.py:78`) covers the new module
  automatically.
- **State partition split** "small + read every call" vs "large + read rarely"
  (`actor.py:54-55`): an `inbox-meta` key (unread count, cursor, prefs pointer) and an
  `inbox-rows` key (the pointer records). Every row set with **`ttlInSeconds`** (the Dapr state
  docs' standing rule for actor state) as the hard backstop, `UNVERIFIED` default 30 d; the
  compaction reminder trims read+dismissed rows and reconciles the count — belt and suspenders,
  so a lost reminder cannot mean immortal state (the invisible-lost-sweep class,
  `open_dapr.md` §2.20).

**What a pointer record is** (claim-check, never a payload copy):
`{notification_id, reason, object_id, source_run_id, event_seq?, occurred_at, seen, dismissed}`
where `notification_id` stays **`run_id@STATE`** — the id scheme the component already keys
seen/dismissed by (`notification-center.svelte:25-27`), so dismissing "started" still lets
"failed" through, unchanged.

**Fan-out shape:** the bus handler resolves audience = author ∪ watchers(project). Watchers come
from `WatchIndexActor(project)` — the `tenant_actor.py` index-actor shape (whole file, 54 lines),
registered synchronously by the watch endpoint, not best-effort. Per-subject watch/channel prefs
live on the subject's own actor.

**Chart cost, named:** `stateStore.scopes` gains `notifications`
(`chart/values.yaml:971-975`), and because the actor store cannot hot-reload
(`open_dapr.md` §2.21) that is **one coordinated rollout** of annotator, catalog, ingest, flows —
schedule it with the next such change (open_dapr open question 6 suggests riding the same window
as the `lance-ray` rename).

**Service API** (under `RASK_API_PREFIX`, all subject-derived, all through the gateway row):
`GET /inbox` · `POST /inbox/seen` · `POST /inbox/dismiss` · `GET|PUT /watches` ·
`GET|PUT /prefs`. The frontend wires the component's existing `onseen`/`ondismiss` seam to
these — the component itself barely changes (D8): badge = inbox unread; panel tabs
**Inbox / Activity**, the activity tab being today's `NotificationList` unchanged
(`notification-list.svelte:23` — "the piece a zone would reuse for a full-page feed").

---

## 5. Channels — bindings, not a second system (D6)

**Decision: Dapr output bindings, driven from the same inbox write path, idempotent by
`(event_id, subject, channel)`.** Subscribing the bell to Slack/SMTP is a per-user delivery
matrix on the same subscriptions — not a parallel notifier.

- Components: `bindings.smtp` and an HTTP binding for the Slack webhook, scoped to
  `notifications`; secrets through `lance-secrets` (add the app-id to `$secretScopes`,
  `chart/templates/dapr-component.yaml:189-201`) — never pod env (the `MEDIA_PUBLISH_CLIENT_SECRET`
  lesson, `open_dapr.md` §2.9).
- **Compose from the governed re-read, never from the bus payload.** The claim-check invariant
  crosses channel boundaries too: the message body is built from `/runs`-shaped state fetched
  through the FGA-checked path at send time.
- Idempotency: JetStream redelivery is at-least-once; the delivery key `(event_id, subject,
  channel)` recorded on the subject's actor makes the retry a no-op — the same token-keyed
  criterion every multi-step path here satisfies (`docs/OPERATORS.md:96-99, 121-124`). **No Dapr
  Workflow**: nothing mints an id mid-saga, so per the estate's own reopen-signal the engine is
  not earned — the ingest lane's adoption (`docs/OPERATORS.md:126-133`) is precedent that it
  *may* be, not that it must.
- Digest (email batching) rides an actor reminder, not a cron sweep.
- **The outbox, considered and deferred.** Dapr's outbox makes "state write + publish" one
  transaction, but (a) it cannot retrofit the existing emitters — catalog mutations do not go
  through Dapr state transactions, and their fail-open publish is a *ruling*
  (`docs/DECISIONS.md:493-496`), not a bug; (b) enabling it on `lance-statestore` means outbox
  metadata on the shared actor store (hot-reload rollout, §2.21). If delivery ever needs
  transactional coupling, the sanctioned path is the docs' **dual-component pattern**: a second
  `state.postgresql` component on the same database, scoped only to `notifications`, carrying
  `outboxPublishPubsub`/`outboxPublishTopic`. Recorded so the next reader does not re-derive it.

**GreptimeDB / OTel, answered:** this service emits RED metrics + traces via
`service_kit.setup_otel` like every fleet member; new vmalert rules alert **about** it
(`dlq.notifications` depth, delivery-failure rate) through the proven-to-fire pipeline
(`chart/alerting/rules.yml` + `rules_test.yml`, `make alert-rules-check`). Telemetry carries
zero notification content in either direction.

---

## 6. What this deliberately does not do

- **No change to the emitters.** Control/lineage emission stays fail-open publish-after-commit;
  the reconciler is the loss-absorber, not a harder emit path.
- **No notification content in any store but the subject's inbox pointers.** No shared
  notifications table, no per-tenant feed cache — the activity surface stays a projection.
- **No estate-wide auto-watch.** Membership gates watching; it does not imply it. The failure
  mode this avoids is the one this plane exists to end: a badge that counts other people's work.
- **No second render site, no forked bell.** The annotator forked the header once and drifted —
  the reason `@rask/ui` owns the component (`notification-surface.test.ts:9-17`). Inbox and
  Activity are tabs of the one shared component.

---

## 7. Test and verification plan — every layer, and what each one alone cannot see

The estate's recorded lesson (`OPEN-WORK.md:1563-1569`): the bell was "shared, tested and
shipped — in one zone out of four", green at every gate, caught only by a live two-user drive.
The plan therefore names, per layer, the claim it gates **and the claim it cannot**.

| Layer | Gates | Cannot see |
| --- | --- | --- |
| **Python unit** — `services/notifications/tests` (added to root `testpaths`); conftest per the flows doctrine (no module-scope env, delete `DAPR_GRPC_PORT`, `services/flows/tests/conftest.py:1-33`) | handler DROP/RETRY/SUCCESS matrix, dedupe keys, audience resolution, pointer TTL math, delivery idempotency key | anything about wiring |
| **Actor unit** — the `tests/unit/test_annotation_*_actor.py` pattern | inbox transitions, count-never-races-itself, reminder arm/disarm ordering, subject second-lock | placement, real sidecar |
| **Gateway tests** — the three `test_lance_routes.py` shapes | the row exists, ordered, rewrites, env-overridable | that the service answers |
| **Invariants** — `test_invariants.py` | topic literals pinned; injector label present; declared deps | semantics |
| **FGA** — `model.fga.yaml` `check:` cases (watch requires member; revoked ≠ visible) via the `ms-authz` job | the model's answers | that services *ask* the model — covered by the authz drive below |
| **zone-contract vitest** — extend `notification-surface.test.ts` with a third gate per zone: the inbox transport exists and calls the seam (`onseen=`/`ondismiss=` wired, not default) | estate-wide mounting, no forked feed | rendering, data |
| **Zone e2e (Playwright, mocked)** — a `mock-notifications.ts` upstream per the `mock-lineage.ts` model: real routes + a `__mock/*` control plane, state **per bearer**, unhandled → `502 {"error":"not mocked"}` (`frontend/microfrontends/lakehouse/e2e/lineage/mock-lineage.ts` doctrine); wired by **env on the dev server** (server-side reads are invisible to `page.route`) | badge counts inbox-only; seen survives a reload (the per-tab bug's regression test); dismiss of `started` ≠ dismiss of `failed`; Inbox/Activity tabs | real auth, real FGA, real bus |
| **`make e2e` smoke** | anonymous zones still 200 with zero pageerrors | everything governed |
| **Live drive** — extend `scripts/verify_all_zones_both_users.mjs` (its harness: real Dex login per user, panel-scoped `getByRole('dialog')` assertions, measured truncation `scripts/verify_all_zones_both_users.mjs:59-192`) | **the targeting claim itself**: alice's failed run increments **alice's** badge in every zone and **not bob's**; bob watching the project *does* get it; revoking bob's grant makes the row degrade; seen-state survives a fresh browser context (the B2 acceptance test) | — this is the top gate |
| **Channel verify** — `UNVERIFIED` tooling: a dev SMTP sink (e.g. Mailpit) in a `.docker` side-stack + a mock Slack webhook asserting exactly-one delivery per `(event, subject, channel)` across a forced redelivery | fan-out idempotency, prefs honoured | prod SMTP/Slack |
| **Alert rules** — new rules in `chart/alerting/rules.yml` with `rules_test.yml` synthetic-series proofs (`make alert-rules-check`) | the ops plane notices notification failures | live Alertmanager routing (the standing prod drill) |

Two drive-level assertions are named now because no lower layer can express them: **(1)** the
badge differs between two signed-in users looking at the same estate — the exact opposite of
today's measured behaviour, and the proof that targeting is real; **(2)** a `FAIL` run's error
text appears inside the *dialog role*, unread-marked for the author, absent for a non-watching
non-author.

---

## 8. What travels with the code — skills, docs, gates

Per `CLAUDE.md`: a skill claim that contradicts a file is fixed in the same commit.

- **`rask-services-fleet`** — port-map row (`notifications` 8850, `RASK_NOTIFICATIONS_URL`), the
  `/api/notifications` route row, dev-micro process list.
- **`rask-frontend`** — the bell's contract changes: badge = inbox, Inbox/Activity tabs, the
  seam is now wired (per-tab fallback remains for auth-off dev), the third zone-contract gate.
- **`rask-architecture`** — the new member appears in the deployables set (`gateway`, `compute`,
  `runner`, + `notifications`).
- **`docs/DATA-CONTRACT.md` §7.2** — the subscriber table gains the `notifications` rows;
  `dlq.notifications` documented beside its siblings.
- **`OPEN-WORK.md`** — B2 closes citing this file (and B1's stale "no actor is registered" line
  is corrected); this file gains the pointer until then.
- **`open_dapr.md`** — its open question 6 (the coordinated state-store rollout window) picks up
  the `stateStore.scopes` addition from §4 here.
- **`CLAUDE.md`** — the services list and the gateway route enumeration.

---

## 9. Slices — each independently shippable, reviewed, red-first

1. **S1 — the service + the inbox, author-targeting, bus ingress.** Skeleton (workspace member,
   dockerfile, chart, gateway row + tests, dev-micro), `lineage.events.v1` subscription
   (terminal states only), `InboxActor` + seen/dismiss endpoints, the frontend seam wired in one
   zone behind the existing component API. **Closes B2.** Includes the coordinated
   state-store-scope rollout.
2. **S2 — completeness.** The `/events` reconciler + cursor; dedupe proven across both ingress
   paths (the same event via bus and feed lands one pointer); `dlq.notifications` + resiliency
   registration.
3. **S3 — the honest bell, estate-wide.** Inbox/Activity tab split in `@rask/ui`; badge counts
   inbox only; all zones wired; zone-contract gate extended; the two-user live drive extended
   and passing.
4. **S4 — project watch.** `WatchIndexActor`, watch endpoints behind `project#member`, the
   settings surface in `home`, FGA `check:` cases, control-events ingress (v3 targeting).
5. **S5 — channels.** SMTP + Slack bindings, prefs, digest reminder, the channel verify rig,
   secret-store scoping.
6. **S6 — the ops seam.** vmalert rules + synthetic proofs; dashboards row; docs/skills sweep
   (§8) finalized.

---

## 10. Open questions

1. **Should authorship bypass the output-visibility check for the author's own terminal
   events?** (§3.) Default no — one rule everywhere — but the START-invisibility consequence
   makes "my run started" impossible under FGA until answered.
2. **Watch granularity below project?** The hierarchy has rungs (warehouse/namespace/table) and
   the FGA model inherits along them; v2 ships project-only to avoid building a subscription
   matrix nobody asked for. Reopen on the first real request.
3. **Retention numbers.** Inbox TTL 30 d, reconciler tick 30 s, digest window 24 h — all
   `UNVERIFIED` defaults to be measured against real volume in S2.
4. **The `notifications` app-id in `list_repos`-style enumerations** — none; but the
   `lance-ray` rename (open_dapr q6) and this plane's scope addition should share one rollout
   window. Owner call.
5. **Does the annotator eventually emit its own notification-worthy events** (review requested,
   task reassigned)? The design accepts them as a third topic with zero structural change —
   named here so the first implementer doesn't special-case it.
