# open_atomicity — where events can be lost, and what to do about it

Two adversarial audits (2026-08-15) mapped every state-write-then-publish site in the estate, checked the
position against Dapr's own documentation and the lakehouse literature, and then attacked the conclusions.
**Two of the three recommendations died.** What follows is what survived, with the refutations kept in
place — the dead options are the most useful part of this document, because each is the obvious idea.

---

## 0. The fact that bounds everything

**Atomicity between object storage and a message broker does not exist.** There is no distributed
transaction spanning "commit a Lance dataset to S3" and "publish to NATS" — not in Dapr, not anywhere. So
the goal is not atomicity. The goal is **no silent loss**: every gap either closes itself or announces
itself.

---

## 1. Dapr's transactional outbox is unavailable here, by construction

Not a missing config. Dapr's outbox is a property **of a Dapr state store**: it publishes the message inside
the transaction Dapr is already running for you, which requires the write to go through Dapr's transactions
API into a Dapr transactional state store. The docs scope it exactly that way, and add that Dapr's
guarantees stop at its own API boundary — *"direct queries of the state store are not governed by Dapr
concurrency control … Writes should be done via the Dapr state management or actors APIs."*

rask's writes do not go through that API:

| Write | Performed by | Why not Dapr |
| --- | --- | --- |
| Lance datasets on S3 | the Lance library | Dapr has no building block that can write a Lance dataset. |
| `public.lineage_events` + the AGE graph | lineage's own `psycopg` pool (`core/age.py:21`) | needs `bigserial`, `jsonb`, and `WHERE seq < %s ORDER BY seq DESC` paging. Dapr's state store is key/value. |
| Inbox rows, watch index, reconciler cursor | **Dapr** (`/v1.0/state/…`) | key/value shaped — Dapr fits, and does perform these. |

No state store in the estate carries `outboxPublishPubsub`/`outboxPublishTopic` (grep is empty across
`chart/` and `.docker/`). Turning it on would change nothing, because Dapr is never asked to do the writes
that matter.

**rask's own object-store outbox (`service_kit.lakehouse.outbox`) is the correct application-side
substitute**, and the Dapr docs offer no other. Two corrections to how it is described, though:

* **Stop calling it "transactional".** It is not the Dapr transactional outbox and shares none of its
  guarantees.
* **It shrinks the window, it does not close it.** `stage_event` runs AFTER the Lance commit, so a crash in
  the commit→stage gap still loses the event. That ordering is deliberate — staging after the commit means
  every surviving object is a real committed write, so there are no phantom events. The trade bought
  *no phantoms* and paid with *a small gap*.

**Long-term direction:** the honest form is a commit-EMBEDDED record — Delta CDF commits change data inside
the transaction log, and Lance already writes a per-commit transaction file. An event derived from the
commit cannot diverge from it. Today's staged object is a defensible interim, provided the docs stop
over-claiming.

---

## 2. REFUTED — "move cascade triggers behind Dapr Workflow"

The idea: triggers are obligations, so persist them in a workflow and retry until delivered.

**Why it fails.** Dapr Workflow's durability begins only once the schedule call **returns**. So
`write → start workflow` carries the *identical* crash window as `write → publish` — it relocates the gap,
it never closes it. Dapr's docs nowhere sanction Workflow as an outbox substitute for a non-Dapr write, and
the architecture page warns Workflow *"may not be appropriate for latency-sensitive workloads."* Workflow
also retries a poison step **forever** with no documented dead-letter, whereas pub/sub has TTL and DLQ — so
consolidating onto it trades a dead-letter surface for an unbounded-retry surface.

Workflow is right for durable multi-step work with compensation. It is not a trigger-durability layer.

**What survives, narrowed:** the **HTTP-initiated heads** genuinely have no durable obligation-carrier —
`/ingest-media` (`media_produce.py:117-161`), `/produce` (`produce.py:120`), `/train` (`train.py:170`) all
return to the caller and rely on caller retry, so a pod death between the Lance commit and the trigger
strands healthy data. The bus-driven sites already have a cheaper carrier. Fix the heads only.

---

## 3. REFUTED — "collapse notifications to one ingress"

The idea: the bus is incomplete (it never carries HTTP-emitted runs, because lineage's ingest door stores
and never republishes — verified, `ingest.py:45`, and `grep dapr_publish` across the service returns zero).
The durable feed is complete. So delete the bus subscription and keep the feed walk.

**Why it fails, and this was reproduced rather than argued.** The feed is **not** complete — it is a
GOVERNED read. `/events` filters every row on visibility over `set(inputs) | set(outputs)`
(`runs.py:117-123`), while `consumer.py:52` persists inputs as **bare names**, so the read path cannot apply
the external-source exemption. A reproduction showed `governed()` returning `[]` for a bronze-head event
whose governed output the caller *can* read, because the external-source input resolves to an unparented
`table:` object that is reader-for-nobody.

**The feed structurally drops exactly the events the collapse was meant to preserve.** This is the untreated
read-path twin of a write-path bug already fixed.

### Replacement doctrine

> **One idempotent handler, N triggers.** The durable feed is authoritative only where it is provably
> complete; the bus is a latency optimisation AND a completeness backstop where the feed is governed. The
> reconciler runs unconditionally on a timer — never as an emergency mode. Instrument which lane wins each
> event; retire a lane only on that evidence.

Concretely: **keep both lanes**, add a per-lane *first-seen* attribute recording which lane won the race for
each `notification_id`, and retire a lane only if it contributes zero first-sees across a window that
includes a lineage restart. Keep `test_ingress_dedupe.py` — it is the only proof that
`notification_id = run_id@STATE` is idempotent under two-source arrival, and that property is required the
moment any second source exists.

---

## 4. The work, in the order the audit corrected it to

Ordering matters here: two of these are prerequisites for a third that would otherwise enforce a rule whose
implementation can lose data.

**(a) Reshape the publish guard — do this first.**
`tests/unit/test_invariants.py` greps for the literal `topic_name=settings.lineage_topic`. Every one of the
ten `dapr_publish.publish_event(` sites in `services/` names its topic through a variable (`self._topic`,
`settings.pub_topic`, …), so the guard **cannot match any call site that exists**. It is a test that can
only pass. A better regex is not the fix — both lineage offenders take the topic as a constructor argument,
so no pattern over the call site can resolve it. Make it a **declare-your-intent** guard: every publish site
must be classified, and an unclassified one fails until a human says what it is.

**(b) Close the forwarding defect class.**
`reconcile()` accepted `watchers`/`push` and forwarded neither, so the feed lane silently notified authors
only and sent no email or Slack — for exactly the producers the bus never carries. Fixed, but the *class*
is still open: both are optional parameters, so any future lane can omit them silently. Require every
production caller of `ingest_run_event` to pass both.

**(c) Fix the outbox object key — before widening coverage.**
`stage_event` writes `<outbox_uri>/<run_id>.json` while `build_run_event` excludes event_type from the
run_id, so a COMPLETE and a FAIL for one run **share one object**. `transform.py:508-516` documents this
having destroyed a staged COMPLETE. Widening the producer set first would spread a lossy implementation.

**(d) Two read-path bugs found in passing.**
* `/events` drops external-input events for **every** caller under FGA, not just notifications. Fix:
  persist the input NAMESPACE in `public.lineage_events` and apply `is_external_source` on the read path.
* The media head's namespace is non-conforming: `is_external_source` is literally `'://' in namespace`
  (`fga_deps.py:141`), but `media_produce.py:108` emits `inputs=[('source', uri) …]` — a bare string.

**(e) A poison-row escape for the reconciler.**
`reconcile()` advances the cursor only on a fully clean pass, with no attempt counter and no park-and-skip,
so one permanently failing recipient pins the cursor and blocks every newer notification indefinitely.

**(f) Only now, widen outbox coverage** to catalog (`lineage_emit.py:467`) and maintenance
(`lineage_emit.py:224`) — the two bare lineage publishes.
**Do not** add "raise when the outbox URI is unset": both emitters swallow every exception by contract
(`catalog/…/lineage_emit.py:475`, `maintenance/…/lineage_emit.py:232`), so a raise would become 100% silent
in precisely the two services being added. De-prioritise maintenance's compaction event specifically — it
is deliberately versionless, mints no logical data, and is excluded from reconcile's `latest_write_version`.

---

## 5. Inconsistencies worth naming

Sixteen were found; these are the ones that describe the estate rather than one bug.

* **Three services publish `lineage.events.v1`; only one stages.** medallion stages; catalog and
  maintenance publish bare.
* **Publish-failure posture differs three ways for the same class of event** — medallion returns
  `publish_failed` → 503 + Retry-After; catalog logs at WARN; maintenance swallows.
* **Durability is config-removable.** `MEDALLION_LINEAGE_OUTBOX_URI` defaults to `""` in code and
  `outbox.py:147` degrades to a plain publish when unset, so every "compensation: outbox" becomes "none"
  outside the chart's own values.
* **The cascade head is bypassable.** `publication.py::publish_table` emits `table_published`, but
  `tags.py::update_table_tag` can move the same `published` ref directly and emits nothing.
* **Two heads, one trigger topic, no dedupe.** `/bronze-arrival` and `/publication-arrival` both publish
  `medallion.bronze`; the movers carry no token de-duplication, so a table emitting both cascades twice.
* **The outbox drain destroys the last copy on a swallowed failure** — `reconcile_cron.py:195-196` and
  `dlq.py:135-136` call `record_event_best_effort` and then `drop_event` unconditionally.
* **An available one-transaction fix was never taken.** `ingest_event` already opens `conn.transaction()`
  (`repository.py:455`), but `record_event` opens its OWN pooled connection, so the graph write and the feed
  row are not atomic with each other — and they could be.
* **`flows` emits nothing at all** — no publishes, no lineage, no `RASK_LINEAGE_*` env.

---

## STATUS (2026-08-15) — all eleven items resolved

Worked under a `/goal` requiring, per item: the defect proven RED first, the fix, the test green, the
full suite pasted at >= 4349 with zero failures, `ty` + `ruff` clean, and a named-path commit pushed.

| # | Item | Verdict | Commit |
| --- | --- | --- | --- |
| 1 | reminder failure policy | DONE | `a413d9ee` |
| 2 | digest one-shot | DONE | `4b468f1a` |
| 3 | single-flight lock -> actor | **DROPPED** + guard shipped | `fd63cc9f` |
| 4 | tenacity -> Dapr Resiliency | DONE | `01dde164` |
| 5 | duplicate cascade trigger | **BLOCKED** | — |
| 6 | train completion watcher | **DROPPED** | — |
| 7 | outbox key collision | DONE | `7142dfee` |
| 8 | catalog + maintenance bare publishes | **BLOCKED** | — |
| 9 | `/events` external-source drop | DONE | `c6719caa` |
| 10 | media head's namespace | DONE | `b2b4944f` |
| 11 | reconciler poison-row escape | DONE | `f26219ee` |

### Why three did not land

**3 — DROPPED.** Not a live defect: notifications is `replicas: 1` in values.yaml, absent from
values-prod.yaml's HA list, and 1 in the live cluster. The fix would also invert the semantics it was
meant to preserve — Dapr actors are turn-based, so they QUEUE, while `reconcile_cron` argues
"Skipping is the right answer rather than queueing". The estate had already ruled identically for
medallion's movers. What WAS wrong is that medallion's single-replica constraint is enforced by a value
and this one was true by accident; that is now a test.

**5 — BLOCKED, mechanism located.** The two heads derive the correlation token from different sources:
`ingest_trigger.py:112` uses the bronze-write event's `lance.token` run facet, `publication_trigger.py:103`
uses the control event's `event_id`. Same table, different tokens, different `stage_submission_id`,
different `instance_id` — so the workflow dedupe that already exists never engages. Making them share a
token asserts that "table ingested" and "table published" are the SAME work, which is a medallion
semantics decision. A prior adversarial review of this exact change also concluded that de-duplicating
on that key "halts every distributed cascade".

**6 — DROPPED.** The premise is false. `scripts/ray_train_job.py:4`: the training job "emits OpenLineage
`START -> RUNNING(progress) -> COMPLETE|FAIL` itself". Completion IS recorded, does reach the durable
feed and does notify. A Workflow monitor would emit a SECOND COMPLETE for the same run. S1's watcher
exists because the stage mover must MEASURE the output and continue the cascade; training has no
downstream stage waiting on it.

**8 — BLOCKED on a latency decision.** Neither catalog nor maintenance has `outbox_uri` config, so this
needs new settings and chart wiring in both. More importantly, catalog's emit is explicitly "the
inline-awaited emit on the create/write request path": staging to S3 first adds an object-store write
to EVERY catalog table create/write, on a user-facing request. That trade needs deciding, not
assuming. The audit itself de-prioritised the maintenance half ("a compaction mints no logical data").
Item 7 has removed the blocker that made this dangerous to attempt.
