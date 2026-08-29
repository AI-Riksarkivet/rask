---
name: rask-notifications
description: How a feature gets a person told — the targeted inbox behind the estate's bell. The six targeting sources (AUTHOR/ORIGINATOR/WATCH/GRANT_ADDED/GRANT_REVOKED/TASK_ASSIGNED+TASK_UNASSIGNED), the producer contract for the lineage and control lanes, and the four silent-drop traps that make an emitted event reach nobody. Use when adding a feature whose outcome a person should hear about; when a notification "should have fired" and did not; when adding a `ControlAction` or a `NotificationReason`; when emitting an OpenLineage run event from any service; or when wiring a new service into `lineage.events.v1` / `catalog.control.v1`.
---

# rask notifications — how a feature gets a person told

**A notification names a PERSON and a REASON. A feed names an estate.** `services/notifications` is an
inbox behind the bell — one Dapr `InboxActor` per subject holding claim-check pointers with durable
read state — precisely so the badge counts *your* work rather than the estate's. That makes coverage a
**targeting** problem, not a transport one: the ingresses already carry everything the estate does, and
the plane's whole job is to discard all of it except rows that name a subject under one of four reasons.

**The corollary, and the thing to internalise: a state change that names nobody is not
under-delivered, it is UNDELIVERABLE.** No amount of subscribing fixes it. Coverage is decided at the
**producer**. For the topology (ports, ingresses, chart wiring, the four values entries the service
needs in-cluster) see `rask-services-fleet`; this skill is only "what must I emit, and who hears it".

## The decision procedure

Run top to bottom. First match is your source. No match means the feature has **no audience** and needs
a producer change before it can have a notification — say that in the design rather than emitting
something adjacent.

| | Question | Source | What you emit |
| --- | --- | --- | --- |
| **Q1** | Does a named person cause it, ending in a terminal run over a governed object? | `AUTHOR` | terminal lineage event + verified `author.sub` + ≥1 output |
| **Q2** | Does a **service** run it, on a named person's behalf? | `ORIGINATOR` | the same event carrying `lance.originator` = that person's sub |
| **Q3** | Should people *other than the actor* know, because it is the project's work? | `WATCH` | the same event **plus** `lance.project` |
| **Q4** | Does it give a specific person access? | `GRANT_ADDED` | `CatalogControlEvent`, `extra.subject` = the principal |
| **Q5** | Does it take access away from a specific person? | `GRANT_REVOKED` | the same envelope, `action="grant_revoked"` |
| **Q6** | Does it hand a person work, or take work away from them? | `TASK_ASSIGNED` / `TASK_UNASSIGNED` / `TASK_CHANGES_REQUESTED` / `TASK_DROPPED` / `TASK_LEASE_EXPIRED` | the same envelope; `extra.subject` is the WORKER, never the manager who clicked |

**Q2 is the one people miss, and it exists because Q1 is unreachable for most long work.** The estate's
expensive runs — a Ray training job, a medallion stage — execute detached, hours after the request, and
post their own lineage as a service. `enforce_author` then OVERWRITES the author facet with that
service's verified sub (trap 2), so the human CANNOT be the author and must not try to be. `originator`
is the field for a run authored by a service but run FOR a person; it is a TARGETING hint that
authorizes nothing, because the plane re-derives every recipient's visibility at delivery. The worked
reference is the `/train` chain (`tests/unit/test_train_originator.py`): five links — door → trigger →
consumer → Ray submission → the job's own events — and the person is told only if every one carries it.

**Long-running jobs need the identity in TWO places, and they are not interchangeable.** Ray's own
`metadata` (`rask.originator`, returned by `GET /api/jobs/<id>`) is how an OUTSIDE observer recovers who
a job was for *after* it died — including a job that died before emitting anything. `runtime_env.env_vars`
is the job's own copy, for the events it emits itself. Put it in only one and you lose either the
self-emitted lane or the post-mortem one.

`AUTHOR` needs no registry and no opt-in — you may always be told about your own run
(`fanout.py:87`). `WATCH` is an explicit `project#member`-gated opt-in: **membership gates watching and
never implies it**, so no audience widens by default.

**Q6 needs ONE ACTION PER EDGE, not a generic one.** The panel renders the reason as each row's
user-visible label, so a distinct verb is not decoration — telling somebody their reviewed work was
"unassigned" is a worse answer than the silence it replaces. The annotator's audience always comes off
the task's **pre-turn snapshot**, never the post-transition document: the actor nulls `assignee` inside
the very turn these edges fire, so by the time `fire()` returns there is nobody left to name.
`submitted_by` is the safer field — written once and cleared by no edge — which is why it carries the
review side.

**`lease_expired` is covered, and how it got there is the reusable part.** It was recorded as
structurally impossible — "no principal, no request, no emitter in scope" — and two thirds of that was
a misreading. No principal is true and IRRELEVANT: the lane targets on `extra.subject` and never reads
`actor` (its envelope carries `system:annotator`). What was actually missing was a handle: a Dapr actor
has no `Request`, so it cannot resolve `ControlEmitterDep`. `service_kit.control_emit`'s
`set_process_control_emitter` / `process_control_emitter` pair closes that — set once in the lifespan
beside `app.state.control_emitter`, read by any non-request producer, defaulting to the NO-OP so a
service that never sets it is silent rather than broken. **Before filing an emit site as impossible,
separate "no audience" from "no handle" — only the first is structural.**

Two details that path forces, and both are general: read the audience BEFORE the transition (the actor
nulls `assignee` in the same turn, so the post-turn document names nobody), and read the object id off
the RECORD rather than `self.id.id` — identical in production, but the actor-runtime attribute does not
exist under a test double, so an emit that used it silently swallowed an `AttributeError` and asserted
nothing.

Q4 is the sharpest and is why the control lane exists at all: *losing access silently is how someone
discovers it by hitting a 403 in the middle of work*. That lane deliberately runs **no visibility
check** — after a revoke the subject can no longer see the object, so a delivery-time check would drop
the one event they most need. **Being named IS the targeting.**

## Lane 1 — lineage (`lineage.events.v1`)

Four things must ALL hold. `notifiable()` (`notifications/api/lineage_events.py:154-203`) returns
`None` — "ack it, tell nobody" — on any miss, and **that ack is a SUCCESS**, so nothing anywhere
reports the loss. This is the estate's most expensive silent failure mode.

1. **Terminal state** — `COMPLETE` / `FAIL` / `ABORT` (`TERMINAL_STATES`, `lineage_events.py:32`).
   `START`/`RUNNING` notify nobody by product decision. `RECONCILED` is excluded on purpose: it is
   lineage's REPAIR marker for a run whose real terminal event was lost, not an outcome anyone chose.
2. **A VERIFIED `author.sub`** — see the trap below. This is the one that keeps biting.
3. **`lance.project`** — optional, but omitting it costs you **every** watcher.
4. **≥1 output, named exactly as the FGA object is named.** Delivery runs `can_be_notified` and render
   re-runs `can_get_metadata` against `table:<output name>`, so an unqualified name against
   tenant-qualified grants counts every recipient HIDDEN.

```python
event = {
    "eventType": "FAIL",  # (1) TERMINAL. START/RUNNING notify nobody.
    "eventTime": datetime.now(UTC).isoformat(),
    "run": {
        "runId": str(run_uuid),  # the notification id is `runId@STATE`
        "facets": {
            # (2) The VERIFIED token sub. NOT settings.author, NOT a role/team string,
            #     NOT a display name. `{name, sub}` together is what every verifying writer stamps.
            "author": {"name": token.sub, "sub": token.sub},
            "lance": {
                "operation": "promote",
                "run_id": producer_run_id,  # what YOUR detail door answers to
                "project": project_id,  # (3) WATCH's key. Omit -> zero watchers.
            },
            "errorMessage": {"message": reason},
        },
    },
    "outputs": [{"namespace": "gold", "name": f"{project_id}-gold$catalog"}],  # (4)
}
await publish_event(client, pubsub_name="pubsub", topic_name="lineage.events.v1", data=json.dumps(event), data_content_type="application/json")
```

## Lane 2 — control (`catalog.control.v1`)

```python
await emit_control(  # best-effort; never raises into a committed mutation
    emitter,
    action="grant_revoked",  # MUST be in NAMED_ACTIONS
    object_type="project",
    object_id=f"project:{project_id}",
    actor=f"user:{token.sub}",  # the VERIFIED principal that made the change
    extra={"relation": relation, "subject": user},  # `subject` = WHO this is about, `user:bob`
)
```

Call it **after** the backend mutation and its audit succeed, so a real change is never announced.
`extra.subject` is the entire targeting: `named_subject` returns `None` for a missing subject, a bare
`user:`, and the `*` wildcard, and the event is then filed IGNORED with a `SUCCESS` ack.

**A new named action is a THREE-file change, and all three are load-bearing:**

1. `service_kit/control_events.py` — add the member to `ControlAction`, or the envelope will not validate.
2. `notifications/api/control_events.py` — add it to `NAMED_ACTIONS`, or the lane files it IGNORED.
3. `notifications/models.py` — add the matching `NotificationReason`, because `as_delivery` constructs
   `NotificationReason(event.action)` and would otherwise **raise on every delivery**.

The deeper reason for (3) is that the reason is **stored, not inferred**: a delivery re-check keys on
it, and a governance row is checked against no object rule at all, so a reader that could not tell it
apart from a run row would have to guess. Note also that `ControlAction` is a wire contract reaching
the frontend through `docs/catalog-openapi.json` — see `rask-lance-catalog` for the regen step.

**A NEW REASON IS A COMPATIBILITY SURFACE, NOT JUST AN ENUM.** The reason is *stored*, so the moment
a row carrying it lands in the actor's durable state, every build that reads that state must be able
to name it. This is not theoretical — it took an inbox down on 2026-08-16: three members were added,
rows landed, the deployment rolled back, and the older enum turned them into
`ValidationError: 4 validation errors for InboxRows` → `InboxUnreadable` → **503 for the entire
inbox**. Not one missing row: the badge went blank and every other notification that subject had
became unreachable, because list validation is all-or-nothing. The bell then fell back to the run
feed, which is indistinguishable from "no service configured".

`InboxPointer` now degrades an unnameable reason to `NotificationReason.UNKNOWN` on read, and
`LineageCursor` takes `extra="ignore"` (it is service-internal and single-writer, so nothing is being
contained). **`extra="forbid"` on `InboxPointer` deliberately stays** — an unknown *field* may be
another subject's data, which is a containment guard (`test_inbox_leak_containment`), while an
unknown *reason value* arrives on a declared field and carries nothing foreign. Those are two hazards
wearing one symptom; do not "fix" the first by relaxing the second.

Still strict and still this class, unfixed because each needs its own containment argument:
`InboxMeta`, `InboxRows`, `ChannelPrefs`. Adding a field to any of them bricks older readers of that
record. A rollback and a mixed-version rollout are both routine.

Adding a `NotificationReason` needs **no frontend change** — but not because the bell ignores it. The
panel RENDERS the reason as each row's label (verified in a browser: rows read `originator · 1m ago`
beside older `author` ones). It passes the string straight through rather than switching on a known
set, so a new member displays correctly with no TS edit. Two consequences: the value is user-visible,
so it must read as a reason a person would accept; and `notification-center.stories.svelte` is stale
(`project_watch` vs the backend's `watch`) without anything failing.

## The four traps — an event emitted is not a person told

1. **A role literal in `author.sub` reaches nobody.** `author_subject()` reads `author.sub` and
   **nothing else** — never `author.name`, never the standard `ownership` facet — because those are
   producer-supplied and honouring them would let any producer put a row in a named person's inbox.
   The medallion movers author with a chart role literal (`data_eng`/`analyst`, `chart/values.yaml`
   `medallion.movers[].author`; the producer's is `ray`), so a failed cascade addresses an inbox
   actor named `ray`. That is not a bug to fix at the mover — `enforce_author` would overwrite a
   human there anyway (trap 2). The literal is *correct* as the author; what makes the cascade
   reachable is the ORIGINATOR riding beside it, which is why every trigger payload in the chain
   re-carries it.
   **The hop that lost it was the TIER BOUNDARY, and the lesson generalises past this cascade.**
   `/produce` → `/bronze-arrival` → the mover's four FAIL emits all carried the human; the mover then
   publishes its output to the CATALOG (the tag move is what wakes the next tier), and that call
   carried `cascade_id` and dropped the person. The publication head filled the gap by deriving an
   originator from the control event's `actor` — but a mover authenticates to the catalog AS ITSELF,
   so the silver→gold trigger named `service-bronze-to-silver` and every gold failure wrote into an
   inbox actor named after a mover. **A service subject in the originator is the same defect as a role
   literal in the author, and it is worse than silence because it looks delivered.** Closed by the
   `cascade_id` shape: the mover puts the human on the publish body
   (`catalog_register.publish_stage_output`), the catalog RESOLVES it once
   (`publication.publication_originator` — a service caller's carried claim, else a human caller's own
   verified sub, else nothing) and echoes it onto `table_published`, and the head reads `extra`
   instead of guessing. The catalog owns that decision because `IDToken.service` — set by its own
   service door — is the only place the estate records "this caller was a service".
   Pinned end-to-end on delivered rows by `tests/unit/test_cascade_originator.py`.
2. **A service token SUBSTITUTES the author.** If your emit runs behind a service bearer,
   `enforce_author` (`lineage/api/fga_deps.py:96-103`) **overwrites** the facet with that service's
   sub — "never trust the request body" is doing its job, and your human is gone. Carrying the human's
   sub through your own call graph is the only fix.
3. **No `lance.project` disables WATCH silently.** `fanout.py:88` skips the watcher loop entirely when
   `project` is `None`. The catalog was the estate-wide instance of this — it stamped
   `lance = {operation, version}` and no project, so no catalog write reached a watcher anywhere.
   **Closed:** `emit_write_event` now resolves the tenant centrally via `emitter.project_for(segments[0])`
   (`catalog/core/lineage_emit.py`) rather than at its eight call sites, none of which has a project
   in scope. Two properties of that fix are the reusable part: resolve the tenant ONCE at the choke
   point (eight sites is eight chances to derive it differently), and **omit rather than sanitize** a
   project that fails `is_safe_project` — a project-less run reaches its author and no watchers, while
   a coerced one could reach the WRONG tenant's watchers, which is disclosure rather than a miss.
   The residue: `project_for` is best-effort and returns `None` on any failure, so a write whose top
   segment is not registry-bound still reaches zero watchers, silently and with a SUCCESS ack.
4. **A non-personal principal is not an address.** `user:*` (managed access) strips to a truthy `*`
   and used to write into an inbox actor literally named `*`; usersets (`team:acme#member`) still do.
   An address must identify a person.

## The FGA prerequisite

The feed is **governed**. The reconciler reads it as its own service principal, so *a deployment that
forgets the grants gets a reconciler that runs cleanly, logs success, and reconciles nothing.* There is
no retry that helps — the estate's `invokeRetry` never retries a 4xx, because a 403's answer will not
change. Symmetrically, delivery needs `can_be_notified` and render needs `can_get_metadata` on
`table:<output name>` per recipient; an FGA outage is fail-closed (RETRY), never a delivery.

So a new producer ships **two** grants: notifications' `reader` on the feed, and the recipients' grants
on the objects your outputs name. See `openfga` — `can_*` relations are never directly assignable.

## Testing with no cluster

Both audience seams are **callables, not imports**, precisely so this needs no sidecar:
`InboxOpener = Callable[[str], TypedActorProxy]` and
`WatcherLookup = Callable[[str], Awaitable[Sequence[str]]]` (`fanout.py:37-69`).
`Visibility(client=None, enabled=False)` is the FGA-off value. Drive the whole ingress through
`ingest_run_event` (`api/ingest.py`) — the identical function both lanes run, which is what makes "the
same event arriving twice lands one pointer" a property of the projection rather than a coincidence.

**Assert on the delivered rows, never on the status** — an event that names nobody returns `SUCCESS`
too, which is exactly how a coverage gap hides. `services/notifications/tests/test_ingress_audience.py`
is the worked reference.

## What is deliberately NOT notified

Rules with reasons, so they are not "fixed" back into defects:

- **Non-terminal states** — "your run started" tells the clicker nothing and is noise to everyone else.
- **Output-less runs** — a pointer names the object it is about, and there is no honest value for that
  field. Refused under FGA on AND off; one rule everywhere.
- **Unverified authors** — see trap 1. Attribution and targeting are different questions.
- **Wildcards and usersets** — a statement about everyone addresses no one.
- **Actions naming no party** — delivering every catalog mutation recreates the estate-wide feed this
  plane exists to replace.
- **Synchronous HTTP failures** — the caller already has the 4xx; an inbox row is a second copy.

## What is still uncovered — the CLASSES, not a row list

A workflow audit (2026-08-16, 8 services, three adversarial verify lenses) found the plane sound and
its **producers** thin. Its register is gone because the work it tracked is done or ruled on; what
survives is the shape of what remains, because these classes recur every time a new feature asks to
notify somebody.

1. **No identity at the door.** A producer that never captured a verified sub can never name a person,
   and nothing downstream can repair it. Closed for `/produce`, `/train`, ingest and — since
   2026-08-22 — `/ingest-media`, which was token-only and so could resolve no principal at all; it now
   shares the pinned dual-auth door (`authorize_ingest_media`, no `?project=`, because the media head's
   target is configured and authorization scope must equal write scope). Still open for
   `services/compute` (`RayJob` has no author field) and the controlplane (the Project CR carries
   `spec.team`, a literal, not a requester). See `docs/DECISIONS.md` — an emitter without an identity
   produces events the plane is *designed* to discard, which reads as coverage and is not.
2. **No principal at all.** Some transitions are caused by a TIMER, not a person: the annotator's
   `lease_expired` fires from an actor reminder with no request and no emitter in scope. The audience
   exists (the task's holder) but the emit site does not.
3. **A fact that only exists while somebody is looking.** Several failures are composed at read time
   inside an HTTP handler and never persisted or published — flows' unparsable result, ingest's
   provenance-verification defect. Nothing can notify what was never recorded.
4. **An audience that is real but unmodelled.** `services/flows` has no project and no lakehouse
   output, so even a perfect emit dies on `notifiable()`'s output rule; the controlplane keys watches
   by CR name while fan-out matches the FGA tenant id, and nothing joins the two namespaces.
5. **Steady states wearing an event's clothes.** A permanently un-granted mover, a repeating denial, a
   degraded lane — these are METRICS, and `docs/DECISIONS.md` records why lineage must not carry them.

**The line, worth re-reading before adding anything:** lineage answers *what happened to this dataset
and who produced it*; the control lane answers *what changed for this person*; metrics answer *how
often is this happening*. A fact that names a principal and touches no data is never lineage.
