# Audit: `services/notifications`

Legend — `N/` = `/home/user/rask/services/notifications/src/notifications/`, `SK/` = `/home/user/rask/packages/service-kit/src/service_kit/`, `T/` = `/home/user/rask/services/notifications/tests/`, `C/` = `/home/user/rask/chart/`.

## 1. How it touches the lakehouse

It never touches Lance. No pylance import, no catalog REST call, no S3 client anywhere under `N/` (`pyproject.toml` declares only `service-kit[governed]`, fastapi, pydantic, uvicorn, `dapr-ext-fastapi` — `/home/user/rask/services/notifications/pyproject.toml:7-19`). The event payload models are re-declared locally rather than imported from lineage (`N/api/lineage_events.py:9-16`). It reads exactly three fields off a run: `author.sub`, `lance.{run_id,project,originator}` and `outputs[].name` (`N/api/lineage_events.py:122-168`).

Three inputs:
- **Bus** — `lineage.events.v1` on its own JetStream component `lineage-pubsub-notifications` and `catalog.control.v1` on `catalog-control-pubsub-notifications` (`N/api/subscriptions.py:59-77, 103-129`; `C/templates/dapr-component.yaml:116-154, 179-211`).
- **Lineage's durable feed** — `GET /events?summary=false` walked by a cron reconciler, through Dapr service invocation `http://127.0.0.1:3500/v1.0/invoke/lineage/method` when Dapr is on, or `RASK_NOTIFICATIONS_LINEAGE_URL` when off (`N/api/settings.py:140-165`; `N/api/reconciler.py:248-259`).
- Nothing else. Outputs go to the Dapr state store and, optionally, SMTP/Slack output bindings.

Credentials held:
- **OIDC bearer** → `token.sub` is the only identity a route accepts (`SK/governed/deps.py:99-106`; `N/api/security.py:29-32`).
- **OpenFGA** client from `LANCE_FGA_*` (`N/lifespan.py:100-111`).
- **`APP_API_TOKEN`** (Dapr app token) — used both to *accept* sidecar deliveries (`SK/governed/dapr_auth.py:69-88`) and to *present itself* at lineage's service door as `dapr-api-token` + `x-lance-service-identity: notifications` (`N/api/reconciler.py:234-246`; `C/values.yaml:323`). One shared token plays both roles.
- **State store** via the sidecar only; the Postgres DSN lives in the Component via OpenBao (`C/templates/dapr-statestore.yaml:148-151`), never in the pod.
- **SMTP/Slack secrets** via Component `secretKeyRef` (`C/templates/notifications-channels.yaml:61-64, 84-85`).
- No estate S3 creds, no vended per-table creds.

The important asymmetry: the feed lane reads lineage **as the `notifications` service principal**, and lineage's `/events` is governed per referenced dataset with `can_get_metadata` on `table:<name>` for *that caller* (`/home/user/rask/services/lineage/src/lineage/api/v1/endpoints/runs.py:116-148`; `/home/user/rask/services/lineage/src/lineage/api/fga_deps.py:248-263`). So the reconciler sees a run only if the service itself holds reader on every dataset it references — see finding #1.

## 2. Authorization

Public routes (all under `RASK_API_PREFIX`, forwarded unrewritten by the gateway row `/api/notifications` — `/home/user/rask/services/gateway/src/gateway/__init__.py:135,178`):

| Route | Authn | FGA |
|---|---|---|
| `GET /notifications/inbox` (`N/api/inbox.py:49-101`) | subject from token | render filter `can_get_metadata` on `table:<object_id>`, **lineage rows only**; control-lane rows exempt (`inbox.py:46, 93-94`) |
| `GET /notifications/inbox/unread` (`inbox.py:104-118`) | subject | none — deliberately not visibility-filtered (`112-116`) |
| `POST /inbox/seen`, `POST /inbox/dismiss` (`inbox.py:121-142`) | subject | none |
| `GET /notifications/watches` (`N/api/watches.py:79-88`) | subject | none |
| `PUT /notifications/watches/{project_id}` (`watches.py:91-113`) | subject | single `check(member on project:<id>)` (`watches.py:42, 67-76`) |
| `DELETE /notifications/watches/{project_id}` (`watches.py:116-136`) | subject | none, by design |
| `GET/PUT /notifications/prefs` (`N/api/prefs.py:51-82`) | subject | none |

Root-mounted, Dapr-token-gated: `POST /lineage-events`, `POST /control-events`, `POST /dlq-event` (`N/api/subscriptions.py:59-77,103-129`; `N/api/dlq.py:35-56`), `POST /notifications-reconcile-cron` (`N/api/reconcile_cron.py:148-155`), and the SDK's `/actors/*`, `/dapr/config` via middleware (`SK/governed/dapr_auth.py:297-341`, called from `N/lifespan.py:51`). Ungated: `/health`, `/livez`, `/readyz`, `/dapr/subscribe`.

Where checks run:
- **Delivery**: `can_be_notified` (bare `reader`, `SK/governed/auth/model.fga:186,293,347,400`) as a subset test over *all* outputs per recipient (`N/api/visibility.py:137-150`; `N/api/fanout.py:165`). Three-outcome rule: FGA on + no client → 503/RETRY, never permissive (`visibility.py:116-119`).
- **Render**: `can_get_metadata` on the pointer's `object_id` (`visibility.py:129-135`; `inbox.py:93-94`).
- **Control lane**: no check at delivery *or* render — "being named is the targeting" (`N/api/control_events.py:11-16`; `inbox.py:79-92`).

Wrong-place / leak observations:
- The "membership re-checked at delivery" claim (`watches.py:9-11`, `N/watch_actor.py:13-14`) is not what runs: delivery checks `reader` on the output tables, not `member` on the project. Membership reaches tables only via `warehouse.reader … or member from project` (`model.fga:98`). A watcher removed from the project who still holds a direct table grant keeps receiving; a producer that stamps a `lance.project` unrelated to its outputs sends that project's watchers rows about another project's tables (audience noise, not disclosure, since `can_be_notified` still gates).
- `next_cursor` is minted from the last **raw** row, so a subject who lost a grant receives `base64(occurred_at, run_id@STATE)` of a run whose outputs they cannot see (`inbox.py:99`; `N/api/cursor.py:27-30`; pinned `T/test_adversarial_inbox.py:351`). The run id and terminal state leak; the object does not.
- The badge counts rows the page will never show (revoked lineage rows) until compaction — up to 30 days (`inbox.py:112-116`; `N/config.py:48`).
- Control-lane `object_id` and `extra.subject` are producer-trusted and copied verbatim into a named person's inbox (`control_events.py:125-139`). The only guard is the pubsub component scope. `task_assigned` rows name `annotation_task:` ids that no FGA type in the render path can check.

## 3. Lineage / events

Consumes:
- `lineage.events.v1` — `deliverPolicy: new` + durable `notifications-durable`, competing-consumer group `notifications` (`C/templates/dapr-component.yaml:191-211`). Handler: `ingest_run_event` (`N/api/ingest.py:79-121`).
- `catalog.control.v1` — same shape (`dapr-component.yaml:129-153`). Handler: `ingest_control_event` (`control_events.py:173-217`).
- `bindings.cron` `notifications-reconcile-cron` every 30 s (`C/templates/notifications-cron.yaml:16-29`; `C/values.yaml:310-313`) → `reconcile()` (`N/api/reconciler.py:314-473`).
- DLQ `dlq.notifications` (`C/templates/configmap.yaml:69`) → count + ERROR + ack (`dlq.py:35-56`).

Emits: nothing on any bus. Only OTel counters (`N/api/metrics.py:562-647`), channel sends (`N/api/channels.py:152-191`) and log lines.

Idempotency / ordering:
- Natural key `run_id@STATE` for runs (`N/models.py:103-109`; `lineage_events.py:207-215`), `<event_id>@<ACTION>` for control (`control_events.py:125-134`). `deliver` is idempotent on it inside the actor turn (`N/inbox_actor.py:321-343`). Redelivery = counted `DUPLICATE`; the same run arriving on both lanes lands one pointer (`T/test_ingress_dedupe.py:180-260`).
- `occurred_at` is the producer's `eventTime`, unbounded (`lineage_events.py:71-74`). A START arriving after FAIL adds nothing; two terminal states are two rows (`T/test_adversarial_ingress.py:355-374`).
- Channel sends are idempotent on `(notification_id, channel)` via a claim written *before* the send that is never rolled back (`channels.py:179-189`; `inbox_actor.py:508-533`).

Lost / duplicated event:
- Bus lost → reconciler re-offers within ≤1 tick, walking down with a 64-seq overlap for out-of-order commits (`reconciler.py:61-76, 361-369`). First tick primes and notifies nobody (`476-488`). Feed pruned below cursor → ERROR + `notifications.feed.gaps` (`438-448`).
- A recipient that RETRYs 10 consecutive passes is stepped over and lost, loudly (`reconciler.py:78-89, 449-462`).
- **Control lane has no reconciler.** A `grant_revoked`/`task_assigned` that exhausts `maxDeliver` (3 or 5) parks in the DLQ with no auto-replay (`dlq.py:9-16`). Loss is permanent until an operator replays.

Silently dropped with a SUCCESS ack (coverage decided at the producer): non-terminal states, missing/non-string `author.sub`, zero outputs (`lineage_events.py:196-202`); control actions outside `NAMED_ACTIONS`, missing `extra.subject`, `user:*`, usersets when FGA is off (`control_events.py:105-122`); watcher-index outage degrades to author-only (`N/proxies.py:150-164`); channel push failures never change the outcome (`fanout.py:138-146`).

Poison handling asymmetry: bus lane DROPs an unparseable event per event; the feed lane validates per *page*, so one row with a non-int `seq` stalls the lane forever (`reconciler.py:259`; pinned `T/test_adversarial_ingress.py:426-455`).

## 4. State

Durable, all in `lance-statestore` (Postgres via Dapr; `C/templates/dapr-statestore.yaml:132-157`, scoped to `notifications` at `C/values.yaml:1434`):
- `InboxActor/<base64url(sub)>` — four partitions: `inbox-meta` (count, rows, `compaction_due_at`), `inbox-rows` (the pointers incl. the `sent` ledger), `inbox-watches`, `inbox-prefs` (email/Slack destinations), plus `inbox-digest` (`N/inbox_actor.py:76-86`). Meta+rows are written in one transactional save (`278-297`).
- `WatchIndexActor/<project_id>` — one `watchers` JSON list (`N/watch_actor.py:36, 64-93`).
- Reminders `compaction` (repeating, drop-on-failure) and `digest` (one-shot) in the Scheduler's etcd — a second store with no transaction to the first; arm-before/disarm-after ordering and read-path repair compensate (`inbox_actor.py:21-37, 232-276`).
- Plain-state key `notifications-lineage-cursor` (`reconciler.py:58, 262-311`), last-write-wins, no etag.

Single-writer assumptions:
- Actor turn-based concurrency is the *only* lock; no OCC (`inbox_actor.py:3-8`). The tests show exactly what is lost without it: an overlapping deliver, mark-seen or compaction turn rewrites the whole row set from a stale read and silently drops rows (`T/test_adversarial_inbox.py:400-480`).
- `_reconcile_lock` is a process-local `asyncio.Lock` (`N/api/reconcile_cron.py:51`); the cursor store docstring says "one writer, no contender" (`reconciler.py:295`).
- `replicas: 1` (`C/values.yaml:269`), with a comment asserting it is not an actor constraint (`263-265`).

With 2 replicas: actors stay correct (placement). The cron binding is delivered to **each** sidecar, the lock does not span pods, and two walks race the same cursor: duplicates are absorbed, but `resume_from`/`pending_high`/`stalls` parked by one pod are clobbered by the other's settle, and a second pod's prime can never happen (cursor exists) — so the practical damage is doubled feed/FGA load and lost stall bookkeeping, not lost rows. The channel `lru_cache` tables and `DaprClient` are per-process and fine.

## 5. Dapr coupling

| Block | Where | Dapr-free replacement |
|---|---|---|
| Actors (state + reminders + turn serialisation) | `inbox_actor.py`, `watch_actor.py`, `proxies.py:110-119`, `lifespan.py:137-156` | JetStream KV per subject with CAS (`revision`) for the read-modify-write, or a Postgres row with `SELECT … FOR UPDATE`; reminders → a scheduler table swept by the existing cron; turn-lock → CAS retry loop |
| Pub/sub subscriptions ×3 (lineage, control, DLQ) | `subscriptions.py:47-129`, `dlq.py` | Native JetStream durable pull consumers with queue groups; DLQ via `MaxDeliver` + advisory subjects |
| Input binding cron | `reconcile_cron.py:148-155`, `notifications-cron.yaml` | In-process scheduler guarded by a JetStream KV lease/lock |
| Plain state API (cursor) | `reconciler.py:262-311` | JetStream KV key with revision-checked put |
| Output bindings smtp/http | `channels.py:92-146`, `proxies.py:167-199` | aiosmtplib + httpx; credentials from OpenBao directly |
| Service invocation + `invokeRetry` resiliency | `settings.py:140-165`, `C/templates/dapr-resiliency.yaml:59,96-100` | httpx with an explicit retry on 408/429/5xx |
| App token (`APP_API_TOKEN`) on delivery routes and as the lineage service credential | `dapr_auth.py:69-88`, `reconciler.py:234-246` | mTLS/SPIFFE or an OpenBao-issued per-service token; today one shared token authenticates both directions |
| Secrets via Component `secretKeyRef` | `notifications-channels.yaml`, `dapr-statestore.yaml` | OpenBao client at boot |
| Sidecar metadata probe / proxy warm-up | `lifespan.py:150-156`, `SK/governed/actor_state_store.py` | drops out entirely |

## 6. Lake-format awareness

None. `LineageDataset` reads only `name` and ignores `namespace` (`lineage_events.py:43-50`); the pointer names `outputs[0].name` (`215`); `lance.version` in the facet is never read; the FGA object is `table:<name>` unless the name already contains `:` (`visibility.py:63-89`). No notion of branch, tag, version, base path, blob descriptors or clones anywhere in `N/`.

Consequences:
- The catalog's branch and tag endpoints emit **no** lineage at all (`/home/user/rask/services/catalog/src/catalog/api/v1/endpoints/branches.py`, `tags.py` — no `emit` call), so a branch write, branch delete, tag create/delete or clone produces no notification to anyone. A tag *promotion* (`blessed`) does emit, at the tag's target version (`/home/user/rask/services/catalog/src/catalog/core/lineage_emit.py:102-104`), and reaches the author as a plain `table` row.
- If a runner later emits a branch write with `outputs[].name = ns$table`, the audience is the main table's `reader` set and the row cannot say which branch; `run_id@STATE` still dedupes correctly.
- A dataset name containing `:` is treated as an already-typed FGA id and checked against a type that may not exist → silently `False` → hidden (`visibility.py:85-89`).

## 7. Governance gaps

- **Read audit**: only authn is audited (`SK/governed/deps.py:86-96`); no `audit()` call anywhere in `N/`. Inbox reads and the render `batch_check` leave no audit record; lineage's `record_read` (`fga_deps.py:90-93`) has no counterpart here.
- **Right to erasure**: no delete route for an inbox, prefs or watches. Erasing a subject means deleting `InboxActor||<b64 sub>` rows by hand *and* scanning every `WatchIndexActor` (no reverse index — `watch_actor.py:1-7`, `N/models.py:322-335`), plus the `sent` ledger and log lines that carry the subject (`fanout.py:171-174`). The subject also appears in `dapr-caller`-side logs and OTel spans (`fanout.py:130-132` puts `notification_id`, not the subject, on the span — good).
- **Retention/TTL**: pointers 30 d / 200 rows via the compaction reminder every 6 h; `ActorStateTTL` is off so the `ttlInSeconds` belt is unfastened (`N/config.py:48-75`; `inbox_actor.py:286-297`). Watches, prefs, digest flag and the lineage cursor never expire (`inbox_actor.py:444-446`). Nothing bounds an inbox between ticks (`T/test_adversarial_inbox.py:528-545`).
- **Quotas**: per-subject row cap only; no per-project or per-producer quota; a producer can fill every recipient's inbox up to the cap and pay quadratic blob rewrites.
- **PII in payloads**: email/Slack destinations stored plaintext in actor state (`models.py:299`); the rendered message carries `object_id`, `source_run_id`, reason and instant (`channels.py:60-74`) to an ungoverned address; Slack webhook body includes the table id; `emailTo` rides binding metadata (`channels.py:118`). Subject on ERROR log lines by design (`fanout.py:171-174`; `metrics.py:8-14` keeps it off labels).
- **Least privilege of the service principal**: for the feed lane to reconcile any run, `notifications` must hold reader on every table those runs reference — an estate-wide read grant for a service that only needs the pointer.

## 8. Tests

32 files, ~470 tests, all sidecar-free (fakes for state manager, FGA, actor proxies). Pinned well:
- Actor invariants, partition split, reminder ordering/repair, owner second-lock, schema drift → unreadable-not-absent (`T/test_inbox_actor.py`, 56 tests).
- Ingress status matrix, dedupe across lanes and restarts, audience rules incl. ORIGINATOR, FGA-off/unwired/outage outcomes (`T/test_ingress_status_matrix.py`, `test_ingress_dedupe.py`, `test_ingress_restart_dedupe.py`, `test_ingress_audience.py`, `test_lineage_projection.py`).
- Reconciler walk, prime, floor, overlap, parking, stalls, gaps (`T/test_reconciler.py`, 38; `test_reconcile_cron.py`, 21).
- Door contract: cursor opacity, wire projection (`sent` never on the wire), 401/503 refusals, gateway prefix (`T/test_inbox_door_contract.py`, `test_inbox_leak_containment.py`, `test_inbox_routes.py`).
- Dapr token/public-caller refusal on every delivery route (`T/test_adversarial_ingress.py:128-185`).
- Lane parity sweep so both lanes forward `watchers`/`push` (`T/test_lane_parity.py`).

Pinned **as gaps** (assert the defect exists): overlapping turns lose rows (`test_adversarial_inbox.py:400-480`), future-dated flood evicts real unread rows (`483-509`), past-dated row trimmed before read (`512-525`), no bound between ticks (`528-545`), bare 500 on store outage (`574-592`), unbounded `seen` payload (`637-655`), blocking sidecar health wait on the event loop (`780-823`), unbounded producer strings and unroutable actor ids (`test_adversarial_ingress.py:311-345`), permanent recipient failure retried forever (`375-390`), one bad feed row stalls the lane (`426-455`).

Untested: `WatchIndexActor` directly (only via route fakes in `test_watches.py`); a real OpenFGA model evaluation of `can_be_notified` vs `can_get_metadata`; real SMTP/HTTP bindings (fakes only, `test_channels.py`, `test_channel_bindings.py`); multi-replica cron/cursor behaviour; DLQ replay; anything about erasure or retention of watches/prefs; the lineage feed's governance interaction with the service principal's grants.

## 9. Top findings

1. **HIGH — Feed-lane coverage depends on the service principal's own table grants.** `GET /events` filters rows by `can_get_metadata` for the caller `notifications` (`runs.py:116-148`, `fga_deps.py:248-263`), so any run over tables the service is not granted is invisible to the reconciler and the tick logs success (`reconciler.py:19-21`); closing it requires estate-wide reader for a service that only needs a pointer. *Fix:* add a service-only, ungoverned projection of the feed (seq, outputs' names, author/lance facets, no payload) gated by a dedicated relation such as `can_observe_events` on the root object, and keep per-recipient `can_be_notified` as the sole disclosure gate.

2. **HIGH — Unbounded producer strings become permanent retry loops.** No `max_length` on `notification_id`/`object_id`/`author.sub` (`models.py:122-135`; `proxies.py:42-60`), and `_deliver_one` collapses every exception — including `ValueError` from an unroutable actor id — into `RETRIED` (`fanout.py:164-176`). *Fix:* bound the delivery fields and the subject at the model, and make `_deliver_one` return a permanent outcome (counted, not retried) for `ValueError`/validation faults.

3. **HIGH — The producer's `eventTime` is both the sort and the retention key, and the cap is only eventual.** `compact` keeps the *newest* unread rows by `occurred_at` (`N/feed.py:61-80`), so future-dated rows evict genuine unread failures and past-dated rows vanish before anyone reads them; nothing trims on the write path (`inbox_actor.py:299-343`). *Fix:* stamp a service-side `received_at` in `InboxPointer.arriving`, compact and cap on it, and apply `inbox_max_rows` inside `deliver`.

4. **MEDIUM — The control lane is trusted end-to-end and has no catch-up path.** `extra.subject` and `object_id` are copied verbatim into a named inbox with no delivery or render check (`control_events.py:125-139`; `inbox.py:93-94`); the only guard is the pubsub component scope, and a dead-lettered `grant_revoked` is lost until manual replay (`dlq.py:9-16`) because no reconciler exists for `catalog.control.v1`. *Fix:* reconcile the control lane from the catalog's durable audit trail the way the lineage lane reconciles from `/events`, and at least verify `object_id` matches `object_type` and that the actor app-id (from the CloudEvent `source`) is one entitled to name that object type.

5. **MEDIUM — Dependency failures surface as bare 500s and block the event loop.** `_translating` re-raises anything but `InboxUnreadable` untouched (`proxies.py:98-105`), so a dead state store is a 500 without problem+json; and `typed_proxy` builds `ActorProxy` per call, whose constructor runs a synchronous 60 s sidecar health wait (`proxies.py:110-119`; pinned `T/test_adversarial_inbox.py:780-823`). *Fix:* map `DaprInternalError`/transport errors to `ServiceUnavailableError` in `_translating`, and build one proxy factory in the lifespan (the warm-up at `lifespan.py:152` already exists; the per-call `create` is what keeps blocking).

Runner-up: the reconcile lock and cursor are single-process/last-write-wins (`reconcile_cron.py:51`; `reconciler.py:294-311`) while the chart says replicas may scale (`C/values.yaml:263-265`) — either pin `replicas: 1` in an invariant test or use a first-write etag on the cursor.

## Where `.claude/skills/rask-notifications/SKILL.md` contradicts the code

- **Reason count.** Description says "six targeting sources"; body says "one of four reasons" (line 12). Code has 12 `NotificationReason` members (`N/models.py:43-89`) and 8 `NAMED_ACTIONS` (`N/api/control_events.py:42-53`). `promotion_review_requested` is absent from the Q1–Q6 decision table entirely.
- **Line references.** `notifiable()` is at `lineage_events.py:171-222`, not `154-203` (line 84). The watcher-loop skip is `fanout.py:93`, not `:88` (line 191). `TERMINAL_STATES` at `:32` and `fanout.py:87` for the author are correct.
- **`lease_expired`.** Lines 61-69 say it is covered; lines 258-260 ("What is still uncovered", class 2) say "the emit site does not [exist]". Code has `task_lease_expired` in both `NAMED_ACTIONS` (`control_events.py:50`) and `NotificationReason` (`models.py:89`).
- **"notifications' `reader` on the feed"** (lines 214-215). There is no feed object: `/events` is governed per referenced dataset via `can_get_metadata` on `table:<name>` for the calling principal (`runs.py:148`, `fga_deps.py:248-263`) — the grant needed is reader on every table, not on a feed.
- **Render check on control rows** (line 94 "render re-runs `can_get_metadata`"). Render skips every control-lane reason (`inbox.py:46, 93-94`); the skill describes only the delivery-side exemption (lines 78-80).
- **"membership re-checked at delivery"** (line 51 and `watches.py:9-11`). Delivery checks `can_be_notified` on the output tables (`fanout.py:165`), never `project#member`.
- **`named_subject` returns `None`** (line 135). The function is `named_subjects` and returns a tuple (`control_events.py:84-122`).
- **Topology omission.** The skill names only the `InboxActor`; `WatchIndexActor` per project (`N/watch_actor.py:32`) is the second durable actor and the one an erasure has to sweep.