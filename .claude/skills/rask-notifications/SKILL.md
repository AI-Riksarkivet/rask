---
name: rask-notifications
description: How a feature gets a person told — the targeted inbox behind the estate's bell. The four targeting sources (AUTHOR/WATCH/GRANT_ADDED/GRANT_REVOKED), the producer contract for the lineage and control lanes, and the four silent-drop traps that make an emitted event reach nobody. Use when adding a feature whose outcome a person should hear about; when a notification "should have fired" and did not; when adding a `ControlAction` or a `NotificationReason`; when emitting an OpenLineage run event from any service; or when wiring a new service into `lineage.events.v1` / `catalog.control.v1`.
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
| **Q2** | Should people *other than the actor* know, because it is the project's work? | `WATCH` | the same event **plus** `lance.project` |
| **Q3** | Does it give a specific person access? | `GRANT_ADDED` | `CatalogControlEvent`, `extra.subject` = the principal |
| **Q4** | Does it take access away from a specific person? | `GRANT_REVOKED` | the same envelope, `action="grant_revoked"` |

`AUTHOR` needs no registry and no opt-in — you may always be told about your own run
(`fanout.py:87`). `WATCH` is an explicit `project#member`-gated opt-in: **membership gates watching and
never implies it**, so no audience widens by default.

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
    "eventType": "FAIL",                       # (1) TERMINAL. START/RUNNING notify nobody.
    "eventTime": datetime.now(UTC).isoformat(),
    "run": {
        "runId": str(run_uuid),                # the notification id is `runId@STATE`
        "facets": {
            # (2) The VERIFIED token sub. NOT settings.author, NOT a role/team string,
            #     NOT a display name. `{name, sub}` together is what every verifying writer stamps.
            "author": {"name": token.sub, "sub": token.sub},
            "lance": {
                "operation": "promote",
                "run_id": producer_run_id,     # what YOUR detail door answers to
                "project": project_id,         # (3) WATCH's key. Omit -> zero watchers.
            },
            "errorMessage": {"message": reason},
        },
    },
    "outputs": [{"namespace": "gold", "name": f"{project_id}-gold$catalog"}],   # (4)
}
await publish_event(client, pubsub_name="pubsub", topic_name="lineage.events.v1",
                    data=json.dumps(event), data_content_type="application/json")
```

## Lane 2 — control (`catalog.control.v1`)

```python
await emit_control(                                  # best-effort; never raises into a committed mutation
    emitter,
    action="grant_revoked",                          # MUST be in NAMED_ACTIONS
    object_type="project",
    object_id=f"project:{project_id}",
    actor=f"user:{token.sub}",                       # the VERIFIED principal that made the change
    extra={"relation": relation, "subject": user},   # `subject` = WHO this is about, `user:bob`
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

Adding a `NotificationReason` needs **no frontend change**: the bell never reads `reason` (it appears
only in a stories fixture, which is itself stale — `project_watch` vs the backend's `watch`).

## The four traps — an event emitted is not a person told

1. **A role literal in `author.sub` reaches nobody.** `author_subject()` reads `author.sub` and
   **nothing else** — never `author.name`, never the standard `ownership` facet — because those are
   producer-supplied and honouring them would let any producer put a row in a named person's inbox.
   The medallion movers author with a chart role literal (`data_eng`/`analyst`/`htr`/`ray`,
   `chart/values.yaml:926-943`), so a failed cascade addresses an inbox actor named `ray`.
2. **A service token SUBSTITUTES the author.** If your emit runs behind a service bearer,
   `enforce_author` (`lineage/api/fga_deps.py:96-103`) **overwrites** the facet with that service's
   sub — "never trust the request body" is doing its job, and your human is gone. Carrying the human's
   sub through your own call graph is the only fix.
3. **No `lance.project` disables WATCH silently.** `fanout.py:88` skips the watcher loop entirely when
   `project` is `None`. The catalog stamps `lance = {operation, version}` and no project
   (`catalog/core/lineage_emit.py:189`), which is why no catalog write reaches a watcher anywhere in
   the estate; `ingest/lineage.py:213-215` is the working precedent.
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

## Known coverage gaps

A workflow audit (2026-08-16, 8 services, three adversarial verify lenses) found the plane sound but
its **producers** thin — the register, with per-gap `file:line` and a value-ordered sequence, is
`open_notifications_coverage.md` at the repo root. The two highest-leverage items are both one dict
key: stamp `lance.project` in the catalog's emitter, and carry the verified human sub through the
medallion cascade head. Fix a producer named there and delete its row.
