# open_workflow_retention — Dapr workflow history, and the operator surface nothing reaches

> **Scope.** Two things that surfaced together on 2026-08-26 and belong in one file because one caused
> the other: what happens to a workflow instance's actor state after it goes terminal, and the fact
> that the estate now has lifecycle-control doors no user interface can reach.
>
> **This file is deleted when both halves land.** Working plans live at repo root; `docs/` is settled
> architecture only.

## Already true — do NOT redo these

Two of the three things a reader would reach for are already done, and one of them is done better
than the obvious version. Recorded here so the next sweep does not re-derive them.

**The retention policy exists, is live, and reaches every workflow-hosting app.**
`chart/templates/observability.yaml` renders `stateRetentionPolicy` onto the `lance-tracing`
Configuration, per terminal state rather than `anyTerminal`:

```yaml
stateRetentionPolicy:
  completed: "168h"    # a cache of a fact lineage already stores durably
  failed: "720h"       # the post-mortem, asked days later
  terminated: "720h"
```

Verified live on 2026-08-26:

```
$ kubectl get configuration.dapr.io lance-tracing -o jsonpath='{.spec.workflow}'
{"stateRetentionPolicy":{"completed":"168h","failed":"720h","terminated":"720h"}}
```

The `dapr.io/config: lance-tracing` annotation is **unconditional** in `rask.daprAnnotations`, and
that is load-bearing rather than incidental: it was once gated on `lance.otelEnabled`, which was right
while the Configuration held only tracing and wrong the moment it also held retention — turning
telemetry off silently turned retention off, and history is then kept forever. All seven
workflow-hosting sidecars carry it (`ingest`, `flows`, `medallion-producer`, the three movers,
`notifications`).

**There is deliberately no application-side purge, and that call is correct.**
`DaprWorkflowClient` exposes `purge_workflow(instance_id)` but **no list-instances API**, and the only
index that could drive a sweep — `InMemoryRunStore` — is documented as deliberately non-durable, so
after a restart it cannot name the instances whose history persists. A sweep built on it would collect
only what happened since the last pod start. The built-in policy needs no such index — see the mechanism below.

**Retention is demonstrably working for post-policy instances.** The day distribution is a clean
natural experiment — see R1.

---

## Order of work

Six items. Three are small, one is a decision, one is real infrastructure.

| # | item | size | blocked by |
| ---: | --- | --- | --- |
| ~~**0**~~ | ~~**Redeploy**~~ — **DONE 2026-08-26**, fleet on `main-553ec99b` (helm rev 57), all pods Running, 0 restarts | — | — |
| ~~**1**~~ | ~~**R1** purge the 64 orphans~~ — **DONE 2026-08-26**, 1367 rows → 0 | — | — |
| ~~**2**~~ | ~~**U1** ruling~~ — **RULED 2026-08-26: SURFACE**, scoped to `compute/ingest` only | — | — |
| ~~**3**~~ | ~~**R2** retention exporter + alert~~ — **DONE 2026-08-26**, live on helm rev 62; every rule replayed against the real GreptimeDB | — | — |
| ~~**4**~~ | ~~**U2** controls on `compute/ingest`~~ — **DONE 2026-08-26**, live and browser-verified | — | — |
| **5** | **U3** surface history/retention | re-ask — **now askable**, see below | — |

**Step 0 traps, each of which has bitten this estate before.** Build from a CLEAN DETACHED WORKTREE
(`git worktree add /tmp/rask-deploy --detach origin/main`): Dagger snapshots the HOST, not git, so a
concurrent session's dirty files land inside an image tagged with a commit that never contained them.
The registry is addressed twice — `172.17.0.1:5000` to push (Dagger's engine is itself a container, so
`localhost` there is the engine) and `localhost:5000` for k3s to pull. `make k3s-up` OWNS the release;
a bare `helm upgrade` with different values replaces every deployed image with the chart default. Run
`make dagger-engine` first, or `publish` dies on the plain-HTTP registry with "server gave HTTP
response to HTTPS client". And never pipe the build to `tail` — the pipe's exit code is reported, not
the build's, so a failed build prints success.


**Two traps hit during the 2026-08-26 redeploy, recorded so the next one does not.**

* **`make k3s-up` was the WRONG command for this release.** The plan said to use it; the live release
  runs `image.localImages: false` (registry-based, per-image `image.tags`), and `k3s-up` renders with
  `localImages=true` — it would have flipped every image to side-loaded mode. The right command was
  `helm upgrade rask chart --reuse-values --set image.tags.<svc>=<tag>`, which also preserves a
  concurrent session's values.
* **`flows` is NOT in `COMPOSE_IMAGES`** and so is missed by any loop built from that variable, even
  though it has its own `.docker/flows.dockerfile` and its own deployment. It was caught only by
  reading `helm get values`' tag map. It is also the one service whose `regex` lock fix mattered —
  and its build succeeding is the proof that fix works, since `uv sync --frozen --package flows`
  refuses a stale lock.

**Why 3 outranks 1 in value but not in order.** R1 is inert disk; R2 is the difference between
"retention is fixed" and "we can tell when retention breaks". R1 goes first only because it is minutes
and closes a measured, sized item cleanly.

## The backlog

### R1 — ~~64 orphaned `ingest` instances~~ · **DONE 2026-08-26**

**Measured 2026-08-26**, `daprstate.state`:

| insertdate | rows |
| --- | ---: |
| 2026-08-03 | 177 |
| 2026-08-05 | 212 |
| 2026-08-06 | 527 |
| 2026-08-07 | 451 |
| **08-08 → 08-22** | **(none)** |
| 2026-08-23 | 989 |
| 2026-08-24 | 675 |
| 2026-08-25 | 3834 |
| 2026-08-26 | 374 |

The pre-policy block totals **exactly 1367** — the same number `observability.yaml`'s own comment
recorded on 2026-08-10, unchanged 23 days later. All 1367 belong to **`ingest` alone**: 64 distinct
instances × (1 `metadata` + ~20 `history-NNNNNN` records), keys shaped
`ingest||dapr.internal.default.ingest.workflow||<uuid>||history-000024`.

**Why they will never self-collect**, and why this is not a bug in the policy: the retention docs are
explicit that a policy applies only to workflows that *newly* reach a terminal state. Existing
terminal instances require a manual purge. The 08-08 → 08-22 gap is the proof the policy works —
post-policy instances in that window were collected — and the untouched pre-policy block is the proof
it cannot reach backwards.

**DONE 2026-08-26.** Purged via the sidecar loop below; **1367 rows → 0**, and the whole workflow table
now starts at 2026-08-23 — inside the retention window.

Two things learned in the doing, both worth keeping:

* **The 64 distinct ids were 43 instances + 21 ACTIVITY keys.** The activity records are keyed
  `<instance-uuid>:0004`, not bare UUIDs, so a loop that assumes one id per instance purges the wrong
  set. Purging the 43 real instances collected their activity keys too — purge deletes an instance's
  metadata *and* its associated keys, as the protocol doc says.
* All 43 returned 2xx and **zero failed**, which is what "COMPLETED is purgeable, no `--force`"
  looks like in practice.

**How — measured 2026-08-26, and simpler than the docs' CLI path suggests.** There is no `dapr` CLI
on this host and none is needed: the sidecar's HTTP API is reachable from the app container
(`/v1.0/healthz` → 204), and a sampled orphan resolves as **COMPLETED** —

```json
{"instanceID":"357e0f98-6e75-548c-94f6-c4e6d40804b9","workflowName":"ingest_run",
 "createdAt":"2026-08-07T11:41:44Z","runtimeStatus":"COMPLETED"}
```

Terminal, so purge accepts it and **no `--force` is involved** (this note previously implied it was;
that was over-cautious). One loop over 64 ids, one app-id:

```bash
POD=$(kubectl get pod --no-headers -o name | grep rask-ingest | head -1)
IDS=$(kubectl exec rask-age-0 -- bash -lc "psql -U lance -d daprstate -tAc \
  \"select distinct split_part(key,'||',3) from state \
    where key like '%workflow%' and insertdate < '2026-08-08'\"")
for id in $IDS; do
  kubectl exec ${POD#pod/} -c ingest -- \
    curl -s -X POST "http://127.0.0.1:3500/v1.0-beta1/workflows/dapr/$id/purge"
done
```

Do not reach for a direct `DELETE` on `daprstate.state` — it bypasses the actor runtime that owns
those keys. Re-measure afterwards with the queries below.

**Cost of leaving it:** disk only. These rows are inert — no correctness consequence, no replay
consequence. Rank it accordingly.

### R2 — ~~nothing alerts on retention stalling~~ · **DONE 2026-08-26**

`chart/alerting/rules.yml` carried `WorkflowActivitiesFailing` and `DaprConsumerWedge` and **no rule on
state-store growth or on retention failing to collect**. Both measurements this estate had of
workflow-history volume — 1367 rows on 2026-08-10, 7239 on 2026-08-26 — happened because a person went
looking. The first automatic symptom would have been a full disk.

**The obstacle was real and was the whole cost of the item.** Enumerated live against the `ingest`
sidecar, daprd's ENTIRE metric surface is `dapr_error_code_total`, `dapr_grpc_io_*`, `dapr_http_*`,
`dapr_runtime_component_{init_total,loaded}` and `go_*`. No workflow metric, no actor metric, no
state-store metric — and `dapr_runtime_workflow_*` (which `rules.yml` already documents as unreliable
for this codebase) is absent outright. A fourth scrape job would have had no target, so this could
never have been a rules edit.

**Shipped as `2fc1ad7b` + `561efd99`, live on helm rev 62.** The state store is measured by
QUERY rather than by scrape: a `sqlquery/daprstate` receiver on the Collector that already ships every
other metric — no second image to vendor and scan, no second credential path, no new service. One
query, 4.7 ms over a 7.5 MB table, every 5 minutes. The password arrives by `secretKeyRef` and the
config references `${env:DAPRSTATE_PASSWORD}`, so no credential is in the ConfigMap.

Two gauges, two rules:

| series | rule | why |
| --- | --- | --- |
| `dapr_workflow_history_oldest_age_seconds` | `DaprWorkflowHistoryNotCollected` > 792h, `for: 30m` | the PROPERTY. Row count varies legitimately with load; age does not |
| `dapr_workflow_state_rows` | `DaprWorkflowStateMetricsMissing` — bare `absent()`, `for: 15m` | `max()` over an empty vector returns nothing, so a dead receiver makes the age rule permanently quiet — identical to a well-collected store |

**Two design points worth keeping**, because both were nearly got wrong:

* **The age metric reads HISTORY rows only** (`key like '%||history-%'`), and that filter is
  load-bearing rather than tidy. A workflow's `metadata` row keeps its original `insertdate` across
  `continue_as_new`, so the movers' watch loops would look arbitrarily old forever and the alert would
  fire on a perfectly healthy store.
* **No `unit:` field on the metric; the name carries `_seconds` itself.** Declaring a unit makes the
  OTLP→Prometheus convention APPEND the suffix — exactly how `outbox_oldest_age` shipped as a rule
  that could never fire.

**Verified end to end, not assumed:**

* the SHIPPED collector config loads — rendered out of the chart and run through `otelcol-contrib
  validate` in the real `0.157.0` image via Dagger, with a negative control (`value_type: bogus`)
  proving `validate` actually rejects a bad config rather than passing everything;
* both series are live in GreptimeDB, and their values match the database: `dapr_workflow_state_rows`
  6157, `dapr_workflow_history_oldest_age_seconds` 267571 (3.1 days) against psql's 6157 / 267571;
* every alert expression is evaluable **on the production engine** — `make alert-rules-drill` replayed
  all 31 rules against the live GreptimeDB, "all evaluable". `max(...) > 2851200` and
  `absent(dapr_workflow_state_rows)` both return empty right now, which is what a correctly-collecting
  store looks like.

**Three gates hold it together**, since `rules.yml` is mounted with `.Files.Get` and cannot be
templated: the rules cannot ship without the receiver and the receiver cannot be renamed or deleted
while they exist (both directions, plus a declared metric no rule reads is refused); and the threshold
literal must track `dapr.workflowRetention` in `values.yaml` the way `GreptimeDBMemoryHigh` tracks the
memory limit.

**vmalert itself is OFF in dev** (`observability.alerting.enabled: false` — dev has no on-call;
`values-prod` flips it). That is pre-existing and correct: the series and the expressions are both
proven against the real store, and the only unexercised leg is Alertmanager's routing, which is a prod
drill.

**A defect found while verifying, fixed in the same pass.** `e1b8f3dd` moved the runbooks into
`docs/runbooks/` and left **24 of 29 alert annotations** pointing at the old flat path — dead links in
the one text an on-caller reads at 3am — plus a `#dlq-parking` anchor that had never matched its
heading, and four more stale `docs/RUNBOOK-restore.md` pointers (one of them inside a helm render
FAILURE message, shown to someone whose upgrade just died). All fixed, and a gate now resolves both
the file and the anchor of every runbook link an annotation cites.

### U1 — ~~five operator routes with no caller~~ · **RULED + PARTLY CLOSED 2026-08-26**

Landed 2026-08-25 in the Dapr audit drain; every one is reachable only by `curl`:

| route | service |
| --- | --- |
| `POST /v1/ingests/{run_id}/terminate` | ingest |
| `POST /v1/ingests/{run_id}/pause` · `/resume` | ingest |
| `POST /api/flows/runs/{run_id}/terminate` | flows |
| `GET /api/trains/{id}` · `POST /api/trains/{id}/terminate` | medallion producer |
| `GET /api/movers/{m}/stages/{id}` · `POST …/terminate` | medallion producer → mover |

Grepped every zone: **zero call sites.** The only `pause` matches in the frontend are media players.

This is residue I created. It is defensible if the intent was a break-glass lever an operator reaches
with `curl`, and indefensible if the intent was an operator surface. **That is the ruling below.**

### U2 — ~~`compute/ingest` shows status and offers no control~~ · **DONE 2026-08-26**

The natural home already exists and is half-built. `compute` renders ingest run status over
`getIngestRunStatus` / `listIngestRuns`, with both a list page and a `[run_id]` detail route.

**Done means:** terminate / pause / resume on the run detail page, and terminate on the list rows.

**How, concretely** — this follows the zone's existing shape, so it is small:

* Add `command()` functions to `compute/src/lib/remote/ingest.remote.ts` beside `startIngest`, which
  is already a `command()` for exactly this reason (typed app **values** ride remote functions; only
  bulk/binary rides `+server.ts`).
* Single-flight the read: `void listIngestRuns().refresh()` in the handler. A remote `query()`
  re-called returns the **cached** value — hold the query and `.refresh()` it, or the row will not
  move after a terminate.
* Return the estate's `ApiResult<T>` union rather than throwing, on the dock-layout precedent:
  status-driven UI states, not exception flow. The routes answer 409 for a terminal run and 503 for an
  unreachable engine, and both are things a person needs to *see*.
* **Show disabled, never hide.** Estate ruling: every action is always visible; one the caller may not
  take renders disabled with the denial reason. A terminate hidden because the run is COMPLETED is
  indistinguishable from a missing feature.
* The 202 bodies already say what terminate does **not** do — for ingest it stops further scheduling
  while in-flight activities finish; for train and stage it stops the WATCH and never the Ray job.
  Surface that text rather than re-inventing a reassurance; it was written to stop exactly the
  misreading a button invites.

`flows`, `train` and `stage` have no equivalent status page, so they are a larger question and should
not be bundled with this one.

**DONE 2026-08-26 — shipped, deployed and browser-verified.** `4e44584c`; `web-compute:main-4e44584c`
live on the cluster.

Pause / Resume / Terminate sit in the run detail header, over three `command()` functions that
single-flight `getIngestRunStatus` and `listIngestRuns` — without that the button works, the door
acts, and the page keeps showing the old state, because a re-called remote `query()` returns its
CACHED value.

**Verified signed-in against the live estate**, not just built: logged in through Dex as
`alice@example.com` at `http://localhost:8080` (the origin the OIDC config expects — the ingress IP
cannot complete the redirect, since `publicIssuer` is `localhost:8080/dex`), opened a real COMPLETE
run, and read the rendered buttons back out of the DOM:

```json
[{"label":"Pause","disabled":true,"why":"This run is COMPLETE — there is nothing to pause."},
 {"label":"Resume","disabled":true,"why":"This run is not paused."},
 {"label":"Terminate","disabled":true,"why":"This run is COMPLETE — there is nothing to terminate."}]
```

Shown-disabled with the reason on each button, per the estate ruling. Zero console errors.

**ONE VERIFICATION GAP, stated rather than glossed:** only the DISABLED path is proven. There was no
live ingest run on the cluster to click Terminate on, so the enabled path — button → door → 202 →
single-flight refresh → state changes on screen — has not been exercised against a real run. The
client seam has six unit tests covering it (path, id encoding, bearer, 409-as-value, non-JSON body,
`detail` preserved), but that is not the same claim. Closing it needs a live run to act on.

### U3 — workflow history and retention are surfaced nowhere · **RE-ASK IS NOW DUE**

No zone shows how much history exists, how old it is, or what the retention policy is. The one
promising grep hit — `home/src/lib/remote/policies.remote.ts` — is the **Lance table** compaction and
retention plane (the maintenance surface), which is a different thing entirely.

This was deliberately deferred behind R2 on the reasoning that *an alert may make the page
unnecessary*. R2 has landed, so the question is answerable now, and the honest answer is **mostly
yes**: the operator concern is covered. Retention stalling now pages, the numbers are queryable in
GreptimeDB, and a page rendering a gauge nobody acts on is a surface to maintain rather than a feature.
The estate's own precedent for operator concerns is an alert, not a page.

**What R2 does NOT cover, and is the only part still worth building:** a person looking at a specific
run has no way to know how long its history will survive. That is a per-run fact on a page that already
exists (`compute/ingest/[run_id]`), not a new observability surface — a line in the run header, sourced
from the same policy values, saying how long this run's history is kept given its terminal state.
Small, and it answers the question a user actually has.

**Recommendation: build that line, drop the dashboard.** Owner's call.

---

## Rulings needed before U1/U2 are actionable

1. **Are the five routes an operator surface, or a break-glass lever?** If break-glass, U1 closes by
   recording that decision beside the routes and U2 does not happen. If a surface, U2 is the first
   increment and `flows`/`train`/`stage` need their own status pages before their controls make sense.
2. **If a surface: who may press the button?** The routes gate on `authorize_ingest` /
   `security.EXECUTE` / `authorize_produce` — whoever may spend the estate's compute may stop
   spending it. A UI makes that reachable by anyone who can load the page, so the gate has to be
   *visible* (disabled + reason) rather than merely enforced server-side.

## How to re-measure

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# is the policy live, and what does it say
kubectl get configuration.dapr.io lance-tracing -o jsonpath='{.spec.workflow}'

# does every workflow-hosting sidecar reference it
kubectl get pods -o custom-columns='POD:.metadata.name,CFG:.metadata.annotations.dapr\.io/config' --no-headers

# volume, TTLs, and age spread
kubectl exec rask-age-0 -- bash -lc "psql -U lance -d daprstate -tAc \
  \"select count(*), count(expiredate), min(insertdate)::date, max(insertdate)::date \
    from state where key like '%workflow%'\""

# the orphans, by app and by instance
kubectl exec rask-age-0 -- bash -lc "psql -U lance -d daprstate -tAc \
  \"select split_part(key,'||',1) app, count(distinct split_part(key,'||',3)) instances, count(*) rows \
    from state where key like '%workflow%' and insertdate < '2026-08-08' group by 1\""
```

`count(expiredate) = 0` across the whole table is **expected and not a symptom**: retention is enforced
by DELETING rows, not by writing a TTL column. Judge collection by the age distribution, never by that
count — reading it as a failure signal is the mistake this note prevents.

## The mechanism, measured rather than assumed

The Dapr docs describe the policy and say nothing about what enforces it, and an earlier version of
this file guessed "the scheduler". That was wrong, and the sidecar answers it directly. Every
workflow-hosting app registers **three** actor types, not two:

```
$ curl -s 127.0.0.1:3500/v1.0/metadata   # from inside the ingest pod, 2026-08-26
actors hosted: ['dapr.internal.default.ingest.retentioner',
                'dapr.internal.default.ingest.workflow',
                'dapr.internal.default.ingest.activity']
scheduler:     {'connected_addresses': ['10.42.0.130:50006', '10.42.0.129:50006', '10.42.0.128:50006']}
```

`retentioner` is the janitor, and it lives in **the app's own sidecar** — not in a central controller.
It holds no state of its own (the state table contains `.workflow` keys and nothing else), so the
window it is waiting out is a **reminder**, and since Dapr 1.15 reminders live in the scheduler's etcd.
Confirmed by elimination too: `notifications` hosts two actor types and no `retentioner`, because it
runs no workflows.

So the chain is: instance goes terminal → the app's `retentioner` registers a reminder for the window
→ the reminder fires from the scheduler's etcd → the retentioner deletes that instance's rows.

**That shape is exactly why R2 exists.** The timer and the data it governs are in DIFFERENT failure
domains — a reminder in a 3-replica etcd StatefulSet, rows in Postgres — and the symptom of a lost
reminder is SILENCE: rows that no longer have anything scheduled to collect them, forever, with
nothing reporting it. A central reconciling controller (Argo's model) re-derives its work list every
loop and cannot lose an item this way; a reminder-driven actor can. `DaprWorkflowHistoryNotCollected`
is the only thing in the estate that would notice.

## Sources

* Retention policy, config keys and the "newly terminal only" rule —
  `docs.dapr.io/developing-applications/building-blocks/workflow/workflow-history-retention-policy/`
* Purge semantics, terminal-state precondition, `--force` hazard —
  `docs.dapr.io/developing-applications/building-blocks/workflow/howto-manage-workflow/`
* What a workflow instance stores (metadata · `wf-history-<id>-<index>` · inbox) and what purge deletes —
  `docs.dapr.io/contributing/protocol-reference/workflow-protocol/workflow-protocol-state-and-history/`
