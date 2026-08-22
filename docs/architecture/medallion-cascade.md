# The medallion cascade — settled decisions

Migrated from `open_medallion_workflow.md` on 2026-08-22, when that working plan was retired. A root
`open_*.md` exists only while work is outstanding; these three sections were never outstanding work —
they are rulings, and two of them say so in their own titles. They live here because the questions
recur and the answers are expensive to re-derive.

**What the plan delivered, for anyone tracing the history.** Its slices S1–S4 (the submit/poll/verify
workflow, `continue_as_new`, the automatic quality split, and the human approval) and §9.1's review
band are implemented and pinned by tests under `services/medallion/tests`. S5 and S6 were audited and
owe no code — S5's defect was closed by reordering rather than by a saga, and S6 is a `movers[]`
declaration rather than a feature; both properties are pinned by
`test_no_rows_without_a_catalog_record.py` and `test_a_same_tier_lane_is_legal.py`, which carry the
full reasoning. §9.2's `lance-ray` rename is an ops scheduling item with no design work owed: it needs
one coordinated rollout because the actor state store cannot hot-reload, and that cost is the same
whenever it happens.

**The three rulings follow verbatim.** They are reproduced rather than summarised because each one was
raised as a defect and closed as correct, and a summary is exactly the form in which that gets
re-litigated.

---

## 10. DECIDED — the two cascade heads are distinct events, and both must fire

Raised as a defect ("a table emitting both signals cascades twice") and closed as **correct behaviour**
after reading what the two heads actually publish. Recorded here rather than in the plan doc that raised
it, because medallion owns this and that doc is scheduled for deletion.

**The claim.** `/bronze-arrival` (`ingest_trigger.py`) and `/publication-arrival`
(`publication_trigger.py`) both publish `medallion.bronze`, and they derive the correlation token from
different sources — `ingest_trigger.py:112` from the bronze-write run's `lance.token` facet,
`publication_trigger.py:103` from the control event's `event_id`. Different tokens produce different
`stage_submission_id`, hence different workflow `instance_id`, so the deterministic-instance dedupe in
`transform.py` never engages between them. Both cascades run.

**Why that is right.** The two triggers do not describe the same work:

| | `/bronze-arrival` | `/publication-arrival` |
| --- | --- | --- |
| fires on | a bronze WRITE reaching COMPLETE | a table being PUBLISHED (`table_published`) |
| `dataset` | the dataset actually written — the events lane's `bronze_dataset`, project-qualified | `bronze$<table>` — the published table |
| range | none; the arrival IS the unit | carries `from_version` / `to_version` (D-R3) |

A version RANGE is a concept the ingest head does not have, and the datasets differ. Unifying the token
would therefore do the opposite of fixing something: it would collide two legitimate cascades onto one
`instance_id`, and Dapr would answer the second `schedule_new_workflow` as a duplicate — silently
dropping one of two pieces of work that must both happen. It would also conflate two distinct cascades
in tracing, which is what the token exists to keep apart.

**Explicitly NOT the fix: token de-duplication at the movers.** An adversarial review found that key is
not unique per legitimate message on the mover's own topic — deploying it halts every distributed
cascade, because the mover deliberately receives more than one message per token.

**What would change this.** If a future head publishes a trigger whose `dataset` AND range are identical
to another's, that IS a duplicate and the instance_id will correctly dedupe it. The property to preserve
is that the token distinguishes *events*, while `stage_submission_id` distinguishes *work* — they are
different questions and must not be merged.

---

## 11. DROPPED — the HTTP heads' "missing durable obligation-carrier" is the caller-retry contract

An atomicity audit listed the three HTTP-initiated heads — `/ingest-media`, `/produce`, `/train` — as
having no durable carrier for the trigger they publish after committing, so a pod death in between
would strand healthy data with the cascade stopped. Recorded here because medallion owns these heads.

**The premise is false, and the design is deliberate.** `media_produce.py` states it outright:

> "The media-chain TRIGGER below stays a bare publish on purpose: the outbox re-ingests lineage, it
> never re-fires triggers — trigger loss is the documented idempotency-token caller-retry contract."

`produce.py` then documents the contract itself: the route's 503 tells the caller to retry; a retry that
minted a FRESH token would double-fire the head as two unrelated runs, so the key is REUSED; every
downstream `run_id` derives from it, so the graph MERGEs the duplicate and the overwrite-writes land the
same data. `train.py` carries the same key-reuse contract. The audit comment in `media_produce.py` even
separates the two failure domains: a failed EMIT means no run landed (a retry re-ingests, no duplicate
possible), while a failed TRIGGER after a landed emit still 503s, and the retry emits for a NEW bronze
version — so every COMPLETE in the graph maps to a real committed write rather than a duplicated one.

**What would actually change it is an API decision, not a durability fix.** The caller IS the transaction
boundary for a synchronous head. Making the obligation durable server-side means accepting the request,
persisting the intent, and returning 202 — turning three synchronous 503-retry heads into asynchronous
accept-and-report heads. That is a contract change for every caller, and it is not what "add a durable
carrier" sounds like.

**The residual risk, stated plainly:** a caller that does not retry strands the work. That is inherent to
the synchronous shape and is why the idempotency key is documented as a *skill rule* — "an operation
whose route invites retry must pair it with one". If the estate ever wants the heads to survive a caller
that gives up, the change to make is 202-with-persisted-intent, decided as an API change.

---

## 12. REVIEWED 2026-08-16 — `stage_run` is idiomatic; its operator surface is event-driven by design

The Diagrid `review-workflow-{determinism,activity,management}` checklists, run over
`services/medallion`. Recorded here because this doc is the cascade's design record and §3's monitor
shape is exactly what the determinism rules grade.

### Determinism: zero findings, and §3's shape is why

`stage_run` (`workflow.py:131-224`) passes all fifteen `DWF-DET-*` rules. The three things that
usually fail this review are absent for reasons this doc already argued:

* **No unbounded loop (`DWF-DET-013`).** The poll loop is one poll per turn plus `continue_as_new`
  (`workflow.py:157-160`) — the Monitor pattern §5 costed. The comment records what it replaced: the
  earlier bounded loop was never the literal `while True` anti-pattern, but its bound *was* the
  history bound, 2880 polls × 30 s ≈ 5,760 events replayed from the start on every continuation.
* **No logging leak (`DWF-DET-012`).** Every one of the five `log.*` calls in workflow scope is
  guarded by `if not ctx.is_replaying` (:151, :176, :191, :198, :219). A line-based scan flags all
  five; each is a false positive, and the guard is the documented fix.
* **No clock and no env read.** The poll interval and ceiling ride `StageJobSpec`, and
  `_is_terminal` (`workflow.py:421-426`) is deliberately a pure comparison over two literals so the
  workflow module has no import-time behaviour — with the literals test-pinned against `ray_kit`'s so
  the duplication cannot drift silently.

**CORRECTION 2026-08-22 — this section's management verdict rested on a distinction that did not
hold.** The sign-off below justifies having no workflow management surface with *"its three exits are
already distinguished (`succeeded` / `abandoned` / `unnotified`)"*. In any deployed estate **only
`abandoned` ever fired**: `submit_stage` re-derived the submission id without `code`, while
`submit_stage_job` posted it with `code` (`MEDALLION_RAY_CODE_VERSION` is rendered on every mover,
`chart/templates/medallion.yaml:383`, outside the `medallion.ray` guard). The poll 404'd, `job_status`
answered `None`, and `stage_run` took the `abandoned` branch on its FIRST poll — a fabricated FAIL over
a job that was writing its data correctly.

Be precise about the scope of the error. This review's determinism findings stand, and the
terminal-state literal pin it cites is real and holds. What was missed is a **different** duplication —
the submission-id derivation — whose own pin (`services/medallion/tests/test_stage_workflow.py`)
re-derived the id inside its fake and so reproduced the defect on both sides of the assertion. The
section is not wrong about what it pinned; it reads as though both duplications were covered.

**Fixed 2026-08-22:** `submit_stage_job` now returns the id it posted and `submit_stage` returns that
value, deleting the second derivation site entirely — the only shape a later axis cannot re-break. The
test now asserts the posted id rather than a re-derived one. Diagnosis: `open_ray_otel.md` §1.

**The `continue_as_new` carry is the subtle part and it is already correct.** `submission_id` and
`polls_done` ride the spec (`workflow.py:108-114`) because each turn starts with empty history: without
them a turn has no memory that `submit_stage` ran, and would resubmit the same stage job once per poll
interval forever, each overwriting the same output dataset, never reaching the ceiling because the
count restarted at zero. Anything added to this workflow that must survive a turn goes in that spec.

### Management: there is no HTTP surface, and that is the design

Unlike ingest — whose missing `terminate` route is a live critical, recorded at
`open_ingest_design.md` §6 — medallion exposes **no workflow management endpoints**, and none of
`DWF-MGT-001`…`015` fires as a defect. The plane is trigger-driven: `transform.py:120` schedules
`stage_run` from a pub/sub trigger with a deterministic `instance_id`, so Dapr's duplicate-instance
answer *is* the dedupe (:94), and `transform.py:146` reads state back through `get_workflow_state`.
There is no door for a human to call, so there is no door to add lifecycle control to.

**Why that is acceptable here and not in ingest, stated so the asymmetry is deliberate:** `stage_run`
is bounded by `max_polls` on every path and terminates itself, and its three exits are already
distinguished (`succeeded` / `abandoned` / `unnotified`, §9's vocabulary).

> **CORRECTION, added when this was migrated (2026-08-22).** That last clause did not hold on the day
> it was written, and the sign-off it supports was therefore standing on luck rather than on the
> reasoning given. A separate audit (`open_ray_otel.md` §1) measured that in a deployed estate
> **`abandoned` was the only exit that ever fired**: the watcher polled a submission id the submitter
> had never posted, because the id was derived twice and the two derivations diverged, so every status
> read came back empty and every run abandoned at the ceiling. A distinction that never materialises
> cannot justify declining an operator surface.
>
> It holds NOW. `519ea5c4` made `submit_stage_job` RETURN the id it actually posted and deleted the
> second derivation site, so the watcher polls the job that exists and the three exits are reachable
> as described. The conclusion — no HTTP management surface — stands; the premise had to be repaired
> first. Kept rather than quietly rewritten because "the design record certified an operator surface
> on a distinction that never held" is the more useful thing for the next reader to know. An ingest run has no such
self-limit — its `max_run_hours` default is 0 = unbounded in code. Medallion's watcher cannot run away;
ingest's harvest can.

**What would change this:** a stage job that must be *cancelled* rather than waited out — an operator
who knows the Ray job is wrong and wants the watcher to stop rather than abandon at the ceiling. That
needs both a `terminate_workflow` call **and** a Ray-side `stop_job`, since terminating the watcher
leaves the job running. Not needed today; if it is ever wanted, it is one decision covering both halves,
not a route.

### Carried over from ingest's review, same verdict

**`DWF-ACT-009` (warning) applies identically:** all four activities (`submit_stage`, `poll_stage`,
`publish_stage_ready`, `report_stage_outcome`) sign as `dict[str, Any]` while 1.18 makes Pydantic the
first-class payload type. The models exist and every body opens with `model_validate`, so validation is
present but one line inside the function rather than at the boundary. Same recommendation: fold it into
a change that already touches these signatures — S3's quality-gate wiring is the natural window — rather
than churning the file for it alone. `DWF-ACT-008` (naming suffix) is declined for the same
single-language reason.
