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
only what happened since the last pod start. The scheduler-enforced policy needs no such index.

**Retention is demonstrably working for post-policy instances.** The day distribution is a clean
natural experiment — see R1.

---

## The backlog

### R1 — 64 orphaned `ingest` instances that will never be collected

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

**Done means:** those 64 instances are gone from the state store, and a re-measure shows the earliest
`insertdate` inside the retention window.

**How.** `dapr workflow purge --app-id ingest --all-older-than 720h`. Read
`docs.dapr.io/.../howto-manage-workflow` first: purge accepts only COMPLETED / FAILED / TERMINATED
instances, and the `--force` variant is documented as risking corruption unless no instances are
active. Do not reach for a direct `DELETE` on `daprstate.state` — it bypasses the actor runtime that
owns those keys.

**Cost of leaving it:** disk only. These rows are inert — no correctness consequence, no replay
consequence. Rank it accordingly.

### R2 — nothing alerts on retention stalling

`chart/alerting/rules.yml` carries `WorkflowActivitiesFailing` and `DaprConsumerWedge`, and **no rule
on state-store growth or on retention failing to collect**. Both measurements this estate has of
workflow-history volume — 1367 on 2026-08-10 and 7239 on 2026-08-26 — happened because a person went
looking. If the policy is dropped by a values edit, or the annotation is re-gated, or the scheduler
stops collecting, the first symptom is a full disk.

**This is the item worth doing first.** It is the difference between "retention is fixed" and "we can
tell when retention breaks", and it is a handful of lines in a file that already has ten rules.

**Done means:** a vmalert rule that fires when workflow-history rows grow monotonically past a
threshold, or when the oldest row's age exceeds the longest configured retention (720h) plus a margin.
The second framing is better — it tests the *property* rather than a volume that legitimately varies
with load.

**Note a real obstacle:** the alerting chain is vmalert → GreptimeDB over PromQL, and row counts in a
Postgres state store are not a series anyone exports today. Either a small exporter or a Dapr metric
that already carries it. Check `dapr_runtime_workflow_*` before writing an exporter — that family is
already referenced in `rules.yml:83`, and its `status` label is documented there as unreliable for
this codebase, so read that comment before depending on any of it.

### U1 — five operator routes with no caller

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

### U2 — `compute/ingest` shows status and offers no control

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

### U3 — workflow history and retention are surfaced nowhere

No zone shows how much history exists, how old it is, or what the retention policy is. The one
promising grep hit — `home/src/lib/remote/policies.remote.ts` — is the **Lance table** compaction and
retention plane (the maintenance surface), which is a different thing entirely.

Whether this deserves a surface at all is a genuine question, not an obvious yes: it is an operator
concern, and the estate's answer to operator concerns elsewhere is an alert (R2), not a page. **Do R2
first and re-ask this afterwards** — an alert may make the page unnecessary.

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

`count(expiredate) = 0` across the whole table is **expected and not a symptom**: the Dapr scheduler
enforces retention by deleting rows, not by writing a TTL column. Judge collection by the age
distribution, never by that count — reading it as a failure signal is the mistake this note prevents.

## Sources

* Retention policy, config keys and the "newly terminal only" rule —
  `docs.dapr.io/developing-applications/building-blocks/workflow/workflow-history-retention-policy/`
* Purge semantics, terminal-state precondition, `--force` hazard —
  `docs.dapr.io/developing-applications/building-blocks/workflow/howto-manage-workflow/`
* What a workflow instance stores (metadata · `wf-history-<id>-<index>` · inbox) and what purge deletes —
  `docs.dapr.io/contributing/protocol-reference/workflow-protocol/workflow-protocol-state-and-history/`
