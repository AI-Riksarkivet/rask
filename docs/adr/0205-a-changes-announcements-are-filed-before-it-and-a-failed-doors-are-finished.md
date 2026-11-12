# 0205. A change's announcements are filed before it, and a failed door's are finished by the catalog (2026-11-12)

**Status:** accepted. Source: [[LH-576]] (#798), audit item E9d of the 2026-11-09 code audit, with LH-584 (#811) folded
in. The owner chose "Intent record first" for events after a change and "Fold into #798" for the table lifecycle doors
on 2026-11-12, through the multi-question tool. Amends [ADR 0188](0188-a-lost-publication-is-announced-from-its-tag-and-lifecycle-markers-stay-best-effort.md),
whose lifecycle-marker decision [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md)
reopened.

## Context

A door that changes state and tells people about it announces only after its change, so a change that did not happen
is never announced. That covers a grant added or revoked, a role or a project ended, a principal offboarded, a trashed
object released, and a table declared, registered, renamed, dropped, deregistered or undropped. Each emitter stages what
it publishes before the publish, so a bus that loses an event loses nothing. What lay between the change and the stage
(an error, a process death) lost the announcement for good. A revoke loses the most. After a revoke the person can no
longer see the object, so being named on the control lane is the only way they learn it. Unannounced, they find out from
a 403 in the middle of work (`.claude/skills/rask-notifications`).

None of the references recovers it. Lakekeeper main `c18bc33` publishes to an in-process channel after the commit,
with a 50 ms send timeout (`service/events/publisher.rs:525-580`). Polaris `8aa75ce` and Gravitino `ebae197` dispatch
after the change to an in-memory bus or queue, and Polaris documents the loss on abrupt termination. `lance_docs`
defines no events.

What the store shows is the evidence of a landed change:

- For a table, the `dir` catalog's DropTable and DeregisterTable remove the table's `__manifest` row, after which
  DescribeTable answers TableNotFound. Declare and register add the row
  (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md` §§ DeclareTable, DeregisterTable, DropTable).
- For a grant, it is whether OpenFGA holds the tuple.

## Decision

1. **A door files what it will announce before its change.** It writes one record per call at
   `_announcement_intents/<sha256 of the object>/<token>.json` on the control root
   (`catalog.services.announcement_intents`). The record names each announcement with the evidence that shows its
   change landed: a tuple held or gone, a name resolving to the table's uuid or no longer, a control record standing or
   gone, a role ended. The door makes its change, sends each announcement through the filing as it always has
   (`catalog.api.announcing.Announcer`), and removes the record last. A door that raises leaves its record.
2. **The doors that file**:
   - every grant door (`access/grant` and `access/revoke` on each type, `managed-access/set`, the raw tuple doors, the
     project members doors, role assign and unassign);
   - the role delete, project create and project delete;
   - the principal offboarding door and the trash release;
   - the table lifecycle doors: declare, register, rename, drop, deregister and undrop;
   - the cascades: the namespace drop (each table's drop marker and its `namespace_dropped`, the namespace's evidence
     its name no longer resolving to its uuid) and the warehouse delete (each table's deregister marker and
     `warehouse_deleted`, landed once the warehouse's record is gone).

   Declare and register file once the location the backend describes is known, which is after the declaration or the
   admission and before the ownership seed. A store that refuses the filing takes the declaration or the registration
   back. Every other door files before its first change. A door whose event carries what it learns while making its
   change (a cascade's counts) files the event as it plans it and sends it with what it did, under the filed id; a
   finish sends the planned one.
3. **The catalog finishes what a failed door left.** The door is `POST /management/v1/announcement-intents/finish`,
   which asks `can_reclaim_trash`, the reclaimer rung, and is audited as `announcement_intents_finish`. Maintenance's
   hourly audit asks it (`audit._finish_announcement_intents`).
   - It judges each record older than `LEASE` (the location claim's 15 minutes). The age is the listing's `Date` less
     the record's `LastModified`, both on the store's clock.
   - It sends each announcement whose change the store shows landed, as the event its door built, counts the rest
     unlanded, and removes the record.
   - It judges at most 200 records per call.
4. **One change is one event across requests.** Before it files, a door reads what is filed on its object.
   - Two announcements are the same event when they have the same evidence, lane and action, and on the lineage lane
     the same author, since lineage binds a run to its first author.
   - A control event of the same identity is sent with the door's own content under the earlier `event_id`. A lineage
     marker is the earlier one, whose run id derives from the time its request fixed and the table's uuid
     (`catalog.core.commit_ids.lifecycle_run_id`).
   - A record older than the lease is finished by the door then.

   So a retry, the attempt it retries and the finish send one event, and its reader keeps the copy that reaches it
   first.

## Consequences

- Announcements from the doors above are delivered at least once.
- **Store requests per door**, measured on the wire on 2026-11-12. The setup: a moto server behind
  `tests/integration/store_requests.counted`, pylance 13.0.0, a `dir` namespace with a 7-day grace period, and the
  same calls against the code before and after this change. Each door adds three requests on the control root when
  nothing stands on its object: a LIST of the object's directory, a conditional PUT and a DELETE.

  | Door | Before | After |
  | --- | --- | --- |
  | declare | 32 | 33 |
  | rename | 42 | 44 |
  | deregister | 37 | 41 |
  | register | 46 | 48 |
  | drop, recoverable | 26 | 29 |
  | drop, `purge=true` | 50 | 53 |
  | undrop | 32 | 35 |

  - Declare passes the uuid it minted rather than reading it back.
  - Rename and register name the uuid they hold, which saves a read.
  - Deregister's count before varied from 36 to 38 across runs, from the `dir` backend's re-reads of `__manifest`.
  - The grant doors file through the same `Announcer.announcing` and pay the same three requests. They were not counted
    on the wire, since they need OpenFGA.
  - A record standing on the object adds a GET. One older than the lease adds the reads that judge it, its sends and
    its DELETE, once.
- **What stays unfiled, and so best-effort as before:**
  - The deregister of a table no uuid identifies, whose announcement names no uuid to judge a name by.
  - A register or declare that dies between its backend call and its filing. That table is attached and unowned, and
    the drift report lists it under `ungoverned_tables`.
- **Evidence is the store's state when the finish reads it.** A change undone before the finish reads it (a grant
  revoked and granted again) is judged unlanded, and is not announced.
- **A redo within the lease can be deduplicated away.** A record its door could not remove (the store refused the
  DELETE) stands until the finish, which sends its announcements again under their ids. If the same change is undone
  and made again on the object within the lease, the second change reuses that id and its reader drops it as a
  duplicate.
- **Attribution across callers.** Where another caller's request lands a change that a failed request filed, the
  control lane carries one event, attributed to whichever copy reaches the reader first. The lineage lane carries one
  run per author.
- **The http lineage transport cannot carry a finish's markers.** It forwards the caller's bearer, and a finish sends
  without one, which lineage's http door refuses. The chart runs the `dapr` transport (`LANCE_LINEAGE_TRANSPORT`), on
  which the catalog signs.
