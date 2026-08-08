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

**The nine decisions in one table.**

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
| **D9** — code quality | The implementation is **bound to four skills, read in full for this spec**: `writing-python`, `fastapi`, `python-infrastructure` (the Dapr/NATS/OTel doctrine), and `openfga` (all from ra-skills / vendored). §10 distills the rules that bind this service and pins precedence where a generic skill default conflicts with a repo pin. Review happens against §10, not against memory. |

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

## 10. Code-quality contract — the four binding skills (D9)

The four skills were read **in full** (every reference file) against this design on 2026-08-08:
`writing-python` + `python-infrastructure` + `fastapi` from `AI-Riksarkivet/ra-skills@main`, and the
vendored `openfga` skill (`.claude/skills/openfga`). What follows is not a summary to be re-derived —
it is the distilled subset that **binds this implementation**, plus the precedence calls. A reviewer
checks the diff against this section and the skills' own anti-pattern checklists
(`writing-python/references/anti-patterns.md` § Quick review checklist,
`fastapi/references/anti-patterns.md` § The table).

### 10.0 Precedence — where a skill default and a repo pin disagree, the repo wins

The skills are generic house style; `CLAUDE.md` + the root `pyproject.toml` are this repo's law.
Stated here so nobody "fixes" the repo toward the skill:

| Skill default | Repo pin that wins | Where pinned |
| --- | --- | --- |
| ruff `line-length = 120`, `target-version = "py314"` | **160**, Python **3.13**, `ANN` family selected | root `pyproject.toml`; `CLAUDE.md` § Conventions |
| Redis for cache / dedup windows / rate limits | **No Redis exists in the estate.** Hot-state = the Dapr state store; dedup = natural keys + JetStream duplicate window; single-flight = actor turn-based concurrency | `CLAUDE.md` § Architecture ("No Redis"); `OPEN-WORK.md:2243-2249` |
| `fastapi dev` CLI, hand-rolled CORS/middleware/handlers | **`make_service_app`** owns middleware, exception handlers, slash-tolerance, OTel, the Dapr client; services run under uvicorn via `dev-micro.sh` / the chart | `packages/service-kit/src/service_kit/__init__.py:90-145` |
| `microservices.md` "use Dapr ONLY for Workflow + pubsub; secrets/state stay native" | The estate deliberately uses Dapr **actors, state, and the secret store** — audited and upheld in `open_dapr.md` §1.4 | `open_dapr.md`; `chart/templates/dapr-statestore.yaml` |
| "duplicate shared types across services" | `service_kit` sharing **one** event model for producer and consumers is a deliberate estate override (`control_events.py` docstring: "producers and consumers import ONE model") — it is a contracts-only lib, which is the exception the skill itself allows | `packages/service-kit/src/service_kit/control_events.py:10` |
| SQLModel/Alembic/database.md, websockets, file-handling | **N/A** — this service has no SQL database, no websockets, no file surface | §4 (state = actor partitions) |
| `tests/` layout with `integration` marker | Repo markers/testpaths are law (`testing-python` owns wiring); add `services/notifications/tests` to `testpaths` | root `pyproject.toml:183-203` |

### 10.1 `writing-python` — the language contract

- **Pydantic everywhere, `@dataclass` banned.** Every wire/config/value shape — the inbox pointer
  record, watch prefs, handler payload models — is a `BaseModel`; settings are `pydantic-settings`
  (fail-fast at boot, `SecretStr` for anything secret, no bare `os.getenv` sprinkled through code).
  Frozen models (`model_config = {"frozen": True}`) for records that cross the actor boundary.
- **Types on every public signature**, modern syntax only: `X | None`, PEP 695 generics/`type`
  aliases, `Self`, `@override` on the actor-interface overrides, `**P, R` on any decorator.
  `Protocol` only where 2+ implementations exist (the delivery channel seam qualifies: SMTP + Slack
  + a test fake; the inbox repository does not — the actor IS the implementation).
- **Design defaults:** functions and modules first; classes only for state; no Java-shape patterns —
  channel dispatch is a **dict of callables**, not a Strategy hierarchy; rule of three before any
  abstraction; guard clauses; ≤3 positional args, else a model; **no boolean flag params** (split
  `send_digest()` from `send_immediate()`); names describe side effects (`get_` is pure;
  `record_`/`mark_` mutate).
- **Errors:** validate at boundaries; domain exceptions with structured context, chained `from e`;
  the fan-out is **batch-with-partial-failure** (`BatchResult`-shaped: one bad recipient never
  aborts the audience — the exact rule `anti-patterns.md` § Ignored partial failures pins).
  Never `except Exception: pass`; the one sanctioned broad-catch is the estate's own fail-open
  emit wrapper pattern (`control_emit.py:79-84`), which logs + counts.
- **Comments:** few, why-only. No restating, no metadata, no commented-out code, no
  docstring-per-private-helper. The estate's dense docstrings (§0-§5 quotes) are *why*-comments —
  that register, not narration.
- **Testing:** F.I.R.S.T.; boundary conditions by name (empty inbox, exactly-`limit` rows, one
  over, unknown subject); **cluster tests near any bug found** (T6); one concept per test,
  parametrized; **`respx` for every httpx seam** — never `@patch` a client method; no
  `@pytest.mark.skip` without a concrete unblock condition; conftest per the flows doctrine
  (function-scoped `MonkeyPatch.context()`, never module-scope env writes).

### 10.2 `python-infrastructure` — the reliability contract

- **One retry layer, and here it is the sidecar.** The Dapr resiliency policy
  (`chart/templates/dapr-resiliency.yaml`) owns redelivery for subscriptions — handlers **must not**
  wrap themselves in tenacity (the skill's "double retry" anti-pattern, which here would multiply
  4 sidecar retries × N app retries). Tenacity (exponential + jitter, transient-only:
  never `ValueError`, never 4xx-except-429) is reserved for the service's **own egress** — the
  `/events` reconciler poll and channel sends — where no sidecar policy applies.
- **Idempotency over exactly-once.** Delivery key `(event_id, subject, channel)` checked
  before send (check-before-write on the subject's actor); ingest dedupe on lineage's natural keys
  (§2). The JetStream `duplicate_window` is 2 min (`nats-stream-job.yaml:58`) — never assumed to
  cover a pod restart (the `open_dapr.md` §3 `publish_units` lesson).
- **Handler discipline** (the skill's worker rules mapped onto Dapr ingress): permanent failure
  (unparseable payload) → **DROP** with a log, never RETRY (poisons the subscription —
  `DATA-CONTRACT.md:154-160` says the same); transient → RETRY and let the sidecar back off;
  exhaustion → the DLQ parks it with `record_dead_letter`. Every outbound call carries an explicit
  timeout **below** the effective redelivery window.
- **Every job/digest has a hard timeout** (`asyncio.timeout`) so a hung send fails before the next
  reminder tick stacks on top of it.
- **OTel:** `service_kit.setup_otel` owns the SDK — no hand-rolled providers, no hard-coded OTLP
  endpoints, no SDK-side sampling. Four golden signals per boundary (subscription handler, reconciler,
  channel egress). **Bounded cardinality is the security rule here:** the *subject* is
  per-user data — it goes on spans/logs only, **never** on a metric label; delivery metrics label by
  `{channel, outcome}` only. No `span.record_exception` (deprecated) — `log.exception` inside the
  active span. Manual spans wrap business operations (`inbox.fanout`, `channel.send`), never whole
  routes.
- **Dapr Workflow determinism rules** (`dapr-workflows.md` § Critical rules) are recorded as the
  bar **if** D6's no-engine call is ever reopened; until then they bind nothing here.

### 10.3 `fastapi` — the HTTP contract

- **Route style, non-negotiable:** `Annotated` for every param and dep (aliased `XxxDep`, matching
  the estate's `CheckerDep`/`CurrentSubject` register); no `...`; no `RootModel`; a **return type on
  every route** (that is the serialization *and* the sensitive-field filter — an inbox row model
  can never leak another subject's fields it doesn't declare); `response_model=` only when it
  differs from the return type, never both for the same class; `prefix`/`tags` on the router;
  **one HTTP operation per function**; `StrEnum` for constrained query values (the inbox filter:
  `state: unread|all`).
- **`async def` only when genuinely async.** Everything this service awaits (Dapr client, httpx,
  actor proxies) is async, so routes are `async def` — but any sync SDK call that sneaks in
  (e.g. a sync workflow client, per `dapr-workflows.md`) goes through `asyncify`, never inline.
- **Lifespan owns every client** — Dapr client, httpx, the FGA client, actor registration — built
  once onto `app.state`, disposed in reverse order after `yield`; routes reach them only through
  dep wrappers. `make_service_app`'s injectable lifespan is the estate's implementation of this
  rule; the service adds its own lifespan pieces (actor registration **in the lifespan, never at
  import** — both the skill and `annotator/main.py:79-100` pin it).
- **Errors:** routes raise **domain exceptions**; handlers (service-kit's) shape the response —
  RFC 9457 `application/problem+json`, internals never in the body, `log.exception` in the handler
  inside the active span. No route-level `except Exception`. Refusal is 403-with-reason (§3).
- **Health:** `/livez` touches no dependency; `/readyz` reports per-component
  (state store reachable, sidecar up) with **three states** — degraded is 200-plus-flag, not 503;
  `startup_complete`/`shutting_down` flags from the lifespan. Excluded from tracing
  (`excluded_urls="/livez,/readyz"`).
- **Pagination:** the inbox list is a **feed → cursor pagination** (the skill's own decision
  table), opaque base64 cursor over `(occurred_at, notification_id)` with `limit+1` has-more
  detection, `le=` cap on limit, deterministic tiebreaker. No offset, no count queries.
- **Authz wiring** (`authz.md`): permission deps at the route/router level for single-object ops;
  **`batch_check` for list filtering** (one round-trip over the candidate set — exactly the
  `governed()` shape lineage already uses); `list_objects`/`list_users` for the audit surfaces if
  ever exposed. The `require_permission` dep reads `request.path_params` — **never `**path_params`
  in a dep signature** (becomes a required query param, 422s every call). Tuple writes, if any,
  through the single estate path (`service_kit.governed.fga`), never sprinkled in routes.
- **Anti-pattern table sweep before every merge** — the ones most likely here: `BackgroundTasks`
  for anything durable (banned — the bus and reminders exist), per-request `httpx.AsyncClient`,
  module-level clients, middleware error handlers, `ContextVar` set without `finally`-reset.

### 10.4 `openfga` — the authorization contract

The estate model (`packages/service-kit/src/service_kit/governed/auth/model.fga`) already follows
this skill's shape — concentric rungs, `member from project` chaining, conditions for time-boxed
grants. This plane's rules for touching it:

- **v1/v2 add no type and no relation** — the watch gate is the existing `project#member`, checked
  at watch-create (create-on-parent doctrine: the check runs against the *project*, the object that
  exists) and re-checked at delivery/render via `can_get_metadata` batch checks. The watch registry
  is **application state, not tuples** — per `core-separation.md`, tuples are authorization facts;
  "alice wants Slack pings about project X" is a preference, not a permission, and modeling it as
  tuples would put per-user mutable app state on the authz hot path.
- **If a relation is ever added** (e.g. an explicit `can_watch`): `can_*` relations **never take
  direct assignments** — name a role, reference it; concentric ordering, most restrictive first,
  each role appearing exactly once; parent links only on the top-level type with roles chained
  through computed relations (`org_admin` naming pattern); precise type restrictions (no kitchen-sink
  `[user, team, team#member, …]`); snake_case, `can_` prefix, singular lowercase types.
- **Testing is non-negotiable** (`workflow-validate.md`: "an untested authorization model may grant
  access to users who shouldn't have it"): any model or fixture change lands with `.fga.yaml`
  `check` cases (positive **and** negative **and** boundary — unknown user, unknown object),
  plus `list_objects`/`list_users` where the plane relies on enumeration; `fga model test` green
  and `model.json` regenerated + drift-diffed before delivery (the `ms-authz` CI job,
  `.github/workflows/ci.yml:190-216`, enforces exactly this).
- **SDK usage** goes through the estate wrapper (`service_kit.governed.fga` — `check`,
  `batch_check`, `write_tuples`, `grant_on_create`), which already encodes the skill's best
  practices (async client, retries, one client per process in the lifespan). Prefer `batch_check`
  over N `check`s for audience filtering; the three-outcome checker rule stands: FGA on + client
  unwired → **503, never permissive** (`deps.py:109-125`).

### 10.5 `diagrid-labs/dapr-skills` — the workflow review rules, and what transfers here

Read in full 2026-08-08 (github.com/diagrid-labs/dapr-skills): three **static review skills** with
stable rule IDs — `review-workflow-determinism` (`DWF-DET-001..015`), `review-workflow-activity`
(`DWF-ACT-001..011`), `review-workflow-management` (`DWF-MGT-001..015`) — plus a shared reference
library (core/python/ops) and three `create-workflow-*` scaffolding generators. The estate
**already uses the rule IDs**: `open_dapr.md` §1.4 verified `flows/workflow.py` "clause by clause
against DWF-DET-001..015". Three findings for this plane:

**(a) What binds `services/notifications` directly — the ACT rules generalize to every
at-least-once callback.** Our pub/sub handlers and actor reminder callbacks have exactly the
activity execution model (at-least-once, retried by the runtime), so the checklist maps:
ACT-002 (external calls carry an idempotency key — our `(event_id, subject, channel)`),
ACT-003 (never `except: pass` — our DROP is explicit, logged and counted),
ACT-004 (payloads are pointers with a size discipline — the claim-check invariant),
ACT-007 (no module-level mutable state in handler scope), ACT-009 (Pydantic-typed I/O).
The ops references add two **concrete S1 checks** that were missing from this spec:
- **`ActorStateTTL` must be enabled in the Dapr `Configuration`** for D5's `ttlInSeconds`
  backstop to function at all — verify against the chart's `lance-tracing`/Dapr Configuration
  before relying on it (open question 6).
- Actor runtime tuning knobs (`actorIdleTimeout`, `actorScanInterval`,
  `drainOngoingCallTimeout`, `drainRebalancedActors`) are the levers for inbox-actor
  memory-vs-reactivation cost — defaults are fine for S1; recorded so scaling work knows where
  the dials are.
The ops guidance also **confirms two decisions**: `state.postgresql` satisfies the three actor
state-store requirements (ETag, multi-item transactions, first-write-wins), and "use a dedicated
state store component per concern" is the documented best practice behind §5's dual-component
outbox path.

**(b) What the estate should adopt beyond this plane — the review skills as gates.** Running the
three review skills against `services/ingest` + `services/flows` would formalize findings
`open_dapr.md` already made by hand: the unauthenticated `POST /flows/runs` **is** DWF-MGT-010;
the missing terminate/purge surface (§2.23) **is** DWF-MGT-003/012; "nothing collects workflow
history" (open_dapr q7) has its concrete answer in `ops/troubleshooting-ops.md` § State Cleanup
(purge only terminal instances, batch purge on a retention window — the direct-SQL variant is
the last resort, it bypasses Dapr's key management). The versioning reference
(`core/versioning.md`) supplies what the estate has no ruling on yet: **never deploy a
command-sequence-changing workflow edit while instances are in flight** — blue/green drain or a
new workflow type are the two strategies available to Python SDKs (named versioning and
`IsPatched` are Go-only today). Recommended: wire the three review skills into the estate's
review flow for any diff touching `@wfr.workflow`/`@wfr.activity` code — proposed as open
question 7, since installing skills is an owner call.

**(c) What does not transfer.** The `create-workflow-*` scaffolds conflict with the estate's
layout (`make_service_app`, uv workspace, chart-owned components — no `dapr init`, no per-project
`components/`); the monitoring reference's `runtime/workflow/*` sidecar metrics are Prometheus-
scrape-shaped where the estate is OTLP→GreptimeDB (useful as a menu of what the sidecar can
expose, not as wiring); and its documented gap — **workflow activities get no propagated trace
context** (dapr/dapr#6950) — is an expectation-setter for ingest/flows traces, not something to
"fix" locally.

### 10.6 How this section is enforced

- The **review gate for every slice** (S1–S6) includes a pass over §10.0's precedence table and the
  two anti-pattern checklists cited at the top of this section.
- `make check` (ruff + ty `error-on-warning` + knip) and the `ms-authz` job are the mechanical
  halves; §10.1's comment/test discipline and §10.3's route-style rules are the review halves.
- When implementation reveals a rule here that contradicts a file, the fix follows `CLAUDE.md`'s
  standing order: correct the skill (in ra-skills) or this section **in the same commit** as the
  code.

---

## 11. Open questions

1. ~~Should authorship bypass the output-visibility check?~~ **RESOLVED 2026-08-08 (owner):
   terminal states only, as the PRODUCT decision, not just the technical default.** The
   notification best-practice rule is notify-on-needs-attention — failures loudest, completions
   second; "your run started" tells the person who clicked start nothing and is noise to everyone
   else. One visibility rule everywhere stands; START events notify nobody.
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
6. **Is `ActorStateTTL` enabled on the cluster's Dapr `Configuration`?** (§10.5a.) D5's
   `ttlInSeconds` backstop silently no-ops without it — S1 verifies against the chart and the
   running sidecar (Dapr 1.18.1) before the compaction reminder is allowed to assume the
   backstop exists. Related check: the state store component is `state.postgresql` **v1**
   (`chart/values.yaml:963`) while the diagrid ops reference recommends v2 for production —
   changing it rides the same coordinated no-hot-reload rollout as everything else on that
   component, so decide it in the §4 rollout window or not at all.
7. **Adopt the three `dapr-skills` review skills as estate review gates** for any diff touching
   `@wfr.workflow`/`@wfr.activity`/workflow-management code in `ingest`/`flows`? (§10.5b.) The
   estate already reviews against their rule IDs by hand; installing them (marketplace or
   vendored) makes it mechanical. Owner call — it changes `.claude/settings.json`.
8. **InboxActor saturation signal + delivery-in-turn question** *(added 2026-08-08, external-scan
   yield)*: `dapr_runtime_actor_pending_actor_calls{actor_type="InboxActor"}` is the turn-queue
   depth — the one metric that shows a slow SMTP/Slack call serializing every subsequent call for
   that user, because any output-binding call made INSIDE the actor turn holds the turn.
   S5 must decide: channel sends inside the turn (simple, serialized per user — probably fine) or
   handed off outside it (a queue hop, parallel) — and either way the vmalert rule on that series
   (start ~`>10 for 2m`) lands with S6. Also verify open_dapr.md q11 (Scheduler-held reminders)
   before trusting reminder durability semantics.
