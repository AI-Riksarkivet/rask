---
name: rask-notifications
description: The producer's side of the targeting contract the lakehouse's events publish for any notifier (docs/COMMIT-EVENTS.md § Telling people) — what a feature must emit so a person CAN be told. The decision procedure (AUTHOR/ORIGINATOR/WATCH/GRANT_ADDED/GRANT_REVOKED/ERASURE_RELEASE_SCHEDULED), each lane's producer requirements, and the five producer traps (an emitted event that names nobody, or that lineage's feed refuses). Use when adding a feature whose outcome a person should hear about; when someone "should have been told" and was not; when adding a `ControlAction`; when emitting an OpenLineage run event from any service; or when wiring a new producer into `lineage.events.v1` / `catalog.control.v1`.
---

# rask notifications — what a producer must emit so a person can be told

**The lakehouse emits; a notifier delivers** (ADR 0190, ADR 0193). The lakehouse runs no notifier, and this repository
hosts none: the console's bell is a browser-side log of the reader's own writes, never a feed of what the lakehouse did.
The lakehouse publishes its events, the fields in them that name whom each is about, and the verbs a notifier asks
before it tells anyone (`can_be_notified` through the catalog's decision door, the whole run feed at
`GET /events/projection` for an `event_reader`). That contract lives in `docs/COMMIT-EVENTS.md` § Telling people, and a
client reads it from there, importing nothing of the lakehouse's; inside the lakehouse its producers and doors share
one definition of it (`lakehouse.control_events`, `lakehouse.lineage_events`, `lakehouse.governed.subjects`,
`lakehouse.signature_doors`). This skill is the producer's side of it.

**A notification names a PERSON and a REASON; a feed names an estate.** A notifier's whole job is to discard everything
except events that name a person under one of six reasons. So **a state change that names nobody is not
under-delivered, it is UNDELIVERABLE**: no subscriber can repair it. Coverage is decided at the **producer**, here.

## The decision procedure

Run top to bottom. First match is your source. No match means the feature has **no audience** and needs a producer
change before anyone can be told: say that in the design rather than emitting something adjacent.

| | Question | Source | What you emit |
| --- | --- | --- | --- |
| **Q1** | Does a named person cause it, ending in a terminal run over a governed object? | `AUTHOR` | terminal lineage event + verified `rask_author.sub` + ≥1 output |
| **Q2** | Does a **service** run it, on a named person's behalf? | `ORIGINATOR` | the same event carrying `rask_lance.originator` = that person's sub |
| **Q3** | Should people *other than the actor* know, because it is the project's work? | `WATCH` | the same event **plus** `rask_lance.project` |
| **Q4** | Does it give a specific person access? | `GRANT_ADDED` | `CatalogControlEvent`, `extra.subject` = the principal |
| **Q5** | Does it take access away from a specific person? | `GRANT_REVOKED` | the same envelope, `action="grant_revoked"` |
| **Q6** | Does a tool hand a person work, or take it away (an annotation task, a review)? | none | nothing on the lakehouse's lanes: a tool's work queue is the tool's, announced on its own channel (ADR 0171) |
| **Q7** | Will a scheduled act take something from a person unless they act first? | `ERASURE_RELEASE_SCHEDULED` | the same envelope, one per person (the erasure obligation's requester and each table owner), when a verification settles the obligation; `extra.due_at` is the deadline (ADR 0138, ADR 0163) |

**Q2 is the one people miss, and it exists because Q1 is unreachable for most long work.** Expensive runs execute
detached, hours after the request, and post their own lineage as a service. `enforce_author` then OVERWRITES the
author facet with that service's verified sub (trap 2), so the human CANNOT be the author and must not try to be.
`originator` is the field for a run authored by a service but run FOR a person; it is a TARGETING hint that authorizes
nothing, because a notifier re-derives every recipient's visibility at delivery. Every hop between the request and the
job's own events must carry it, or the person is not told.

**Long-running jobs need the identity in TWO places, and they are not interchangeable.** A detached job that emits its
own events carries the person in `rask_lance.originator` on every event, and in whatever record its own orchestrator
keeps, so a job that dies before emitting can still be traced. Put it in only one and you lose either the self-emitted
lane or the post-mortem one.

**A user's pipeline is its own producer** (ADR 0155). The lakehouse emits no run event for work it does not run: a
pipeline's runs reach people through the OpenLineage events its job emits, targeted by their `rask_author.sub` (Q1),
or by `rask_lance.originator` when the job authenticates as a service (Q2). The catalog's own COMPLETE for each commit
the pipeline makes is authored as the verified caller.

**Moving a table's `published` tag is a WATCH event** (ADR 0156 C1, ADR 0161, [[LH-466]]): the catalog emits a
`publish_table` run authored by the verified caller, its `rask_lance.project` the tenant and its output the table.
Lineage's bus doors record it on the catalog's signature, as they record every announcement the catalog signs for the
caller it authenticated ([[LH-551]]); its HTTP door, which verifies no signature, authorizes a `publish_table` run on
`can_update_tag` or `can_promote` rather than the writer rung (`lineage/api/fga_deps.py`). Lineage answers no graph
query (ADR 0158): a person cannot be targeted on an upstream or downstream walk, only on what the event itself names.

Q4 is the sharpest and is why the control lane names its subject at all: *losing access silently is how someone
discovers it by hitting a 403 in the middle of work*. After a revoke the subject can no longer see the object, so a
notifier runs no visibility check on a control event: **being named IS the targeting**.

## Lane 1 — lineage (`lineage.events.v1`)

Four things must ALL hold, or the run names nobody. A notifier answers such an event "ack it, tell nobody", and that
ack is a SUCCESS, so nothing anywhere reports the loss: the estate's most expensive silent failure mode.

1. **Terminal state** — `COMPLETE` / `FAIL` / `ABORT` (`lakehouse.lineage.TERMINAL_STATES`). `START`/`RUNNING` end nothing.
   `RECONCILED` is lineage's REPAIR marker for a run whose real terminal event was lost, not an outcome anyone chose.
2. **A VERIFIED `rask_author.sub`** — see traps 1 and 2. This is the one that keeps biting.
3. **`rask_lance.project`** — optional, but omitting it costs you **every** watcher.
4. **≥1 output, each naming its table's uuid.** A notifier asks `can_be_notified` (and renders on `can_get_metadata`)
   on `table:<uuid>`, the uuid the output's own `rask_lance` facet carries (`tableUuid`, ADR 0173 §10, [[LH-522]],
   [[LH-524]]). An output without one names no object any grant could reach. The catalog and maintenance stamp the
   uuids, lineage's HTTP door resolves an outside producer's names as its token, and `/events/projection` keeps each
   output's `rask_lance` facet.

**And the event is SIGNED.** Lineage verifies the `rask_signature` of every event on the bus and in the outbox
(`signing.enforce`, on by default) and refuses what does not verify (trap 5). A notifier verifies the same signature
against the same signer sets, by the contract `docs/LINEAGE.md` § Signed events (the `rask_signature` facet) publishes,
so an unsigned event reaches nobody whose notifier enforces.

```python
event = {
    "eventType": "FAIL",  # (1) TERMINAL. START/RUNNING end nothing.
    "eventTime": datetime.now(UTC).isoformat(),
    "run": {
        "runId": str(run_uuid),
        "facets": {
            # (2) The VERIFIED principal key (`token.subject`, `<idp-id>~<user id>`, [[LH-063]]). NOT settings.author,
            #     NOT a role/team string, NOT a display name. `{name, sub}` together is what every verifying writer stamps.
            #     (5) binds it: a signer is its own author, so a service stamps its own signing
            #     identity and the person rides in `rask_lance.originator` (Q2). Only the catalog signs
            #     for a person it authenticated (`on_behalf_of=`).
            "rask_author": {"name": token.subject, "sub": token.subject},  # AUTHOR_FACET, lakehouse.lineage_events
            "rask_lance": {  # LANCE_FACET
                "operation": "promote",
                "runId": producer_run_id,  # what YOUR detail door answers to; a catalog commit names its writer's run as `parent` (ADR 0191)
                "project": project_id,  # (3) WATCH's key. Omit -> zero watchers.
            },
            "errorMessage": {"message": reason},
        },
    },
    # (4) Each output carries its table's uuids (`lakehouse.lineage_events.with_dataset_uuids` stamps `facets.rask_lance.tableUuid` and
    #     `namespaceUuid`), as the table's and namespace's DescribeTable/DescribeNamespace answer them (`rask.uuid`).
    #     An output without one names no object any grant reaches, so the run reaches nobody.
    "outputs": [with_dataset_uuids({"namespace": "gold", "name": f"{project_id}-gold$catalog"}, uuids, producer=PRODUCER)],
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
    action="grant_revoked",  # a ControlAction member
    object_type="grant",
    object_id=f"project:{project_id}",
    actor=f"user:{token.subject}",  # the VERIFIED principal that made the change, `user:` and its key
    extra={"relation": relation, "subject": fga.user_object(principal)},  # WHO this is about, as the OpenFGA user it is
)
```

Call it **after** the backend mutation and its audit succeed, so a change that did not happen is never announced. A
table's or namespace's `object_id` is its uuid object (`table:<uuid>`, the object its grants are held by, [[LH-524]])
with its name in `object_name`, and the typed `object_uuid` and `namespace_uuid` name it (or the one a grant or policy
is scoped to) by its uuids (ADR 0173 §10). The catalog's control emitter fills `object_uuid` from `object_id` and the
namespace's uuid from `object_name` after the door acts (`catalog.api.dependencies.UuidNamingControlEmitter`); a door
whose act ends or moves the object reads them first and passes them.

**`extra.subject` is the entire targeting**, and it is the OpenFGA user the change names: `fga.user_object` (`user:` and
the principal key percent-encoded), or a userset. A notifier decodes it with `fga.subject_of` and keeps only a person
(`is_person_subject`), expanding a userset through the decision door's `list-users`. A raw `f"user:{key}"` in
`extra.subject` is the defect, though the `actor` keeps that form: it names another principal whenever the key carries a
character the encoding escapes. A missing subject, a bare `user:`, `*` and a service account's principal name nobody.

**The emitter signs, and `make_control_emitter(sign=...)` has no default** ([[XC-078]]). A producer passes
`attach_control_signature` over its own `SigningKeyHolder` (the catalog's, `catalog/core/control_signing.py`, declares
the `user:` actor it authenticated; maintenance signs as itself), or `None` when it holds no signing identity, which
only a door verifying nothing acts on. A signer whose key is unresolved publishes and stages nothing: the event is
withheld and counted under `reason="unsigned"`, outbox or not, and the signer reports itself not ready. The catalog
therefore refuses every write it would announce while its key is unresolved, 503 code 17 before the commit
(`catalog/api/signing_gate.py`, [[XC-095]]); a producer with no such gate loses what it emits while its key is
unresolved. No relay signs: the catalog's control relay republishes a staged event verbatim only when it verifies as
signed by the catalog's own identity and retires anything else as poison.

**A notifier that enforces signatures hears an action only from the role that emits it.**
`lakehouse.control_events.control_signer_role(action)` names that role; a notifier's door verifies the envelope's
top-level `rask_signature` against that role's identities, so a valid signature from another service is refused. A
person actor (`user:<sub>`) verifies only under a delegation from a delegator, `onBehalfOf` equal to the whole actor. No
action is exempt, so the lane carries only the lakehouse's own signed events (ADR 0171).

**One action erases rather than tells: `principal_deleted`** (CTL-022). The catalog's offboarding door ([[LH-233]])
names the principal in `extra.subject`; nobody is left to tell, so a notifier erases what it keeps for them. A userset,
wildcard or service account names nobody to erase.

**A new action is a change to `lakehouse/control_events.py`:** add the member to `ControlAction`, or the envelope
will not validate, and its case to `control_signer_role` (ty fails the `assert_never` until it has one), which names the
role whose signature a notifier requires. Then name its subject in `docs/COMMIT-EVENTS.md` § Whom a control event names
if it names a person. `ControlAction` is a wire contract reaching the console through `docs/catalog-openapi.json`: see
`rask-lance-catalog` for the regen step. A notifier's own reasons and stored records are its own; a client that stores
the action names it reads must survive a member it does not know, since the lakehouse adds members without asking.

## The five traps — an event emitted is not a person addressed

1. **A role literal in `rask_author.sub` names nobody.** A notifier reads `rask_author.sub` and **nothing else** —
   never `rask_author.name`, never the standard `ownership` facet — because those are producer-supplied and honouring
   them would let any producer address a named person. A service that authors with a role literal or its own identity
   names that literal; the literal is *correct* as the author, and what reaches the person is the ORIGINATOR riding
   beside it, so every hop that carries the work re-carries it.
   **A service subject in the originator is the same defect, and it is worse than silence because it looks
   addressed.** A service authenticates to the catalog AS ITSELF, so an originator derived from a control event's
   `actor` names the service. The catalog RESOLVES the originator of a publication once
   (`publication.publication_originator` — a service caller's carried claim, else a human caller's own verified sub,
   else nothing) and echoes it onto `table_published`, so a reader takes `extra` instead of guessing. The catalog owns
   that decision because its door is the one that verified the caller: a `ServicePrincipal` (a projected service-account
   token, [[LH-220]]) is a service, and an IdP token is a person whatever claims it carries.
2. **A service token SUBSTITUTES the author.** If your emit runs behind a service bearer, `enforce_author`
   (`lineage/api/fga_deps.py`) **overwrites** the facet with that service's sub — "never trust the request body" is
   doing its job, and your human is gone. Carrying the human's sub through your own call graph is the only fix.
3. **No `rask_lance.project` disables WATCH silently.** The catalog resolves the tenant centrally in `emit_write_event`
   through `emitter.project_for(segments[0])` (`catalog/core/lineage_emit.py`) rather than at each call site. Two
   properties of that are the reusable part: resolve the tenant ONCE at the choke point, and **omit rather than
   sanitize** a project that fails `is_safe_project` — a project-less run reaches its author and no watchers, while a
   coerced one could reach the WRONG tenant's watchers, which is disclosure rather than a miss. The residue:
   `project_for` is best-effort and returns `None` on any failure, so a write whose top segment is not registry-bound
   reaches zero watchers, silently.
4. **A non-personal principal is not an address.** `user:*` (managed access) is a statement about everyone and
   addresses no one. A userset (`team:acme#member`) on the control lane is expanded to the persons it holds, and names
   nobody with authorization off. An address must identify a person.
5. **An unsigned event, or one from a publisher lineage does not list, is recorded by nobody.** Lineage verifies every
   bus and outbox event against the keys its signers publish and refuses one with no signature, an identity outside
   `LINEAGE_SIGNERS`, a signer that is not its stamped author (only the catalog signs for a person), or bytes that do
   not verify. The refusal is an ACK counted by lineage's signature-refused series (`lineage.core.metrics`): never
   retried, parked or recorded, and the only alert excludes `unsigned` and `signer`. A notifier that does not enforce
   would tell a person about a run lineage's feed does not hold. **A new lineage-lane producer is a `lance.signers`
   entry in the chart** (it mints the pair, sets `RASK_SIGNING_IDENTITY` and lists the identity in `LINEAGE_SIGNERS`)
   that calls `attach_signature` on every event it emits and emits nothing while its key is unresolved
   (`SigningKeyUnavailableError`). Worked reference: `services/maintenance/src/maintenance/core/lineage_emit.py`.

## The FGA prerequisite

A producer ships **one** grant: the recipients' grants on the objects its outputs name, since a notifier asks
`can_be_notified` on `table:<output uuid>` per recipient. See `openfga` — `can_*` relations are never directly
assignable. A notifier's own roles (`decider` for the check, `member_expander` for a userset, `event_reader` for the
whole feed) are grants to its identity made at the catalog's access doors ([[LH-563]], ADR 0192), never a values file,
and are the client's to request.

## Testing a producer

Assert on the emitted event's targeting fields — the terminal state, `rask_author.sub`, `rask_lance.project` and
`originator`, each output's `rask_lance.tableUuid`, a control event's `extra.subject` and its signature — never on the
emit call's status: an event that names nobody is emitted successfully too, which is exactly how a coverage gap hides.

## What a notifier is entitled to discard

Rules with reasons, so a producer does not rely on the opposite:

- **Non-terminal states** — "your run started" tells the clicker nothing and is noise to everyone else.
- **Output-less runs** — a notification names the object it is about, and there is no honest value for that field.
- **Unverified authors** — see trap 1. Attribution and targeting are different questions.
- **Wildcards** — a statement about everyone addresses no one. A userset is not a wildcard: it is expanded to its person
  members, or addresses no one when authorization cannot expand it.
- **Actions naming no party** — delivering every catalog mutation recreates the estate-wide feed targeting exists to
  replace.
- **Synchronous HTTP failures** — the caller already has the 4xx, and the console's bell already records it for the
  person who sent the write.

## What is still uncovered — the CLASSES, not a row list

These classes recur every time a new feature asks to tell somebody.

1. **No identity at the door.** A producer that never captured a verified sub can never name a person, and nothing
   downstream can repair it. Closed for every write through the catalog's namespace doors, whose event the catalog
   authors as the verified caller (data enters the lakehouse only there, ADR 0154). An emitter without an identity
   produces events a notifier is *designed* to discard, which reads as coverage and is not.
2. **No principal at all.** Some transitions are caused by a TIMER, not a person. The audience usually exists, but a
   timer's emit site has no `Request` to resolve `ControlEmitterDep` from, so it must be given a handle to an emitter.
   Before filing such an emit site as impossible, separate "no audience" from "no handle": only the first is structural.
3. **A fact that only exists while somebody is looking.** Several failures are composed at read time inside an HTTP
   handler and never persisted or published. Nothing can tell anyone what was never recorded.
4. **An audience that is real but unmodelled.** A producer with no project and no lakehouse output fails the output rule
   even with a perfect emit.
5. **Steady states wearing an event's clothes.** A permanently un-granted service, a repeating denial, a degraded lane —
   these are METRICS, and `docs/adr/0039-lineage-records-what-happened-to-data-an-authorization.md` records why lineage
   must not carry them.

**The line, worth re-reading before adding anything:** lineage answers *what happened to this dataset and who produced
it*; the control lane answers *what changed for this person*; metrics answer *how often is this happening*. A fact that
names a principal and touches no data is never lineage.
