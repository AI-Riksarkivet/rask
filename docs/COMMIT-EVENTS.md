# The commit-event contract: following writes and telling people

A pipeline that reacts to writes (trigger the next tier, refresh a cache, run a check) follows lineage's
durable feed. This page is the contract it polls; rask code is not needed to read it (LH-472, ADR 0156). It is also the
contract the lakehouse's events publish for any notifier, a client that tells a person what the lakehouse did
([§ Telling people](#telling-people-the-notifier-contract),
[ADR 0190](adr/0190-the-notifications-inbox-is-a-client-of-the-lakehouses-events.md),
[ADR 0193](adr/0193-the-inbox-is-retired-and-the-console-joins-the-product.md)).

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
  in commit order (`lance_docs/lance/format/table/row_id_lineage.md`, "Row ID Assignment"): no event appears with a smaller seq
  than one you have already read. Seq numbers can have holes; a hole is not a lost event.
- **Where to start.** To read everything the feed still holds, start at `since = oldest_seq - 1`; to read
  only what arrives from now on, start at the highest `seq` of a newest-first read (`GET /events?limit=1`).
- **A cursor below `oldest_seq - 1` may have lost events to retention.** The feed deletes old rows, and a
  seq hole just below `oldest_seq` looks the same, so this is a possible loss rather than proof. Treat it as
  one: reconcile the tables you follow from the catalog (`DescribeTable`, `ListTableVersions`).
- **A cursor beyond the feed's head came from another feed.** A `since` larger than the newest seq the feed
  holds is answered with no events and `next_cursor` equal to that newest seq, lower than the `since` you
  sent. The feed was rebuilt or restored; re-read it from `oldest_seq - 1`, and expect to see events again
  that you have already handled. A notifier walking the feed does exactly this.

## What you can see

An event is returned only if your subject has `can_get_metadata` on every governed table it names, checked on
`table:<uuid>`, the uuid the table's `rask_lance` facet carries (ADR 0173 §10). Inputs from outside the lakehouse are exempt
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
| `author` | an attribution label: the `rask_author` facet's `name`, else the first owner of an `ownership` facet |
| `inputs`, `outputs` | the table ids the event reads and writes |
| `table_uuids`, `namespace_uuids` | the uuids of the governed tables the event names and of their namespaces |
| `event` | the stored OpenLineage event, omitted with `summary=true` |

The `rask_lance` facet below is rask's own: `run.facets.rask_lance` on a run, `dataset.facets.rask_lance` on a dataset
change. Its `version` is the **Lance version the write committed**, an integer, and is the value to act on:
open the table at that version, never "latest". It is absent when the catalog could not identify the commit.
The `rask_lance` and `rask_author` facets have published schemas, `spec/facets/rask/RaskLanceFacet.json` and
`RaskAuthorFacet.json`, served under the git tag `facets-1.0.0` that each facet's `_schemaURL` names.

Every governed dataset an event names carries `tableUuid` and `namespaceUuid` in its own `rask_lance` facet
(`outputs[].facets.rask_lance`, `inputs[].facets.rask_lance`, `dataset.facets.rask_lance`): the uuids the catalog minted for the table
and its namespace ([ADR 0173](adr/0173-tables-and-namespaces-are-keyed-by-a-minted-uuid.md)). A table's name can be
renamed or taken by another table; its uuid cannot, so key what you keep about a table on `tableUuid`. A uuid is
absent when the catalog could not read it. The OpenLineage dataset `name` stays the table id.

Every dataset the catalog and maintenance name is under one OpenLineage `namespace`, the catalog's endpoint:
`lance-namespace://<host>/api/catalog`, the release's public host ([[LH-553]]; OpenLineage names a catalog's tables by the
catalog serving them, https://openlineage.io/docs/spec/naming). Two estates' `acme$bronze$events` are then two datasets
in one graph backend. An event that names a table's location (a create, a write, a register, a publication) also lists
it in the dataset's standard `symlinks` facet as OpenLineage's clients name a storage location, type `LOCATION`:
`s3://<bucket>` and the key, or `file` and the path. So an engine that reads the table by its location (Spark, Ray or
DataFusion through the namespace) names the same dataset in the graph the feed is forwarded to: Marquez joins a dataset
by every identifier it was first seen under (`DatasetSymlinkDao`, 0.51.1). A drop or a deregister names no location;
the event that made the table laid its symlink, and a rename keeps the location, so in Marquez the location stays
joined to the name it was first seen under. A rename is one dataset change on the new name whose standard
`lifecycleStateChange` is `RENAME`, with the old name as `previousIdentifier` (LH-554); Marquez 0.51.1 reads only `DROP`
from that facet (`OpenLineageDao`), so there the new name is a dataset of its own, and the old name is not marked
deleted.

### A write that committed rows

`event_type` is the run state (`START`, `RUNNING`, `COMPLETE`, `FAIL`, `ABORT`) and `event` is an
OpenLineage `RunEvent` whose `producer` is the catalog's emitter
(`https://github.com/AI-Riksarkivet/rask/tree/main/services/catalog/src/catalog/core/lineage_emit.py`):

- `outputs[0].name`: the table id, byte-for-byte the catalog's id, as the table was named when the event was made.
  `outputs[0].facets.rask_lance` carries `tableUuid` and `namespaceUuid`, which a rename keeps: the table's OpenFGA object
  is `table:<tableUuid>`, never its name (ADR 0173 §8, §10).
- `run.facets.rask_lance`: `operation` (the catalog operation: `insert`, `merge_insert`, `update`, `delete`, ...;
  `create_table` when the table's first version holds rows, whether the catalog's `create` door wrote them or a
  client committed them through `version/create`),
  `version`, `ref` (the branch, absent for main), `branchIdentifier` (tells two incarnations of one branch
  name apart), `project`.
- `run.facets.parent`: the standard `ParentRunFacet` naming the writer's own run, when the commit declared one: the
  spec Context headers `x-rask-run-id` (a UUID), `x-rask-job-namespace` and `x-rask-job` on a catalog door, or the
  transaction properties `rask.run_id`, `rask.job_namespace` and `rask.job` on a stock client's `version/create`, all
  three or none (LH-554). A graph backend files the catalog's commit run under the pipeline's run by it.
- `outputs[0].facets.version.datasetVersion`: the same version as a string, when known.
- `run.facets.rask_author.sub`: the subject of the verified token that made the call.
- `inputs`: the tables the write derived from, at their versions.

### A publication

The table's `published` tag moved: the version readers of a governed tier read changed, and no row was committed
([ADR 0161](adr/0161-the-tag-gate-runs-at-publish.md)). `event_type` is `COMPLETE` and `event` is a `RunEvent` from the
catalog's emitter whose `run.facets.rask_lance.operation` is `publish_table` and whose `version` is the version now
published, a version an earlier row already committed. `run.facets.rask_author.sub` is who moved it: the publisher when the
project's gate passed the version, or the validator who approved it when the gate held it. Trigger the next tier on
this row when it should read only what readers of `@published` read. Its run id is derived from the table, the version
and the time the tag moved, so one publication is one row however many times it is announced, and a version published
again later (after an erasure dropped the tag) is a new row.

### A dataset change

A schema or lifecycle change that committed no rows (a create with no rows, a drop, a rename, ...). `event_type` is
`COMPLETE`, `job` is `null`, and `event` is an OpenLineage `DatasetEvent` with no `run`: the table is `dataset.name`, and the
`rask_lance` and `rask_author` facets sit on `dataset.facets`. A rename's `dataset.facets.lifecycleStateChange` is `RENAME`
and names the old name as `previousIdentifier` (`namespace`, `name`), so a follower filtering on the old name reads the
rename by it.

### Events from other producers

Events posted to `POST /api/lineage/api/v1/lineage` by other OpenLineage producers appear in the same feed.
Their `rask_author.sub` is bound to the posting token, and each governed dataset's `tableUuid` and `namespaceUuid` are the
ones its name resolved to through the catalog with that token when lineage took the event in, whatever the producer
claimed. A name the token cannot resolve (a service account's token is lineage's audience, which the catalog refuses)
is recorded without a uuid. Every other facet is that producer's own claim.

A dataset another producer names by a storage location, and every identifier its `symlinks` facet lists, is decided on
the table it names, because the forwarded graph joins them to that table: lineage asks the catalog which table a
location holds (`POST /management/v1/decisions/locate`, on the `governance_prober` role), and resolves a symlinked
catalog name as the poster. Recording one the poster may not see is refused 403, as naming the table by its id is, and
a dataset named by a location a table holds carries that table's `tableUuid` in the feed; a location no table holds is
an outside source, and so is a dataset under another catalog's `lance-namespace://` host.

## Delivery

- The catalog stages each write's event in its outbox (the chart sets `LANCE_LINEAGE_OUTBOX_URI`) before
  publishing; from there, delivery into the feed is at least once, and lineage records each event once
  (an insert-only merge on the row's key), so a redelivery lands no second row.
- A crash between the Lance commit and the stage loses that announcement, not the write: the catalog recorded the
  caller it authorized the commit for and the commit names its operation, and the catalog's reconcile cron (`catalog.commitReconcile`, every 10 minutes by default) announces
  every committed version older than `catalog.commitReconcile.graceSeconds` that the feed holds no row for, from the
  table's version log (LH-497). A write's run id is derived from its commit (the table's uuid, ref, ref incarnation and
  version, keyed by a secret only the catalog holds), so the write's own announcement and a re-announcement are one
  run and land one row, whichever arrives first. A versioned dataset change carries its commit's time as `eventTime`
  for the same reason. A re-announcement names the same commit, at the same version and for the same author, as the
  write's own announcement would: the catalog signs it only for the caller it recorded authorizing that commit, at that
  version and for the manifest it judged. Where the catalog authenticates its callers, a version it recorded no author
  for, whatever `rask.author` or transaction uuid it carries, is never announced: it is counted unauthored, which pages
  the estate's operator (LH-550). A table registered over a dataset written elsewhere is
  looked at from its register on (LH-550): the versions it held then were committed past the catalog, so none is
  announced, whatever author it names, and the register's own `register_table` event is the one the feed carries.
- A publication is recovered the same way (LH-552, [ADR 0188](adr/0188-a-lost-publication-is-announced-from-its-tag-and-lifecycle-markers-stay-best-effort.md)):
  the reconcile cron reads the version each table's `published` tag names and when the tag moved there, and when the feed
  holds no `publish_table` for that move, announces it for the person the catalog recorded moving the tag. A tag moved past the catalog's doors names no
  such person: it is counted unauthored, which pages, and never announced. Only the version the tag names now is looked
  for, so a publication the tag moved past before the cron ran is not announced; the newer one is.
- A table's lifecycle markers (a declare, a register, a rename, a drop, a deregister) are best-effort: a crash
  between the change and its outbox stage loses that marker, and nothing announces it again (ADR 0188). A follower that
  must not miss a table coming or going reads `ListTables` and `DescribeTable` as the truth, and the marker as its cue.
- An event the outbox drain or the reconciler recovers takes a new, larger seq, so it can arrive after events for later
  commits. Order the writes of one table by `rask_lance.version`, not by `seq`.
- The feed keeps rows for `services.lineage.eventsRetentionDays` of arrival time (default 7), so a follower
  that stops for longer can find its cursor below `oldest_seq - 1` (see the cursor rule).

## Telling people: the notifier contract

A notifier tells a person what the lakehouse did: a run wrote a table they may read, a grant changed what they may do.
The lakehouse runs no notifier. It publishes its events, the fields in them that name whom each is about, and the
authorization verbs a notifier asks before it tells anyone; a notifier is a client of all three, deployed in its own
release ([ADR 0190](adr/0190-the-notifications-inbox-is-a-client-of-the-lakehouses-events.md)). This repository hosts
none: the console's bell lists only the reader's own writes, kept in the browser
([ADR 0193](adr/0193-the-inbox-is-retired-and-the-console-joins-the-product.md)).

An event that names nobody is undeliverable, whoever subscribes to it: coverage is decided by the producer that stamps
these fields, never by the notifier.

### Whom a run names

On a run event (a dataset change carries the same facets on `dataset.facets`, and has already happened when it is
emitted):

| field | names |
| --- | --- |
| `eventType` | the run's state: `COMPLETE`, `FAIL` and `ABORT` end it |
| `run.facets.rask_author.sub` | the principal lineage verified as the run's author, `<idp-id>~<user id>`: the posting token's subject on its HTTP door; on the bus the signer, or the person the catalog authenticated, which only the catalog may sign for ([LINEAGE.md](LINEAGE.md) § Signed events). `rask_author.name` and an `ownership` facet are attribution, never an address |
| `run.facets.rask_lance.originator` | the person a run was done for, when a service authored it as itself |
| `run.facets.rask_lance.project` | the tenant the run wrote in, whose members may choose to watch it; a run without one reaches no watcher |
| `outputs[].facets.rask_lance.tableUuid` | each table the run wrote, as the object `table:<tableUuid>` every check is asked on; an output without one names no object a grant could reach |

A value is an address only when it is one person's principal: `<idp-id>~<subject>`, where the idp id is `[a-z0-9-]+`
(so the first `~` ends it) and is neither `kubernetes` (a service account) nor `dex-client` (a Dex client signed in for
itself), and the subject is non-empty and carries no control character. A role literal, a service account's
principal, a userset, an OpenFGA object id and `*` address nobody.

### Whom a control event names

The catalog announces each governance change on the `catalog.control.v1` topic as a `CatalogControlEvent` (maintenance
announces its purges there too), after the change succeeds, signed by the role `control_signer_role` names for its
action. Most actions name an object and nobody.
These name a person in `extra.subject`, the OpenFGA user the change concerns (`user:` and the principal key
percent-encoded, which `fga.subject_of` decodes, or a userset):

| `action` | `extra.subject` |
| --- | --- |
| `grant_added`, `grant_revoked` | the principal or userset the grant names; `extra.relation` is the rung and `object_id` the object |
| `erasure_release_scheduled` | one event per person the release costs (the erasure's requester and each table owner), with the deadline in `extra.due_at` |
| `principal_deleted` | the principal the estate admin offboarded: nobody is left to tell, so a notifier erases what it keeps for them |

`table_published` carries `extra.originator`, the person a publication was for, as the catalog resolved it from a
service's claim or a person's own token.

### What a notifier asks

A notifier holds no OpenFGA credential. It asks the catalog's decision door as its own identity, on roles granted to
that identity at the catalog's access doors ([[LH-563]]); no values file grants one and no upgrade revokes one. A
notifier serving every tenant holds them on the estate root, granted at the estate admin's tuple door
(`POST /management/v1/access/tuples`). One serving some tenants holds `decider` on their projects or warehouses
(`POST /management/v1/projects/{id}/access/grant`, `POST /management/v1/warehouse/{id}/access/grant`), and is answered
about their objects alone: every other object is answered refused, as one that does not exist is.

| role | route | what it asks |
| --- | --- | --- |
| `decider` | `POST /management/v1/decisions/check` | `can_be_notified` on each `table:<uuid>` a run names, for each person it would tell, and `member` on `project:<id>` for a watcher. `can_be_notified` is the lakehouse's verb for any notifier: a stake in the object itself (`reader`), never a grant on something beneath it |
| `member_expander` | `POST /management/v1/decisions/list-users` | the people a userset in `extra.subject` holds |
| `event_reader` | lineage's `GET /events/projection` | the whole run feed, below |

### The whole run feed

`GET /api/lineage/events/projection?since=<seq>&limit=<n>` is the feed without the per-dataset filter, for an account
holding `event_reader` (`can_read_event_feed` on the estate root). The governed `GET /events` shows a service only the
tables its own grants reach, so a notifier walking it would never see a run in a warehouse it holds nothing on. Each row
is `{seq, event}`, the event cut to what deciding whom to tell reads: `eventType`, `eventTime`, `producer`, the run id
with its `rask_author`, `rask_lance` and `parent` facets, and each output's name with its own `rask_lance` facet (for a dataset
change, the dataset's name, namespace and those two facets). Nothing describing the data crosses it, and the cursor rule
above holds unchanged. Who is told stays gated per person by `can_be_notified`, so the role widens what the notifier
observes and never what a person receives.

The bus carries the run events too, on `lineage.events.v1`, but only the feed holds the ones producers post over HTTP,
so a notifier that must not miss a run walks the feed. The topics are internal: the NATS stream Job deletes every
`-durable` on a stream it walks that no component in the release renders (`chart/templates/nats-stream-job.yaml`), so a
durable a client creates there does not outlive the next upgrade.

### The names that carry the contract

A client reads these events with no import from the lakehouse: this section and the facet schemas
(`spec/facets/rask/`) are the contract. Inside the lakehouse its producers and doors share one definition of each
name, in the `lakehouse` library:

- `lakehouse.control_events`: `CatalogControlEvent`, `ControlAction`, `CONTROL_TOPIC` and `control_signer_role`;
- `lakehouse.lineage_events`: the `rask_lance` and `rask_author` facet keys and their schema URLs, `dataset_uuids`
  and `with_dataset_uuids`, and `event_identity` and `static_event_id`, the one derivation of an event's identity
  (§ An event's identity below), so a notifier reading the bus and one walking the feed key a change alike;
- `lakehouse.governed.subjects`: `is_person_subject`, the address rule above;
- `lakehouse.signature_doors`: `SignatureDoorSettings`, a verifying door's mode (`RASK_SIGNATURE_DOORS`) and the
  identities that sign the lakehouse's events (`RASK_EVENT_SIGNERS`, `RASK_EVENT_DELEGATORS`,
  `RASK_CONTROL_SIGNER_ROLES`), against which a notifier verifies each event's `rask_signature` (`lakehouse.lineage.signing`)
  before it tells anyone. Each signer's public key is the secret `signing-public-<key name>` (the signer's
  ServiceAccount name, `lance.signingKeys`) in the platform's secret store. No store role the product chart renders
  names a client (`lance.storeReads`), so a notifier's release provisions its own role to read those keys.

### An event's identity

A notifier keys what it delivers by an event's identity and state, and lineage's feed keys a row by the same identity,
so a reader of the bus and a walker of the feed must derive it alike:

- **A run event** is its run: the identity is `run.runId`.
- **A `DatasetEvent`** (a static metadata event) carries no run id, so its identity is derived from its content, never
  minted: the RFC 4122 version-5 UUID, in the namespace `6f0d9bb2-3c47-5f1a-9b8e-4a2d1c7e5f30`, of the UTF-8 string
  `<producer>|<dataset.namespace>|<dataset.name>|<operation>|<eventTime>`, where `<operation>` is the `rask_lance`
  facet's `operation`, every field is taken exactly as the event carries it, and an absent one is the empty string.
  `eventTime` is part of it on purpose: a redelivery carries the same instant and collapses onto one identity, while a
  table dropped and created again carries another and stays a second fact.

The signature an event carries is verified as `docs/LINEAGE.md` § Signed events (the `rask_signature` facet) describes.
