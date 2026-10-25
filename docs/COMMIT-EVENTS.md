# Following committed writes: the commit-event contract

A pipeline that reacts to writes (trigger the next tier, refresh a cache, run a check) follows lineage's
durable feed. This page is the contract it polls; rask code is not needed to read it (LH-472, ADR 0156).

## The call

```
GET /api/lineage/events?since=<seq>&limit=<n>[&table=<table-id>][&namespace=<namespace-id>]
Authorization: Bearer <token>
```

- `since` is the last cursor you stored. The answer holds the events with a larger `seq`, **oldest first**.
- `limit` is at most 500. A page can be shorter than `limit` while more events exist: see the cursor rule.
- `table` keeps the events naming the table that id (`ns$table`) names now, under every name it has had: lineage
  resolves the id to the table's uuid, which a rename keeps. `namespace` keeps the events naming a table directly in
  the namespace that id names now. Each resolves as your token sees it; an id that names nothing you may see keeps the
  events whose names match it (for `namespace`, every table id under it). Both may be given.
- The token is a bearer token lineage verifies. Which service-account tokens it admits is LH-474 (#600).

## The cursor rule

```json
{"events": [...], "next_cursor": 1043, "oldest_seq": 812}
```

- **Always pass `next_cursor` back as `since`.** It is the last seq this page covered, including events
  your filters or your grants hid, so the next poll never re-reads or skips one.
- **`next_cursor` equal to the `since` you sent, with no events, means you are caught up.** Poll again later.
- **`seq` is the only ordering promise.** A row's seq is its Lance stable row id plus one, which Lance assigns
  in commit order (`lance_docs/file_format.md`, "Row ID Assignment"): no event appears with a smaller seq
  than one you have already read. Seq numbers can have holes; a hole is not a lost event.
- **Where to start.** To read everything the feed still holds, start at `since = oldest_seq - 1`; to read
  only what arrives from now on, start at the highest `seq` of a newest-first read (`GET /events?limit=1`).
- **A cursor below `oldest_seq - 1` may have lost events to retention.** The feed deletes old rows, and a
  seq hole just below `oldest_seq` looks the same, so this is a possible loss rather than proof. Treat it as
  one: reconcile the tables you follow from the catalog (`DescribeTable`, `ListTableVersions`).
- **A cursor beyond the feed's head came from another feed.** A `since` larger than the newest seq the feed
  holds is answered with no events and `next_cursor` equal to that newest seq, lower than the `since` you
  sent. The feed was rebuilt or restored; re-read it from `oldest_seq - 1`, and expect to see events again
  that you have already handled. The notifications reconciler does exactly this.

## What you can see

An event is returned only if your subject has `can_get_metadata` on every governed table it names, checked on
`table:<uuid>`, the uuid the table's `lance` facet carries (ADR 0173 §10). Inputs from outside the lakehouse are exempt
when the event payload is returned; with `summary=true` the payload is dropped, and the event is governed on the uuids
lineage recorded for it. An event naming a governed table it recorded no uuid for is hidden. Hidden events are never
returned, with any filter; the cursor still moves past them, and a seq number discloses nothing about a table.

## A feed row

| field | meaning |
| --- | --- |
| `seq` | the feed position (see the cursor rule) |
| `event_type` | the row kind's state, below |
| `event_time` | the producer's time for the event, an ISO-8601 string |
| `job` | `<job namespace>/<job name>`, or `null` on a dataset change |
| `author` | an attribution label: the `author` facet's `name`, else the first owner of an `ownership` facet |
| `inputs`, `outputs` | the table ids the event reads and writes |
| `table_uuids`, `namespace_uuids` | the uuids of the governed tables the event names and of their namespaces |
| `event` | the stored OpenLineage event, omitted with `summary=true` |

The `lance` facet below is rask's own: `run.facets.lance` on a run, `dataset.facets.lance` on a dataset
change. Its `version` is the **Lance version the write committed**, an integer, and is the value to act on:
open the table at that version, never "latest". It is absent when the catalog could not identify the commit.

Every governed dataset an event names carries `tableUuid` and `namespaceUuid` in its own `lance` facet
(`outputs[].facets.lance`, `inputs[].facets.lance`, `dataset.facets.lance`): the uuids the catalog minted for the table
and its namespace ([ADR 0173](adr/0173-tables-and-namespaces-are-keyed-by-a-minted-uuid.md)). A table's name can be
renamed or taken by another table; its uuid cannot, so key what you keep about a table on `tableUuid`. A uuid is
absent when the catalog could not read it. The OpenLineage dataset `name` stays the table id.

### A write that committed rows

`event_type` is the run state (`START`, `RUNNING`, `COMPLETE`, `FAIL`, `ABORT`) and `event` is an
OpenLineage `RunEvent` whose `producer` is the catalog's emitter
(`https://github.com/AI-Riksarkivet/rask/tree/main/services/catalog/src/catalog/core/lineage_emit.py`):

- `outputs[0].name`: the table id, byte-for-byte the catalog's id, as the table was named when the event was made.
  `outputs[0].facets.lance` carries `tableUuid` and `namespaceUuid`, which a rename keeps: the table's OpenFGA object
  is `table:<tableUuid>`, never its name (ADR 0173 §8, §10).
- `run.facets.lance`: `operation` (the catalog operation: `insert`, `merge_insert`, `update`, `delete`, ...;
  `create_table` when the table's first version holds rows, whether the catalog's `create` door wrote them or a
  client committed them through `version/create`),
  `version`, `ref` (the branch, absent for main), `branchIdentifier` (tells two incarnations of one branch
  name apart), `project`.
- `outputs[0].facets.version.datasetVersion`: the same version as a string, when known.
- `run.facets.author.sub`: the subject of the verified token that made the call.
- `inputs`: the tables the write derived from, at their versions. A rename names the source table as its
  input, so a follower filtering on the old name sees the rename.

### A publication

The table's `published` tag moved: the version readers of a governed tier read changed, and no row was committed
([ADR 0161](adr/0161-the-tag-gate-runs-at-publish.md)). `event_type` is `COMPLETE` and `event` is a `RunEvent` from the
catalog's emitter whose `run.facets.lance.operation` is `publish_table` and whose `version` is the version now
published, a version an earlier row already committed. `run.facets.author.sub` is who moved it: the publisher when the
project's gate passed the version, or the validator who approved it when the gate held it. Trigger the next tier on
this row when it should read only what readers of `@published` read.

### A dataset change

A schema or lifecycle change that committed no rows (a create with no rows, a drop, ...). `event_type` is `COMPLETE`, `job`
is `null`, and `event` is an OpenLineage `DatasetEvent` with no `run`: the table is `dataset.name`, and the
`lance` and `author` facets sit on `dataset.facets`.

### Events from other producers

Events posted to `POST /api/lineage/api/v1/lineage` by other OpenLineage producers appear in the same feed.
Their `author.sub` is bound to the posting token, and each governed dataset's `tableUuid` and `namespaceUuid` are the
ones its name resolved to through the catalog with that token when lineage took the event in, whatever the producer
claimed. A name the token cannot resolve (a service account's token is lineage's audience, which the catalog refuses)
is recorded without a uuid. Every other facet is that producer's own claim.

## Delivery

- The catalog stages each write's event in its outbox (the chart sets `LANCE_LINEAGE_OUTBOX_URI`) before
  publishing; from there, delivery into the feed is at least once, and lineage records each event once
  (an insert-only merge on the row's key), so a redelivery lands no second row.
- A crash between the Lance commit and the stage loses that announcement, not the write: the commit names its author
  and operation, and the catalog's reconcile cron (`catalog.commitReconcile`, every 10 minutes by default) announces
  every committed version older than `catalog.commitReconcile.graceSeconds` that the feed holds no row for, from the
  table's version log (LH-497). A write's run id is derived from its commit (the table's uuid, ref, ref incarnation and
  version, keyed by a secret only the catalog holds), so the write's own announcement and a re-announcement are one
  run and land one row, whichever arrives first. A versioned dataset change carries its commit's time as `eventTime`
  for the same reason.
- An event the outbox drain or the reconciler recovers takes a new, larger seq, so it can arrive after events for later
  commits. Order the writes of one table by `lance.version`, not by `seq`.
- The feed keeps rows for `services.lineage.eventsRetentionDays` of arrival time (default 7), so a follower
  that stops for longer can find its cursor below `oldest_seq - 1` (see the cursor rule).
