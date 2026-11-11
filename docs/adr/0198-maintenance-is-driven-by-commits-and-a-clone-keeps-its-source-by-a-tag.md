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
  revision with each heartbeat, released only by its holder (at the revision it holds, or, where a renewal the bus
  applied lost its answer, at the revision its own token stands at, the heartbeat ended first), and gone one ack wait
  after its last renewal. A pass that finds another's claim waits it out, and a stale holder's late renewal or release
  cannot match the revision standing (measured on nats-server 2.15.2, three nodes, R3, 2026-11-09). The owner asked for it to be fixed
  rather than documented (2026-11-09), though two cleanups of one table at once were measured harmless (pylance 13.0.0:
  60 rounds on disk and S3, no table harmed). A heartbeat-extended deadline survives a consumer leader change (measured: a
  task redelivered at the extended deadline after a step-down). A pass is a thread nothing can
  stop, so it is the process that ends: a bus outage longer than the ack wait restarts each replica that was mid-pass,
  which has no work without the bus and may wait out a crash-loop backoff once it is back. For the same reason a stopping
  replica whose drain ends with a pass still running ends there, and a task is only ever run off the pull it answered:
  each pull has a subscription of its own, so a pull a reconnect held back, answered after the worker gave up on it,
  delivers to an inbox no one reads. A pass past twice `maxPassSeconds` ends its replica, as does a heartbeat that ends by an error nothing expected, and a task that
  outlives its worker on every one of `maxDeliver` deliveries is ended unrun on one more, which the consumer allows. The
  backstop is the hourly audit, which opens no table and queues each table whose schedule was lost, and each trash record
  still to be released (one released, or carrying no deadline, has no task to run); the orphan scan and the trash purge
  run once a day (owner, 2026-11-09). The stream Job reads MAINTENANCE_SCHEDULES back and fails unless it allows message
  schedules: a server older than 2.12 drops the field and reports the stream created (measured, nats-server 2.11.10,
  2026-11-10), after which no schedule fires. A pass reads the trash again once it holds the table's history fence, before
  it reclaims; the catalog's drop door takes no fence (decided 2026-11-10: a drop refused while maintenance cleans costs
  more), so a drop landing after that read is followed by one cleanup under the table's own policy.
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
(`explain_cleanup_old_versions` and `versions`, [[LH-562]]), bounded by the table's retention: a pass opens each dataset once before its expiry and once after, and counts once where it holds no vend (measured on moto 2026-11-10: 50 requests for a commit's pass of a table of 4 versions, 155 for one of 25).
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

## Amendment, 2026-11-10: the queue can be seen ([[LH-494]], #638)

What the queue does is now measured, and alerted on (`deploy/alerting/rules.yml`, group `maintenance`). Counted by the
worker: a task handed back after its run failed (`maintenance.tasks.retried`, by kind), a completed task's time from the
claim on its table to its acknowledgement (`maintenance.tasks.duration`), how each store event was settled
(`maintenance.store_events.settled`: held, ignored, retried, unreadable, ended) and a fired schedule the queue refused
(`maintenance.schedules.not_queued`). Sampled each minute by one more supervised loop of the worker
(`maintenance.services.queue.sample_queue`): each of the three streams' message count, the oldest message's age on
MAINTENANCE_TASKS and STORE_EVENTS, and each durable's deliveries awaiting an ack and redeliveries. The worker's bus user
gained exactly the three reads that needs it lacked: STORE_EVENTS's stream info and its three durables' consumer info.
The alerts: a task waiting six hours of the 24 the stream keeps it (`MaintenanceTaskAging`), a store event ended or
unreadable (`MaintenanceStoreEventsEnded`), store events at 90 % of their durable's delivery ceiling
(`MaintenanceStoreEventsStuck`), and a refused move standing an hour (`MaintenanceSchedulesNotQueued`). The counters and
the sampler are proved on the bus lane, the sampler under the grants the render issues.

Not built, and stated: an alert on one replica crash-looping (pod restarts are kube-state-metrics', the platform's, which
this rules file reads nowhere); an operator's view of a task's history, a dead-letter of ended tasks, and a way to cancel
a task.

A governed bucket without store events is reported (the reporting half of decision 4). Measured on RustFS 1.0.3
(2026-11-10, a throwaway store booted through Dagger with a NATS notify target): `GetBucketNotificationConfiguration`
answers a bucket's configuration as it was put and an empty one for a bucket with none; the action checked is
`s3:GetBucketNotification` on the bucket's own resource (a user granted it on one bucket is refused 403 on another, and
`s3:GetBucketNotificationConfiguration` is refused as no action); maintenance's canned policy as it stood was refused 403
on every bucket. So the hourly audit, with events expected (the development store, whose fixture configures them, or
`objectStore.events.provisioned`), asks for every governed bucket's configuration (the configured buckets and every
active warehouse's), and counts each bucket once per audit on the counter `maintenance.store_event_buckets.checked` by
outcome: `configured`, `without` (no `s3:ObjectCreated:*` rule over the whole bucket, logged by name) or `unknown` (the
store refused or failed the read, logged with the error, never counted as configured). A counter, because it sums across
replicas, whichever runs an audit, and stops rising within two hours of a fix; and `unknown` alerts
on its own, because a store refusing the read on every bucket otherwise reported nothing missing
(`maintenance.services.bucket_events`; alerts `MaintenanceBucketWithoutStoreEvents` and
`MaintenanceBucketStoreEventsUnknown`, each on its outcome's two-hour increase; changed in the final review of the
second change, 2026-11-10).
Maintenance's canned policy gained `s3:GetBucketNotification` on every bucket, since the warehouse buckets are made at
runtime and no render names them. Not measured, and stated: the answer proves the configuration, not delivery. RustFS
took a rule naming a target it does not have (`arn:rustfs:sqs::nosuch:nats`) and answers it back as configured, and refuses
any rule while it has no notify target at all; whether a configured target's events reach `store.events` stays the
table's daily recheck's to back up. The event body the worker parses is stated where a platform store's operator reads it,
under `objectStore.events` in the chart's values.

## Amendment, 2026-11-10: a clone's end is finished from the record its door filed ([[LH-494]], #638)

**The defect.** Every door ending a registered clone removed the clone's row before it released the tag the clone holds
on each source (§1): deregister (`tables._detach`), a destructive drop (`drop_table`, `dataplane.destroy_table` then
`forget_base_record`), a namespace's destroying cascade (`namespaces._destroy_subtree`) and a warehouse cascade
(`warehouses._detach_table`). A store error on a source's `_refs/tags`, or the process dying, between the two left the
tag for good: a re-issued call answered 404, the row being gone, and the source stayed undestroyable. A re-issued
namespace cascade was worse: with the clone gone from the listing, its stranded tag read as a clone outside the subtree,
and the cascade was refused 409 `ReadFromOutsideError` for ever.

**The decision** (owner, 2026-11-10, through the multi-question tool: "Intent record"). Before a door removes a clone's
row it files the clone's ENDING (`lakehouse.clone_endings`, filed by `catalog.services.clone_endings.file_ending`): the
clone's location as the door read it, its table id, the uuid its claim recorded, each source root its base record names
as `derived_from`, and whether its base record goes with its bytes (a destructive drop, a destroying cascade, a purging
warehouse cascade) or stays for bytes left in place (deregister, a warehouse cascade keeping its bucket). A failure to
file refuses the door 503 code 17 before anything changes. A destructive drop and a destroying cascade file it through
`dataplane.destroy_table`'s `before_removal`, once every judgement of the drop has passed and right before the row goes,
so a refusal files nothing; a deregister and a warehouse cascade file it before they change anything. The door then
removes the row, releases the tags and the claim as before, and removes the ending last. A table that is no clone files
none. A door whose native call failed leaves the ending, whatever the failure: a describe cannot tell the clone's row
from one the same name registered at the location since (a registration between its native register and its claim's
take), so a door that judged by one could remove the ending of a clone whose row was gone and strand its tags. The one
ending a door removes after a failure is the warehouse cascade's, when the data-directory delete that precedes its
native call fails: that request never touched the row.

**No request releases a claim another table holds.** `location_claims.release` takes the uuid its caller owns the
claim for and, given one, frees only a claim recording it. The register door's undo passes the uuid it minted, so a
registration whose take was refused (another request fences the claim) frees nothing; the finish passes the uuid of the
claim it fenced; and the two cascades, which hold no fence between a table's row removal and its claim's release, pass the
uuid they identified the table by, so a same-name registration taking the location there keeps it. The other releases
own their claim another way: the drop door and deregister release by name under their own fence, which refuses every
other take while it stands; the create and declare undos act on a location named after a uuid they minted, which the
register door refuses to attach; data-directory claims record no uuid, and are released by name by the table the
location's claim names; and `lakehouse.trash.end` releases a claim the trash record keeps from every other take. A
release by name alone, landing after a same-name take, would leave a live table at a released location, open to a second
id over the same bytes.

**One object per filing, deleted once ended.** `_clone_endings/<sha256 of the decoded location>/<filing token>.json`:
the digest the location's claim and the tag are keyed by, and the filing request's token. Each object has one writer,
its door, and one other actor, a finish, which marks it under the ETag it read before acting. So an ending filed again
at the location is another object, never acted on by a finish that read an earlier one, and an ended ending is removed
with a plain delete: the estate asks no conditional delete of its store, and an object no one else writes needs none.
The listing is one list request, whatever was ended before: measured on a moto server, the first design (records
stamped `cleared_at` and kept) cost 21 requests for 10 cleared endings and 201 for 100 (a HEAD and a GET each, and the
listing); this one costs 1 for 10 and 1 for 100, and 1 with one standing beside 100 ended.

**Who finishes it.** Maintenance's store policy writes no source's `_refs/tags` and gains nothing. The catalog serves
`GET /management/v1/clone-endings` (each ending standing, by digest and filing) and `POST
/management/v1/clone-endings/{digest}/{filing}/finish`, both on `can_reclaim_trash` on the estate root, the `reclaimer`
role the chart's bootstrap hook already grants maintenance for the trash doors: no new relation and no new grant. Each
finish is recorded on the audit trail (`clone_ending_finish`, success or failure, the caller and the ending). The hourly
audit (and the daily scan, which share the step) lists them and asks the finish for each; a refusal is logged by the
ending's id at WARNING (`clone_endings_result`) and asked again next run, never fatal.

**The finish's judgement**, on its own reads (`catalog.services.clone_endings.finish`), and only on the claim it read:

- nothing until a claim's `LEASE` (15 min) has passed since the filing, the time a door's own fence holds the claim
  (409 code 19); the ending is then marked by this finish under the ETag it read, and a second finish refuses while that
  mark is younger than `LEASE` (409 code 19); an ending written since the read answers 409 code 14;
- the location's claim held for another uuid than the ending's: another table's location now (a registration between
  its claim and its refusal, below), so nothing is released and the ending stays (409 code 19);
- held for the clone, which stands there (`table_claims.holder_stands`: it resolves to the location, or the trash keeps
  it there; a read that fails raises, where `holder_is_gone` answers "not gone"): the door never removed it, or is still
  at work (a destructive drop deleting a large clone's bytes keeps its residue record meanwhile), so nothing is released
  and the ending stays (409 code 19). Once `STANDING_BOUND` (a day) has passed since the filing, the clone is taken to
  stand for good, its own end owning its holds, and the ending is removed with nothing released; so an ending a door
  left without removing its clone (it crashed before its native call) is not asked about for ever;
- held for the clone, which no longer stands: the finish FENCES the claim under the ETag it read
  (`location_claims.fence`), so no registration takes the location while it acts; a claim that moved since the read
  answers 409 code 14 with nothing released;
- released or absent: nothing to fence; a registration taking the location meanwhile refuses itself while the ending
  stands.

Then each source's tag is deleted through Lance's tag API (a source whose dataset is gone holds none), the base record
is forgotten where the door said, the fenced claim is released, and the ending removed last; a finish failing part-way
lifts its fence and leaves the ending for the next. The finish commits no table version and announces nothing, as the
doors' own tag release announced nothing.

**A registration at a location where an ending stands is REFUSED, 409 code 14** ("a clone's hold at this location is
still being released"), asked once the registration holds the location's claim (`refuse_registration_while_ending`).
Adopting the ending instead (removing it, since the new clone's `take` rewrites a tag of the same name) was the other
choice, and cannot be made safe: the new clone may read other sources than the old one did, whose tags would then stand
for no one, and a finish already past its judgement would delete the tags the new clone has just taken. With the
refusal, of a registration and a finish the second sees the first, and a finish acting on the clone's claim holds it
fenced, so no finish releases a new clone's tags or its claim. Create and declare cannot reach a location an ending
names: they lay a table out at `<namespace root>/<a uuid they mint>` (`dataplane.declare_table`), and the register door
refuses a location whose first segment is spelled as a uuid (`identifiers.is_spelled_as_a_uuid`), so no registered
clone, and no ending, is at one.

The design as first written had a standing table of any identity clear the ending and release nothing, cleared the
ending of a standing clone at once, released the claim by name on an unfenced read, and kept every ended ending. A
review round (2026-11-10) found each could release a live clone's hold, or strand one, or grow the listing for ever.

**Accepted costs.** A native call that failed and left the row keeps its ending: the finish answers 409 at every audit
while the clone stands, until `STANDING_BOUND`, and then removes it with nothing released. A door retried after a
failure files a second ending and removes its own, so the first stands until the next audit's finish (at most an hour
past `LEASE`), and a registration at the location is refused meanwhile. An ending whose clone a later door ended since
(a drop that failed with its row in place, then a deregister) releases nothing that door left held, and forgets no base
record of bytes that still stand: the finish forgets the record only where the ending says the bytes were to go AND no
Lance dataset stands at the location (`objectfs.is_lance_dataset_root`, at the key the location decodes to; a store that
cannot answer fails the finish closed). A purging warehouse cascade whose base-record step failed after its row went
keeps the record if the finish runs before a re-issued cascade purges the bucket, and nothing forgets it afterwards; no
worse than before, when the record and the tag both stayed. Once the bucket is gone the store answers that no dataset
stands (measured on moto: a location in a deleted bucket reads as no dataset) and the finish forgets it.

**What stays unfixed, by design.** A request stalled for longer than `LEASE` while it acts can be overtaken: a finish
judges an ending once `LEASE` has passed since its filing, as any take judges a fence once `LEASE` has passed since it
was set. A door acting on one clone for longer than `STANDING_BOUND` between filing and removing its ending could have the
ending removed under it. A clone kept in the trash under `trashPurge: false` keeps its holds: its trash record ends them
(`lakehouse.trash.end`), and that is the purge's switch. Pinned by
`test_a_clones_end_that_cannot_delete_its_tag_is_finished_by_maintenance` (deregister, a destructive drop, a destroying
namespace cascade), `test_a_cascade_that_cannot_delete_a_clones_tag_keeps_the_clones_location_until_the_finish` (the
warehouse cascade), `test_a_door_refused_before_its_row_would_go_files_no_ending`,
`test_a_finish_keeps_the_base_record_of_bytes_a_later_deregister_left_in_place`,
`test_a_purging_cascade_whose_clone_ending_cannot_be_filed_deletes_nothing`,
`test_a_purging_cascade_that_fails_on_a_clone_removes_its_ending_only_if_it_never_reached_the_row`,
`test_a_registration_refused_by_another_requests_fence_releases_nothing`,
`test_a_destroying_cascade_releases_only_the_claim_of_the_table_it_identified`,
`test_a_cascade_releases_only_the_claim_of_the_table_it_identified`,
`test_a_release_naming_its_uuid_leaves_a_claim_the_same_name_took_since`,
`test_a_finish_releases_nothing_when_the_clones_claim_moved_after_its_read`,
`test_a_finish_leaves_the_ending_of_a_clone_its_door_is_still_ending`,
`test_a_door_whose_native_call_was_attempted_leaves_its_ending_for_the_finish`,
`test_a_finish_that_may_not_act_releases_nothing_and_leaves_the_ending`,
`test_a_registration_while_a_clones_ending_stands_is_refused_until_it_is_finished`,
`test_the_audit_asks_the_catalog_to_finish_every_clones_ending_it_lists` and the two doors' rows in
`test_a_door_the_router_leaves_open_decides_for_itself.py`.
