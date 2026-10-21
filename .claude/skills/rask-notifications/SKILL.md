---
name: rask-notifications
description: How a feature gets a person told — the targeted inbox behind the estate's bell. The targeting sources (AUTHOR/ORIGINATOR/WATCH/GRANT_ADDED/GRANT_REVOKED/ERASURE_RELEASE_SCHEDULED), the producer contract for the lineage and control lanes, and the five silent-drop traps (an emitted event that reaches nobody, or reaches the bell and not lineage's feed). Use when adding a feature whose outcome a person should hear about; when a notification "should have fired" and did not; when adding a `ControlAction` or a `NotificationReason`; when emitting an OpenLineage run event from any service; or when wiring a new service into `lineage.events.v1` / `catalog.control.v1`.
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
| **Q6** | Does a tool hand a person work, or take it away (an annotation task, a review)? | none | nothing on the lakehouse's lanes: a tool's work queue is the tool's, announced on its own channel (ADR 0171) |
| **Q7** | Will a scheduled act take something from a person unless they act first? | `ERASURE_RELEASE_SCHEDULED` | the same envelope, one per person (the erasure obligation's requester and each table owner), when a verification settles the obligation; `extra.due_at` is the deadline (ADR 0138, ADR 0163) |

**Q2 is the one people miss, and it exists because Q1 is unreachable for most long work.** Expensive
runs execute detached, hours after the request, and post their own lineage as a service. `enforce_author`
then OVERWRITES the author facet with that service's verified sub (trap 2), so the human CANNOT be the
author and must not try to be. `originator` is the field for a run authored by a service but run FOR a
person; it is a TARGETING hint that authorizes nothing, because the plane re-derives every recipient's
visibility at delivery. Every hop between the request and the job's own events must carry it, or the
person is not told.

**A user's pipeline is its own producer** (ADR 0155). Moving data between tiers runs on the user's own
compute and orchestrator, and the lakehouse emits no run event for work it does not run: a pipeline's
runs reach people through the OpenLineage events the pipeline's job emits, targeted by their
`author.sub` (Q1), or by `lance.originator` when the job authenticates as a service (Q2). The catalog's
own COMPLETE for each commit the pipeline makes is authored as the verified caller.

Moving a table's `published` tag notifies the project's watchers (ADR 0156 C1, ADR 0161, [[LH-466]]): the catalog
emits a `publish_table` run authored by the verified caller, its `lance.project` the tenant and its output the table,
so it is delivered by WATCH like any catalog write. Lineage records it only because its bus door authorizes a
`publish_table` run on `can_update_tag` or `can_promote` rather than the writer rung (`lineage/api/fga_deps.py`): a
validator's approval demanded `can_write_data`, was refused and dead-lettered, and still rang the watcher's bell, the
class of trap 5. Lineage's `GET /events` follows forward from `since=` with the commit-event shape in
`docs/COMMIT-EVENTS.md` (C7, [[LH-472]]). Lineage is an authenticated OpenLineage intake, a commit feed and table-level
edge list kept as Lance tables in the catalog's provenance namespace, and a forwarder to a bring-your-own graph backend
(ADR 0158). It answers no graph query: a notification cannot be targeted on an upstream or downstream walk, only on what
the event itself names.

**Long-running jobs need the identity in TWO places, and they are not interchangeable.** Ray's own
`metadata` (`rask.originator`, returned by `GET /api/jobs/<id>`) is how an OUTSIDE observer recovers who
a job was for *after* it died — including a job that died before emitting anything. `runtime_env.env_vars`
is the job's own copy, for the events it emits itself. Put it in only one and you lose either the
self-emitted lane or the post-mortem one.

`AUTHOR` needs no registry and no opt-in — you may always be told about your own run
(`fanout.py:87`). `WATCH` is an explicit `project#member`-gated opt-in: **membership gates watching and
never implies it**, so no audience widens by default. **A watch covers what happens after it began.** The
`WatchIndexActor` records when each watch began, and the fan-out asks who was watching at the run's
`occurred_at`, never at delivery. Every run arrives twice, on the bus and again on the reconciler's tick, so an
audience read at delivery would hand a watch begun between the two the work from before it (measured on the
e2e-stack lane, [[XC-094]]); keyed on the run's time, both lanes name the same people.

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
   tenant-qualified grants counts every recipient HIDDEN. The output's own `lance` facet carries
   `tableUuid`/`namespaceUuid` (ADR 0173 §10, [[LH-522]]), which the pointer stores as `object_uuid` beside the name:
   the catalog and maintenance stamp them, lineage's HTTP door resolves an outside producer's names as its token, and
   `/events/projection` keeps each output's `lance` facet for the reconciler.

**One more holds before notifications is asked, and it is not `notifiable()`'s: the event is SIGNED.** Lineage verifies
the `rask_signature` of every event on the bus and in the outbox (`signing.enforce`, on by default) and refuses what
does not verify. Notifications' `/lineage-events` checks the same signature against the same signer sets for every event
`notifiable()` would deliver, as `RASK_SIGNATURE_DOORS` says: `enforce` refuses it (acked, told to nobody, counted in
`notifications_signature_refused_total`), `observe` delivers it and counts it, and `off`, the default, verifies nothing,
so an unsigned event still rings the bell for a run lineage's feed never recorded (trap 5, `docs/LINEAGE.md` § Signed events).

```python
event = {
    "eventType": "FAIL",  # (1) TERMINAL. START/RUNNING notify nobody.
    "eventTime": datetime.now(UTC).isoformat(),
    "run": {
        "runId": str(run_uuid),  # the notification id is `runId@STATE`
        "facets": {
            # (2) The VERIFIED principal key (`token.subject`, `<idp-id>~<user id>`, [[LH-063]]). NOT settings.author,
            #     NOT a role/team string, NOT a display name. `{name, sub}` together is what every verifying writer stamps.
            #     (5) binds it: a signer is its own author, so a service stamps its own signing
            #     identity and the person rides in `lance.originator` (Q2). Only the catalog signs
            #     for a person it authenticated (`on_behalf_of=`).
            "author": {"name": token.subject, "sub": token.subject},
            "lance": {
                "operation": "promote",
                "runId": producer_run_id,  # what YOUR detail door answers to; the catalog and lineage spell it so
                "project": project_id,  # (3) WATCH's key. Omit -> zero watchers.
            },
            "errorMessage": {"message": reason},
        },
    },
    "outputs": [{"namespace": "gold", "name": f"{project_id}-gold$catalog"}],  # (4)
}
# (5) SIGNED with this service's own key, through the service's one emit door
#     (`maintenance/core/lineage_emit.py` is a worked service emitter). A bare dict is refused by lineage and acked.
event = attach_signature(event, key=holder.key(), identity=holder.identity)
await publish_event(client, pubsub_name="pubsub", topic_name="lineage.events.v1", data=json.dumps(event), data_content_type="application/json")
```

## Lane 2 — control (`catalog.control.v1`)

```python
await emit_control(  # best-effort; never raises into a committed mutation
    emitter,
    action="grant_revoked",  # MUST be in NAMED_ACTIONS
    object_type="grant",
    object_id=f"project:{project_id}",
    actor=f"user:{token.subject}",  # the VERIFIED principal that made the change, `user:` and its key
    extra={"relation": relation, "subject": fga.user_object(principal)},  # WHO this is about, as the OpenFGA user it is
)
```

Call it **after** the backend mutation and its audit succeed, so a real change is never announced.
The envelope's typed `object_uuid` and `namespace_uuid` name a table or namespace object (or the one a grant or policy is
scoped to) by its uuids (ADR 0173 §10). The catalog's control emitter fills them from `object_id` after the door acts
(`catalog.api.dependencies.UuidNamingControlEmitter`); a door whose act ends the object reads them first and passes them.
`extra.subject` is the entire targeting, and it is the OpenFGA user the change names (`fga.user_object`: `user:` and the
principal key percent-encoded, or a userset). `named_subjects` decodes a `user:` id back to the principal key
(`fga.subject_of`), so a key carrying an encoded character addresses the inbox its owner's token reaches, and keeps only
a person (`is_person_subject`): a missing subject, a bare `user:`, the `*` wildcard and a service account's principal
address nobody, and the event is then filed IGNORED with a `SUCCESS` ack. A raw `f"user:{key}"` in `extra.subject` is the
defect, though the `actor` keeps that form: it addresses another inbox whenever the key carries a character the encoding
escapes.

**The emitter signs, and `make_control_emitter(sign=...)` has no default** ([[XC-078]]). A producer passes
`attach_control_signature` over its own `SigningKeyHolder` (the catalog's, `catalog/core/control_signing.py`,
declares the `user:` actor it authenticated; maintenance signs as itself), or `None` when it holds no signing
identity, which only a door verifying nothing acts on: every action has a signer role in `control_signer_role`
(the catalog or maintenance). A signer whose key is unresolved publishes and stages nothing: the event is withheld
and counted under `reason="unsigned"`, outbox or not, and the signer reports itself not ready. The catalog therefore
refuses every write it would announce while its key is unresolved, 503 code 17 before the commit
(`catalog/api/signing_gate.py`, [[XC-095]]), so none of its announcements is lost to a missing key; a producer with no such
gate loses what it emits while its key is unresolved. No relay signs: the catalog's control relay
republishes a staged event verbatim only when it verifies as signed by the catalog's own identity and retires anything
else as poison, so a staged forgery never becomes a catalog-signed event.

**Under `RASK_SIGNATURE_DOORS=enforce` a named action reaches nobody unless the role that emits it signed it.**
`service_kit.control_events.control_signer_role(action)` names that role, and the `/control-events` door
verifies the envelope's top-level `rask_signature` against the role's identities in `RASK_CONTROL_SIGNER_ROLES`, so a
valid signature from another service is refused. A person actor (`user:<sub>`) verifies only under a delegation from a
delegator, `onBehalfOf` equal to the whole actor. No action is exempt, so the lane carries only the lakehouse's own
signed events (ADR 0171). A refusal is acked and counted in
`notifications_signature_refused_total{lance_notifications_door,lance_notifications_reason}`.

**A new named action is a THREE-file change, and all three are load-bearing:**

1. `service_kit/control_events.py` — add the member to `ControlAction`, or the envelope will not validate, and
   its case to `control_signer_role` (ty fails the `assert_never` until it has one), which names the role whose
   signature the doors require.
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

`InboxPointer`'s fields are this class too: `object_uuid` is one, so a rollback to a build without it reads every row
carrying it as unreadable, and the estate is rebuilt rather than rolled back past it.

Still strict and still this class, unfixed because each needs its own containment argument:
`InboxMeta`, `InboxRows`, `ChannelPrefs`. Adding a field to any of them bricks older readers of that
record. A rollback and a mixed-version rollout are both routine.

Adding a `NotificationReason` needs **no frontend change** — but not because the bell ignores it. The
panel RENDERS the reason as each row's label (verified in a browser: rows read `originator · 1m ago`
beside older `author` ones). It passes the string straight through rather than switching on a known
set, so a new member displays correctly with no TS edit. Two consequences: the value is user-visible,
so it must read as a reason a person would accept; and `notification-center.stories.svelte` is stale
(`project_watch` vs the backend's `watch`) without anything failing.

## The five traps — an event emitted is not a person told

1. **A role literal in `author.sub` reaches nobody.** `author_subject()` reads `author.sub` and
   **nothing else** — never `author.name`, never the standard `ownership` facet — because those are
   producer-supplied and honouring them would let any producer put a row in a named person's inbox.
   A service that authors with a role literal or its own identity addresses an inbox actor of that
   name; the literal is *correct* as the author, and what reaches the person is the ORIGINATOR riding
   beside it, so every hop that carries the work re-carries it.
   **A service subject in the originator is the same defect as a role literal in the author, and it is
   worse than silence because it looks delivered.** A service authenticates to the catalog AS ITSELF,
   so an originator derived from a control event's `actor` names the service. The catalog RESOLVES the
   originator of a publication once (`publication.publication_originator` — a service caller's carried
   claim, else a human caller's own verified sub, else nothing) and echoes it onto `table_published`, so
   a reader takes `extra` instead of guessing. The catalog owns that decision because its door is the one
   that verified the caller: a `ServicePrincipal` (a projected service-account token, [[LH-220]]) is a
   service, and an IdP token is a person whatever claims it carries.
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
5. **An unsigned event, or one from a publisher lineage does not list, is recorded by nobody, and it rings the bell
   unless notifications' door enforces.**
   Lineage verifies every bus and outbox event against the keys its signer publishes and refuses one with no
   signature, an identity outside `LINEAGE_SIGNERS`, a signer that is not its stamped author (only the catalog signs
   for a person), or bytes that do not verify. The refusal is an ACK counted in `lineage_signature_refused_total{reason}`:
   never retried, parked or recorded, and the only alert excludes `unsigned` and `signer`. Notifications' signature door
   (`notifications/api/signature_door.py`) applies the same check at `/lineage-events` and refuses the event only under
   `RASK_SIGNATURE_DOORS=enforce`; under `observe` or `off` the person is told about a run lineage's feed does not hold.
   **A new lineage-lane producer is a
   `lance.signers` entry in the chart** (it mints the pair, sets `RASK_SIGNING_IDENTITY` and lists the identity in
   `LINEAGE_SIGNERS`) that calls `attach_signature` on every event it emits and emits nothing while its key is
   unresolved (`SigningKeyUnavailableError`). Worked reference: `services/maintenance/src/maintenance/core/lineage_emit.py`.

## The FGA prerequisite

The reconciler reads lineage's `GET /events/projection` as its own service principal ([[CTL-021]]): the
whole run feed with no per-dataset filter, gated on the estate rung `can_read_event_feed`, which the
chart's bootstrap hook grants it as `event_reader` on `estate:rask`. Not the governed `/events`: that
shows a service only the tables its grants reach, so every tenant warehouse's runs would be invisible to
the lane and the tick would still log success. Each row carries only the targeting view (run id, state,
time, the `author` and `lance` facets, output names), because addressing people needs nothing more. A
deployment that forgets the grant gets a 403 on every tick, and no retry helps — the sidecar's
`invokeRetry` (`<release>-invocation-resiliency`, scoped to `notifications`, targeting `lineage`) retries
`408,429,500-599` and never a 4xx, because a 403's answer will not change. Delivery needs
`can_be_notified` and render needs `can_get_metadata` on `table:<output name>` per recipient; an FGA
outage is fail-closed (RETRY), never a delivery.

So a new producer ships **one** grant: the recipients' grants on the objects its outputs name. The feed
reaches the reconciler whichever warehouse the run wrote. See `openfga` — `can_*` relations are never
directly assignable.

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
   and nothing downstream can repair it. Closed for every write through the catalog's namespace doors, whose event the catalog authors as the
   verified caller (data enters the lakehouse only there, ADR 0154). Still open for
   `services/compute` (`RayJob` has no author field). See `docs/adr/0042-the-compute-service-gets-no-emitter-yet-and-the-blocker-is.md` — an emitter without an identity
   produces events the plane is *designed* to discard, which reads as coverage and is not.
2. **No principal at all.** Some transitions are caused by a TIMER, not a person. The audience usually
   exists, but a timer's emit site has no `Request` to resolve `ControlEmitterDep` from, so it must be
   given a handle to an emitter. Before filing such an emit site as impossible, separate "no audience"
   from "no handle": only the first is structural.
3. **A fact that only exists while somebody is looking.** Several failures are composed at read time
   inside an HTTP handler and never persisted or published — flows' unparsable result. Nothing can notify what was never recorded.
4. **An audience that is real but unmodelled.** `services/flows` has no project and no lakehouse
   output, so even a perfect emit dies on `notifiable()`'s output rule.
5. **Steady states wearing an event's clothes.** A permanently un-granted service, a repeating denial, a
   degraded lane — these are METRICS, and `docs/adr/0039-lineage-records-what-happened-to-data-an-authorization.md` records why lineage must not carry them.

**The line, worth re-reading before adding anything:** lineage answers *what happened to this dataset
and who produced it*; the control lane answers *what changed for this person*; metrics answer *how
often is this happening*. A fact that names a principal and touches no data is never lineage.
