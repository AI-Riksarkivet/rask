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
- `table` keeps the events whose inputs or outputs name that table id (`ns$table`). `namespace` keeps the
  events naming a table under that namespace id (`ns`, `ns$sub`). Both may be given.
- The token is a bearer token lineage verifies. Which service-account tokens it admits is LH-474 (#600).

## The cursor rule

```json
{"events": [...], "next_cursor": 1043, "oldest_seq": 812}
```

- **Always pass `next_cursor` back as `since`.** It is the last seq this page covered, including events
  your filters or your grants hid, so the next poll never re-reads or skips one.
- **`next_cursor` equal to the `since` you sent, with no events, means you are caught up.** Poll again later.
- **`seq` is the only ordering promise.** Seq order is the order events became visible in the feed: no
  event appears with a smaller seq than one you have already read. Seq numbers have holes; a hole is not
  a lost event.
- **Where to start.** To read everything the feed still holds, start at `since = oldest_seq - 1`; to read
  only what arrives from now on, start at the highest `seq` of a newest-first read (`GET /events?limit=1`).
- **A cursor below `oldest_seq - 1` may have lost events to retention.** The feed deletes old rows, and a
  seq hole just below `oldest_seq` looks the same, so this is a possible loss rather than proof. Treat it as
  one: reconcile the tables you follow from the catalog (`DescribeTable`, `ListTableVersions`).

## What you can see

An event is returned only if your subject has `can_get_metadata` on every governed table it names. Inputs
from outside the lakehouse are exempt when the event payload is returned; with `summary=true` the payload is
dropped, the bare names are all that is left, and those events are governed on every name. Hidden events
are never returned, with any filter; the cursor still moves past them, and a seq number discloses nothing
about a table.

## A feed row

| field | meaning |
| --- | --- |
| `seq` | the feed position (see the cursor rule) |
| `event_type` | the row kind's state, below |
| `event_time` | the producer's time for the event, an ISO-8601 string |
| `job` | `<job namespace>/<job name>`, or `null` on a dataset change |
| `author` | an attribution label: the `author` facet's `name`, else the first owner of an `ownership` facet |
| `inputs`, `outputs` | the table ids the event reads and writes |
| `event` | the stored OpenLineage event, omitted with `summary=true` |

The `lance` facet below is rask's own: `run.facets.lance` on a run, `dataset.facets.lance` on a dataset
change. Its `version` is the **Lance version the write committed**, an integer, and is the value to act on:
open the table at that version, never "latest". It is absent when the catalog could not identify the commit.

### A write that committed rows

`event_type` is the run state (`START`, `RUNNING`, `COMPLETE`, `FAIL`, `ABORT`) and `event` is an
OpenLineage `RunEvent` whose `producer` is the catalog's emitter
(`https://github.com/AI-Riksarkivet/rask/tree/main/services/catalog/src/catalog/core/lineage_emit.py`):

- `outputs[0].name`: the table id, byte-for-byte the catalog's id and the OpenFGA object id.
- `run.facets.lance`: `operation` (the catalog operation: `insert`, `merge_insert`, `update`, `delete`, ...),
  `version`, `ref` (the branch, absent for main), `branchIdentifier` (tells two incarnations of one branch
  name apart), `project`.
- `outputs[0].facets.version.datasetVersion`: the same version as a string, when known.
- `run.facets.author.sub`: the subject of the verified token that made the call.
- `inputs`: the tables the write derived from, at their versions. A rename names the source table as its
  input, so a follower filtering on the old name sees the rename.

### A dataset change

A schema or lifecycle change that committed no rows (create, drop, ...). `event_type` is `COMPLETE`, `job`
is `null`, and `event` is an OpenLineage `DatasetEvent` with no `run`: the table is `dataset.name`, and the
`lance` and `author` facets sit on `dataset.facets`. The same row kind records a drop that lineage's
reconcile observed on storage: `author` is `reconcile` and `dataset.facets.lance` carries
`operation: "drop_table"` and `observed: "absent on storage"`.

### A write recovered by reconcile

A committed write whose event never reached the feed (see Delivery) is back-filled by lineage's reconcile.
`event_type` is `RECONCILED`, `author` is `reconcile`, and `event` is a `RunEvent` with `eventType: OTHER`,
`producer` lineage's reconcile, job namespace `lance-reconcile`, and `run.facets.lance` carrying
`operation: "reconcile"`, the committed `version` and the `ref`. It names no author facet, no project and no
`outputs[0].facets.version`: act on `run.facets.lance.version`.

### Events from other producers

Events posted to `POST /api/lineage/api/v1/lineage` by other OpenLineage producers appear in the same feed.
Their `author.sub` is bound to the posting token; every other facet is that producer's own claim.

## Delivery

- The catalog stages each write's event in its outbox (the chart sets `LANCE_LINEAGE_OUTBOX_URI`) before
  publishing; from there, delivery into the feed is at least once, and lineage drops redeliveries of the
  same event.
- A crash between the Lance commit and the stage loses the event until lineage's reconcile back-fills it.
  A back-filled event takes a new, larger seq, so it can arrive after events for later commits. Order the
  writes of one table by `lance.version`, not by `seq`.
