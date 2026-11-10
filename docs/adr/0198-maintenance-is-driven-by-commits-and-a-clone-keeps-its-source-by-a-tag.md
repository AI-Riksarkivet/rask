# 0198. Maintenance is driven by commits, and a clone keeps its source by a tag (2026-11-08)

**Reopened 2026-11-09** by [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md): decision 5 (maintenance on the bus directly, the other services through Dapr) is two bus paths in one product; it becomes the one path with the move off Dapr.

**Status:** accepted. Owner, 2026-11-08, through the multi-question tool: "Registered, by tag" for how a clone keeps its
source, and "Ship it, then redesign" for the order (XC-084 shipped first, ADR 0197). Supersedes the discovery decision
of [ADR 0045](0045-the-lakehouse-cloud-native-cutover-2026-09-03-04.md) ("maintenance discovers work by listing
buckets, and must"), the whole-estate protection pre-pass of
[ADR 0049](0049-maintenance-leaves-the-planner-pod-2026-09-04.md) and the clone list of
[ADR 0162](0162-the-lakehouse-builds-nothing-maintenance-decides.md) § 3. Each row that builds a part below removes what
it supersedes; until then the superseded design runs.

## Context

ADR 0045 rejected a catalog-directed work queue because writers then committed without the catalog. Since
[ADR 0157](0157-stock-clients-commit-through-managed-versioning.md) every stock client commits through `version/create`,
and a vend cannot write a final manifest (`NeverAFinalManifest`), so the catalog sees every commit and the premise is gone.

What the sweep costs, measured 2026-11-08 (the catalog on a moto server, every store request counted on the wire, an
idle estate): one tick is about 30 requests per table, 24 of them listings, plus 6 per retained commit (a HEAD per
recorded file), every 30 minutes whatever changed: 247 requests at 5 tables of 3 commits, 482 at 10, 337 at 5 tables of
6 commits. The tick runs in the one maintenance replica the chart pins, and the catalog refuses every stock compaction
503 once the clone list the tick writes is an hour old (LH-562), so a slow tick or a stopped pod blocks compaction. The
list is also written under maintenance's own identity and trusted by the catalog's commit judge (ADR 0197, residuals).

## Decision

1. **A registered clone keeps its source by a Lance tag.** A shallow clone is a dataset of its own whose manifest names
   the source's files; the source records nothing of it (`lance_docs/lance/format/table/transaction.md` § Clone), and it
   relies on "continued source retention, such as a source tag" (`lance_docs/lance/format/table/blob.md:309-313`), which
   cleanup honours (`lance_docs/lance/guide/tags_and_branches.md:61-67`). pylance 13.0.0's `shallow_clone` writes its
   target directly, with no namespace client, and the write vend denies that final manifest, so a clone enters the
   estate only through `RegisterTable`, whose door reads its `base_paths`: there the catalog takes a tag on each source
   version a clone names, and ends it when the clone ends. A clone never registered keeps its own tag through the tag
   door, as Lance's contract asks. The whole-estate manifest walk, the clone list and its 503 ceiling go.
2. **Work is enqueued where the catalog already sees it.** At `version/create` the catalog knows the table's uuid,
   resolves its policy by key and records when the table is next due; at a drop it records the deadline. The work is a
   JetStream work queue that any number of maintenance replicas consume as competing consumers, each table's task
   idempotent; the backstop is a slow pass over the due records, one listing per thousand tables, not over buckets.
3. **An overwrite in place is caught from the store's change notifications.** A session policy cannot require a
   conditional write on RustFS 1.0.3 (it refuses `s3:if-none-match` as an unknown condition key, measured), so the
   store announces each write and only the keys a commit recorded are checked, instead of a HEAD per recorded file
   every tick.

## Evidence, measured 2026-11-08

- A shallow clone of an untagged version is broken by the source's `cleanup_old_versions` (`Not found`); a clone of a
  tagged version reads all its rows after it (pylance 13.0.0). A second cleanup removes nothing: it is idempotent.
- One commit through maintenance's existing commit lane (`arrival.act_on_write`) costs 11 store requests at 5 tables and
  at 20.
- On NATS 2.14.2 JetStream, a work-queue task held by a worker that died unacknowledged is redelivered to another after
  the consumer's ack wait (2 s), no task is in flight twice, and the queue is empty after every ack.
- RustFS 1.0.3 publishes `s3:ObjectCreated:Put` on NATS for a write and for an overwrite in place, each naming the
  bucket, the key and the writer's principal (`RUSTFS_NOTIFY_NATS_*_PRIMARY`, ARN `arn:rustfs:sqs::primary:nats`).

## Consequences

- Maintenance's cost follows the estate's change, and it scales out; a stopped replica blocks no compaction.
- Not yet measured: whether RustFS keeps its events through a NATS outage (`RUSTFS_NOTIFY_NATS_QUEUE_DIR`), and the
  event and task throughput at estate scale. Each row that builds a part measures it with the same harness.
- The rows, in order: clone tags at register and the clone list's removal; commit-time due records and the due-record
  backstop; the overwrite check from store notifications; maintenance's work queue and scale-out.

## Amendment, 2026-11-09: as built ([[LH-494]], one row)

The four rows were built as one (owner, 2026-11-08), and building them settled what the decisions above left open.

- **Decision 2, the queue.** A commit's announcement holds one follow-up for its table, `maintenance.queue.commitPassDelaySeconds`
  out, so a burst makes one pass; each pass schedules the table's own next one. Tasks sit on MAINTENANCE_TASKS, one per
  subject: a guarded publish (`Nats-Expected-Last-Subject-Sequence: 0`) refuses a second while one is queued or in flight.
  A running worker keeps its task by heartbeat, and ends its replica when the bus stops answering until the ack wait its
  last answered heartbeat restarted runs out: JetStream answers a heartbeat for a delivery it has already handed on as it
  answers a live one (measured on nats-server 2.15.2, three nodes, R3, 2026-11-09), so only an unbroken run of answers
  vouches for the task, and two passes of one table never overlap, a bus cut that closes the connection included. An
  ack, nak or term is sent the same way, answered inside the lease or never, since JetStream applies a handed-on
  delivery's late ack, which frees the table's task under another replica's pass. A settle the network itself holds
  past the lease (a silent partition that heals, a stalled node that resumes) is still applied late, so a pass also
  holds a claim on its table in the MAINTENANCE_CLAIMS key-value bucket: created only where none stands, renewed by
  revision with each heartbeat, released only by its holder's revision, and gone one ack wait after its last renewal.
  A pass that finds another's claim waits it out, and a stale holder's late renewal or release cannot match the
  revision standing (measured on nats-server 2.15.2, three nodes, R3, 2026-11-09). The owner asked for it to be fixed
  rather than documented (2026-11-09), though two cleanups of one table at once were measured harmless (pylance 13.0.0:
  60 rounds on disk and S3, no table harmed). A heartbeat-extended deadline survives a consumer leader change (measured: a
  task redelivered at the extended deadline after a step-down). A pass is a thread nothing can
  stop, so it is the process that ends: a bus outage longer than the ack wait restarts each replica that was mid-pass,
  which has no work without the bus and may wait out a crash-loop backoff once it is back. For the same reason a stopping
  replica whose drain ends with a pass still running ends there, and a task is only ever run off the pull it answered:
  each pull has a subscription of its own, so a pull a reconnect held back, answered after the worker gave up on it,
  delivers to an inbox no one reads. A pass past twice `maxPassSeconds` ends its replica, and a task that
  outlives its worker on every one of `maxDeliver` deliveries is ended unrun on one more, which the consumer allows. The
  backstop is the hourly audit, which opens no table and queues each table whose schedule was lost; the orphan scan and the
  trash purge run once a day (owner, 2026-11-09).
- **Decision 3, the store's events.** Only writes are acted on: a write under a table holds that table's follow-up, and
  the pass checks every file its commits recorded against one listing of the table's root, rather than a HEAD per file.
  A delete is not acted on: a recorded file deleted while a retained version names it breaks every read at once, and the
  table's daily pass names it. STORE_EVENTS is capped (`maintenance.queue.storeEventsMaxBytes`, discard old), since each
  event is a hint the daily pass backs up. Maintenance keeps its own copy of the commit records it has read
  (`_maintenance_files/`, `lakehouse.file_identities`), so a pass reads only the record of the commit it follows.
4. **The store's operator configures its bucket events** (owner, 2026-11-09, through the multi-question tool: "Store's
   operator"), as it configures versioning; a production install attests it (`objectStore.events.provisioned`), and the
   development store's fixture does it for its own buckets.
5. **Maintenance talks to the bus itself**, as its own bus user (`maintenance-worker`). Dapr's JetStream subscription sends
   no in-progress acknowledgement, so a pass longer than its ack wait would be delivered again beneath itself, and its
   publish sets no header but the message id, so it cannot put a schedule (components-contrib e90662b02d70,
   `pubsub/jetstream/jetstream.go:115-145`). nats-py is the client and the worker is a plain pull worker; FastStream and
   taskiq-nats were weighed and do neither the bind-only consumer nor the in-progress heartbeat. Its replies arrive on an
   inbox of its own. The streams and consumers are the chart's stream Job's; the worker creates none. Its credential is
   the one the store held when it started, and the store's seed issues a new one when the user's grants change and
   revokes the old, which nats-py never gets past: it closes for good on a session the server ends, and retries a
   credential the server refuses for ever (nats-py 2.16). So a worker whose credential the bus takes no longer ends its
   process (`TaskQueue.connect`), and starts again on the store's current one, as `lakehouse.bus_watchdog` restarts a
   sidecar.
6. **The times are NATS message schedules** (ADR-51, MAINTENANCE_SCHEDULES): each table's next pass, a commit's follow-up,
   the hourly audit and the daily scan. The bus was upgraded to nats-server 2.15.2 for them (an estate on 2.14.2 steps
   through 2.14.9, as the upgrade guide asks); the worker asserts the audit's and the scan's schedules at its start and
   every 15 minutes, so a purged schedules stream heals.

Measured 2026-11-09:

- On nats-server 2.15.2: a second task on a held subject is refused (10077), and so is a guarded publish while a task is
  queued or in flight (10071). A fired `@at` schedule leaves no message; a replaced one fires once with the newer body.
  A work-queue message whose deliveries ran out stays in the stream; a term removes it.
- RustFS 1.0.3 publishes one event per write, overwrite in place and delete. Two writes made while NATS was down queued
  in its `QUEUE_DIR` and arrived about 5 s after NATS returned. It refuses a stream whose duplicate window is shorter than
  its retry lifetime (274 s at a 30 s ack timeout).
- Costs, on the wire over moto: a commit's pass is 58 requests at 4 tables and at 12, and a table of 13 commits reads one
  commit record. The hourly audit costs about one request per table; an idle table costs about 106 requests a day
  against about 7,200 under the sweep.
- On the bus lane, two replicas shared a burst with no pass in flight twice, and a killed replica's task went to the
  other after the ack wait.

What still grows with a table is Lance's own cleanup accounting: a pass reads each retained version's manifest
(`explain_cleanup_old_versions` and `versions`, [[LH-562]]), three reads a version, bounded by the table's retention.
Deriving the counts from a listing of `_versions/` instead would replace Lance's own answer, which LH-562 chose so that
the threshold counts exactly what cleanup deletes.

Closes-when (a) as the owner amended it, 2026-11-09, through the multi-question tool ("Amend clause to built", "Keep
Lance's count"): an idle table costs about one request an hour (the audit) and one pass a day (the recheck, the backstop
for a lost commit or store event and for the versions a deleted tag releases), and nothing walks the estate's files; a
commit's pass costs a fixed handful plus Lance's own cleanup accounting. A task a worker ends holds its next try
`maintenance.queue.recheckHours` out, so the audit does not hand a task that kills its worker straight back.

## Amendment, 2026-11-09: what a second audit of the row found ([[LH-494]])

A fresh audit of the design and the code, with the first audit's fixes in the tree, found decision 1 incomplete in three
places and two defects in the fixes themselves. Each is fixed with the first audit's HIGH findings, in one change.

- **Decision 1 tags every table a clone reads through, not only the one its transaction names.** A clone of a clone
  declares both roots and reads the first source's files where they are (measured on pylance 13.0.0), while its `Clone`
  names only the clone between them. Tagged on that one alone, it was broken by the first source's cleanup once the clone
  between them was deregistered, which releases only that clone's tag. Its registration now tags each root, at the
  version its own source's record pins, and asks `can_create_tag` on each.
- **The base record keeps the version and branch each `derived_from` entry pins.** A clone's own version expiry removes
  its first version, and with it the only transaction that names its source. Registered again after that, it was admitted
  on its record alone with no tag. A registration now tags from the record when the transaction is gone, and refuses a
  recorded root that carries no pin.
- **A clone of a source branch is registered.** Its `Clone` names the branch's directory (`<source>/tree/<branch>`) and
  the branch in `ref_name`; `branch_name` is the branch an operation creates, and is unset for a clone into a dataset of
  its own. The door read the wrong field and found no claim at the branch's directory, so it refused every such clone.
- **A task that finds its claim held on every delivery is given up uncounted.** The audit and the scan share one claim
  (the first audit's fix), so an audit fired behind a long scan spent its deliveries waiting and was then ended and
  counted as a task that failed. It is now terminated without the count on its last delivery; its next firing brings it
  back.
- **uvicorn's graceful bound in the maintenance pod is cut by the worker's drain.** The drain runs after uvicorn's wait,
  so the render's guard, which held the drain alone inside the grace, allowed preStop, a slow request and a full drain to
  outlast it.
- `CommittedFileOverwritten` reads a 25 h window, since a pass counts an overwritten file once and the table's next pass
  is a day out; the queue's schedule values admit no `@every` interval, which the worker's reassertion of a cadence every
  15 minutes might restart (not measured).

- **Every end of a clone deletes its tags before it frees the clone's location, the trash's end included.** The first
  audit put the deregister door and the warehouse cascade in that order; `trash.end` still released the claim first. With
  all three in that order the claim is the ender's own while its tags go, which is what makes comparing the location's
  holder by name sound.
- **The maintenance pod's sidecar blocks its shutdown for the grace less the teardown**, so it outlives preStop, uvicorn's
  bound and the worker's whole drain; at the fleet's 20 s it stopped about 15 s into a 90 s drain. Not measured: whether
  daprd, with no app health check configured, holds the pod in Terminating for that whole time once the app has exited.
- A task that names a table at a location another table holds no longer deletes that table's due record, and the
  worker's bus credential is named as `nats.auth.users` issues it, whatever maintenance's Dapr app id.
- The register door's check that a location is free, which the first audit's fix added ahead of the backend call,
  answered a table registered again at its own location 400; it reaches the backend's `TableAlreadyExists` (409) and the
  converge path again.

### The review of that change (2026-11-09)

Two fresh reviewers read the change before it was committed. What they found is fixed in it:

- **Registering a clone asks `can_read_data` and `can_create_tag` on each source, in that order.** The owner's decision
  put the tag door's rung on the door, and the change had replaced the read check with it. In the model
  `can_create_tag` is `owner or publisher`, and a publisher is no reader: it moves a pointer. A publisher who could not
  read a table registered a clone of it and, as the clone's owner, read its rows (shown on a real OpenFGA: 200). A caller
  holding nothing on a source is refused `can_read_data required on <root>` whether a governed table is there or not.
- **The sources are asked in the order the clone reads through them.** A source no table holds, behind a governed one,
  was refused first, which told a caller with no rights on the governed one what it is a clone of.
- **The register door's own undo deletes the clone's tags before it frees the location**, as every other end does, and
  **a late end forgets no record a newer holder of the location owns**: the base record is keyed by the location, as the
  tag is named for it.
- **A location whose holder is gone is registered under another name again.** The check added ahead of the backend call
  refused any location another name's claim stood on, standing table or not, so a claim an end left behind kept the
  location for that name for good.
- **A pass reads the uuids its table's policy is kept under once.** They were read to find the policy and again to name
  the dataset; a claim read that failed the first time and answered the second ran the pass with no policy read, under
  the defaults (shown over moto).
- **The bucket's owner is read off the control root and the project's policy off the policy root.** Both were read from
  the policy root, so a deployment keeping them apart lost every project-level hold.

## Amendment, 2026-11-10: the worker runs in pods of its own ([[LH-494]], the second change)

The owner's decision of 2026-11-09 (the queue worker leaves the web app's lifespan for a Deployment of its own from the
same image; the web Deployment keeps the write-event lane), as built:

- **One service, two Deployments, one identity.** The worker pods keep the ServiceAccount, the Dapr app id and the store
  and bus credentials the single pod had, so nothing gains access it lacked, and no second Dapr identity is built ahead
  of the move off Dapr (ADR 0199). Lakekeeper runs its API pods and its worker pods from one binary under one identity
  (`docs/docs/table-maintenance.md`, `configuration.md` at `e9b202d`). A sidecar subscribes only to what its app
  declares, and the worker declares nothing, so the write-event lane's deliveries reach the web pods alone.
- **The worker is a process (`python -m maintenance.worker`), owned by its `main` and not by a lifespan.** It serves no
  API, only `/livez` and `/readyz`, from a thread: uvicorn on the main thread takes SIGTERM for itself and would stop
  answering as the drain begins.
- **The sidecar outlives the drain and leaves with the worker.** daprd ends its shutdown block early only when its app
  turns unhealthy (`pkg/runtime/runtime.go:541-566` at v1.19.0-rc.4), which it learns from a health check; measured on
  that binary, 3 s after the app with the check and the whole block without. Under the block alone a maintenance pod
  stayed Terminating 113 s on every rollout (helm revision 33). The check is on the worker pod only, since it also gates
  a sidecar's subscriptions.
- The web pods take the fleet's grace and uvicorn bound again: nothing in them drains longer than a request.
- **Each process starts only what it uses.** The worker holds the signing key, the sidecar client and the two
  emitters; the web process holds none of them, asserts the app token its delivered route needs, and connects the queue
  only while the write-event lane is served. The lane no longer waits on a signing key: the process that serves it
  signs nothing.
- **The worker's liveness is its own loop's.** Served from another thread, `/livez` would prove only that the probes'
  loop turns, so the worker's loop marks a beat each second and `/livez` fails once the last is 30 s old. A worker asked
  to stop while it is still starting stops there (its first bus connection is retried without end), and one that has
  not started within 240 s ends, to be started again. A task a pull answers as the worker stops is handed back at once
  rather than started under the drain, while it has a delivery to spare; on its last it is handled where it arrived, since
  a hand-back there would spend the consumer's last delivery and leave the task on the queue for good.
- **The two Deployments share one Dapr app id and one ServiceAccount until the move off Dapr** (owner, 2026-11-10, after
  the cost was laid out). Stated costs: Dapr's operator keeps one `<app id>-dapr` Service per app id and rewrites its
  selector and owner from whichever Deployment it reconciled last (`pkg/operator/handlers/dapr_handler.go:204-250` at
  v1.19.0-rc.4), so `maintenance-dapr` changes hands at rollouts, and nothing in the estate reads it; and the secret
  store scopes a sidecar's reads by app id and binds the role to the ServiceAccount, so each kind of pod can read what
  the other reads. A separate ServiceAccount, store policy, bus user and store role per pod kind belongs to the row that
  rebuilds those identities (XC-199), since the bus user table is keyed by app id and `maintenance-worker` is already
  the worker's own user there.

Not fixed here, and stated: the hourly audit and each pass's listing still grow with the estate and the table, and a
pass's read of its bucket's `__manifest` is one filtered scan whose bytes grow with the bucket's rows; store
events in production rest on an attestation nothing checks; the queue has no operator-visible task history, dead letter
or cancel; and four of the worker's recovery paths (a refused enqueue of a fired schedule, a refused hold of an ended
task's next try, a fetch and a settle the bus fails) have no test, since each needs one request to fail on a real bus
and the lane's link can only cut a whole connection. Each is the second and third change's.

## Amendment, 2026-11-10: each table's orphan check is its own task ([[LH-494]], #638)

The daily scan walked every table's files inside one task, so it grew with the estate, and a scan past twice its bound
ended its replica on every delivery, after which neither the listing floors nor the purge ran. Decided by the owner
through the multi-question tool, 2026-11-09 and 2026-11-10:

- **Each table's orphan check is its own task, on an adaptive cadence with a one-day floor** (2026-11-09). An `orphans`
  task per table (`maintenance.task.orphans.<uuid>`, held on `maintenance.schedule.orphans.<uuid>`) takes its table's
  claim (`table.<uuid>`), so it never runs beside the table's pass or another check of it. It applies the pass's guards
  with the pass's keyed reads, scans the table's main dataset, raises the listing floor on its own findings, and holds
  its next check. Its bound is its own (`maintenance.orphanCheck.maxSeconds`), and a table whose live versions
  reference more files than one scan probes is excluded, as one past 500 versions is.
- **The ceiling is 30 days and configurable** ("30 days is fine", 2026-11-10): `maintenance.orphanCheck.ceilingDays`,
  at least 1. The floor F is fixed at a day.
- **The rule.** Stranded or undecided files hold the next check at F; a raised floor at max(F, the table's retention +
  a day), since the floor moves only once a pass cleans the manifests beneath the raise; a clean check, or one finding
  only files Lance reclaims itself, doubles the interval it ran at up to C; an excluded table, or a bucket maintenance
  does not govern, waits C; a table no `__manifest` row names waits F; a table gone or in the trash is checked no more;
  a failed read raises, the worker retries, and a check it ends is held again at F. The interval rides the schedule's
  body (`Task.interval_hours`): a lost schedule means the table is checked at F again. A table's pass holds its first
  check a day out unless one is held (a publish expecting the schedule's subject empty), and the hourly audit holds
  one, a jittered time within a day out, for every live table holding neither a check's schedule nor a queued check.
- **The purge's gate is the drift report only** ("Drift report only", 2026-11-10). The gate lost the orphan files'
  blocking count, the scan's `storage:datasets` incomplete source, its unavailable category and the coverage-gap skip.
  None protected a trashed table's bytes: the purge deletes only under the trashed location, by key, and refuses a
  location a live table equals, contains or sits inside; another live table's unreferenced files sit under that table's
  own prefix, and a table reading through the trashed one is excluded from any scan by its base-path flag and protected
  by the clone tag and the base registry instead. Read from the code, the orphan part could also block the purge for
  good: a trashed table holding a stranded file (no pass and no raise touches a trashed table), an unnamed dataset with
  stranded residue, and a dataset whose raise stamp refused every later raise. The daily scan keeps the storage walk
  (discovery, `unregistered_datasets`, `absent_datasets` and the walk's own incomplete notes, which still gate) and the
  purge, which consumes the report its own run produced; the gate refuses a report whose run walked no storage.
- **No stored per-table result and no per-bucket storage totals** ("Drop it, log per table", 2026-11-10): each check
  logs one `maintenance_orphan_check` line (the table, the outcome, counts and bytes by orphan class, the reason when
  excluded, the interval chosen) and counts `maintenance.orphans.checks` by outcome and `maintenance.orphans.stranded`.
- **What stays excluded**: tables with branches, MemWAL, an unsupported flag or files through a base. The scan subtracts
  the main branch's references from a listing, which those layouts break; a branch-aware check is not built.

Two fixes every per-table check needed. A file the store cannot place before the scan began reading its referenced set
is no finding, since a commit landing during the check writes files that look stranded and age is the only evidence an
unverified file has (`lance_docs/lance/guide/read_and_write.md:564-571`). And a table carrying the raise's stamp is raised
again once its floor has moved past the stamp, instead of never again; the stamp carries the epoch to the fraction, so a
manifest written earlier in the stamp's second cannot read as having passed it.

Measured 2026-11-10 on pylance 13.0.0 (one unreferenced file planted in each directory an hour either side of the floor,
one cleanup with `delete_unverified=True`): `data/` and `_indices/` files above the floor removed, `_transactions/` and
`_deletions/` ones kept, every one below it removed. A floor raised a second time, over a floor that had moved past the
first raise, made one cleanup remove the transaction file stranded after the first raise.

Not measured, and stated: the per-table request count on a real store; how long a check of the estate's largest table
takes against `orphanCheck.maxSeconds`.

A table's pass cleans only once `maintenance.minVersionsToExpire` versions are reclaimable (20 by default), which on a
table with fewer would leave a raise without the cleanup it was written for. So while a raise stands above the floor
(`floor.raise_stands`: the stamp is later than the earliest retained manifest) the pass cleans from the first
reclaimable version, under the table's own retention as ever.

Review, 2026-11-10 (two reviewers, twelve findings, all fixed in the change). The ones that changed the design: the
gate refuses a report whose run walked no storage, since such a report carries none of the walk's notes and the dropped
coverage-gap skip had been the only thing refusing it; a check lists its table a page at a time and excludes one past
100,000 listed files, since a ceiling on the referenced files alone does not see what a writer that never commits
leaves (measured: 1.4 KiB held per unreferenced file); and the audit holds a missing check only where none is held
(`schedule_first`), so it cannot replace the next time a check that fired meanwhile has just held.
