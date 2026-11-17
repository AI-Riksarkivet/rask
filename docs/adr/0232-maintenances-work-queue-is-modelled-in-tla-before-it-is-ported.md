# 0232. Maintenance's work queue and a table's pass are modelled in TLA+ before they are ported (2026-11-17)

**Status:** accepted; the fourth model of the port (ADR 0217 §5, ADR 0221). It describes the Python maintenance worker
as it runs today, before any of it is ported to Rust. Its two findings were fixed in the worker the same day (§4).

## Context

Maintenance runs each table's pass as a task on a JetStream work queue that every replica pulls (ADR 0198 §2 and its
amendments of 2026-11-09). What keeps two passes of one table apart, and what keeps a table from being forgotten, is
spread over four places: the stream's one message per subject, the delivery's ack wait and the worker's heartbeats, a
claim in the `MAINTENANCE_CLAIMS` bucket, and the schedules, follow-ups and hourly audit that put a task back on the
queue. The code argues each in comments and pins the bus's behaviour by measurements in those comments. Nothing checked
them against each other.

## Decision

1. **`specs/maintenance/WorkQueue.tla` models one table's task and the workers that pull it.** Each action is one effect,
   in the code's order:
   - the queue (`maintenance.services.queue`): `TaskQueue.enqueue`, a guarded publish refused while a task is queued
     or in flight; a delivery and its ack wait; a nak, ack or term applied by sequence;
   - a worker's handling (`Worker._handle`): the pull, the claim (`_claim`, `TaskQueue.claim`), the give-back while
     another's claim stands (`_give_back`), the pass (`tasks.run_task`, `table_pass.run_table_pass`: it reads the table,
     ends with no next time for a table the trash keeps, and otherwise holds its next time with `schedule_at`), the ack
     or the retry or the end (`_retry_or_end`, `_end`), and the claim's release last;
   - the bus's clocks: the ack wait running out on a delivery no live worker vouches for, a claim aging out one ack wait
     after its last renewal, a settle the network held applied late, and a task no delivery is left for dropped at the
     stream's 24 h max age;
   - the schedules (`Worker.run_due`, `_move`): a fired schedule or follow-up moved onto the queue once no task holds
     the table, and naked meanwhile; a time a pass held fires whenever it comes, while that pass still runs included;
   - the commit lane (`arrival.Arrivals.hold_task`): an announcement holds the table's follow-up unless one is held; an
     undrop's announcement, a registration, holds its own, replacing the one held;
   - the hourly audit (`audit._queue_what_holds_nothing`): it queues a live table that neither a schedule, held or fired,
     nor the queue holds (`TaskQueue.held`);
   - a drop, and an undrop that announces the table before it clears the trash record
     (`api/v1/endpoints/tables.undrop_table`, `filing.send(restored)` at 1593 before `trash.clear` at 1595).

   Faults are actions counted against a bound: a worker that dies between any two effects (a crash, a hung pass, a
   drain outrun, a heartbeat or renewal the bus stopped answering), a settle sent and not answered and held by the
   network, a pass that raises, a next time the bus refuses, and an announcement the lane gives up.

2. **What it proves** (`make tla-check`, four workers; times measured 2026-11-17 on four cores shared at a load of 6):
   - `WorkQueue.cfg` (two workers counted as interchangeable, two faults, two deliveries a task, a commit): 109,169
     states, every invariant below, 4 s.
   - `WorkQueueLive.cfg` (the same with one fault, the workers distinct): 47,714 states, the invariants and the three
     properties over a whole behaviour, 15 s.
   - `WorkQueueUndrop.cfg` (a drop and its undrop, one fault, symmetry): 134,894 states, the invariants, 4 s.
   - `WorkQueueUndropLive.cfg` (a drop and its undrop, no fault, a commit, the workers distinct): 13,730 states, the
     invariants and the three properties: both guards of §4 together over a whole behaviour, 6 s.
   - `WorkQueueLiveWide.cfg`, ON DEMAND (both guards together with a fault: one fault, a commit, a drop and its undrop,
     the workers distinct): 263,303 states, the invariants and the three properties, 72 s.
   - `WorkQueueWide.cfg`, ON DEMAND (three workers counted as interchangeable, three faults, a commit, a drop and its
     undrop): 2,975,446 states, the invariants, 106 s on two TLC workers.

   The gate's maintenance configs, the six that must fail included, run in about 35 s.

   The invariants, each a claim the code makes:
   - `OnePassPerTable`: two passes of one table never run at once, a bus cut, a late settle and a restart included
     (`Worker`'s docstring, queue.py 827-828).
   - `ThePassHoldsTheClaim`: a running pass holds its table's claim (`_claim`, `_renew`).
   - `HeldUnlessBackstopped`: something on the bus will pass a live table (its task, a schedule held or fired, a
     follow-up, an announcement still to be read) unless a fault or a give-up happened that the code leaves to the audit
     (`run_task`'s docstring; `_end`).
   - `ACommitIsCovered`: after a commit, a pass that reads the table after it is on its way: the announcement, its
     follow-up, a give-back's minute, or a task not yet read (`queue.py` module docstring; `run_due`).

   The properties, once faults stop:
   - `TheTableStaysHeld`: no live table is forgotten; from some point on something always holds it.
   - `EveryCommitIsRead`: every commit is read by a later pass, unless the table goes.
   - `NothingDeadBlocks`: a pass that died leaves nothing that blocks the next for ever; its claim ages out and its
     delivery is handed on.

   Two passes of one table cannot both commit, because they never run at once (`OnePassPerTable`) under A1. Where A1
   fails, the two run beside each other (`WorkQueuePaused.cfg`), and what is left is ADR 0198's measurement: two
   cleanups of one table at once, 60 rounds on disk and S3 on pylance 13.0.0, harmed no table. The model does not
   model the pass's effects on the store, so it does not prove that.

3. **Six assumptions the design rests on are configs that must fail, and do** (each first line `EXPECT VIOLATION`):
   - A1, `WorkQueuePaused.cfg` (`OnePassPerTable`, a trace of 9 states): a process paused past its lease runs on; the
     ack wait runs out and the claim ages out under it, and another worker passes the table beside it.
   - A2, `WorkQueueNoClaim.cfg` (`OnePassPerTable`, 15 states, one fault): without the claim, an ack the network held
     past its worker's end is applied to the next worker's delivery, frees the subject under that pass, and a fired
     schedule starts a second pass of the table.
   - A3, `WorkQueueDueLimit.cfg` (`ACommitIsCovered`, 9 states, no fault): with a delivery limit on the fired schedules'
     consumer, a commit's follow-up is given up while a pass that read the table before the commit still runs.
   - A4, `WorkQueueNoAudit.cfg` (temporal, 1,865 states, one fault): without the hourly audit, a table whose first
     announcement the lane gave up is held by nothing for good.
   - A5, `WorkQueueLateSchedule.cfg` (`HeldUnlessBackstopped`, 10 states, no fault): the first finding's guard removed (§4).
   - A6, `WorkQueueUndropFollowUp.cfg` (`HeldUnlessBackstopped`, 12 states, no fault): the second finding's guard
     removed (§4).

4. **Two findings, each CONFIRMED against the running worker and FIXED on 2026-11-17.** Each was shown by
   `tests/integration/test_a_live_table_stays_held_on_the_bus.py`: the real worker, task runner and commit lane on the
   pinned nats-server 2.15.2 (`scripts/nats_server.sh`), its streams, consumers and claims bucket declared as the chart's
   stream Job declares them, one step held at the moment the trace names, and the harm read as the audit reads it
   (`TaskQueue.held` over the table's schedules and its task). Each test failed on its harm assertion five runs of five
   before its fix, and passes five runs of five after it; with the fix reverted it fails again. Neither finding lost
   data or let two passes overlap: each left the table to the hourly audit (`auditSchedule` `@hourly`,
   `chart/values.yaml` 786), its next pass up to 60 minutes late, 30 on average.
   - **`WorkQueueLateSchedule`, FIXED: a fired schedule waits for the task that holds its table** (A5). The trace: the
     worker pulls the task and claims the table. The pass reads the table and counts its next time from its start
     (`table_pass.run_table_pass`): `now + _SOONEST` (60 s) for a dataset due during the pass, or `now + retry_seconds`
     (900 s) for one it could not decide. A pass longer than that holds a time already past (`tasks.run_task`,
     `TaskQueue.schedule_at`), which fires at once: measured on the pinned server, a past `@at` fired 248-261 ms after
     its publish was answered, 20 of 20. The move finds the pass's own task in flight, the guarded publish is refused,
     and the fired schedule was acked as covered; the pass then acked and released its claim, and nothing held the table.
     The harm needed the pass's ack answered more than about 250 ms after its next time was held, as on a bus that
     answers slowly; unheld, the ack won 30 of 30 on one local server.

     The guard: `Worker._move` (`services/maintenance/src/maintenance/services/queue.py` 1298-1299) naks a fired
     schedule the queue refuses while a task holds its table, `_HELD_TABLE_RETRY` (60 s, queue.py 134) out, for every
     kind a task of which holds its own next time (`_SCHEDULED_KINDS`: table, trash record, orphan check), as it
     already did a fired follow-up; the `maintenance-due` consumer redelivers for ever (A3), and measured on the pinned
     server a fired schedule naked with a delay on it came back after the delay on each of four deliveries. A cadence's
     firing (the audit's, the scan's) is still acked as covered: the cadence holds its next firing itself. The audit
     reads a fired schedule waiting on its `due` subject as holding its table (`TaskQueue.held`, queue.py 660-678), so
     it queues no pass beside one. Proof the guard is needed: `WorkQueueLateSchedule.cfg`. Its test:
     `test_a_pass_that_holds_a_time_already_past_leaves_its_table_held`, which holds the pass's ack until the move has
     settled the fired schedule. Its cost: one more pass of a table whose schedule fires while another task passes it,
     up to a minute after that task ends.
   - **`WorkQueueUndropFollowUp`, FIXED: an undrop's announcement holds a follow-up of its own** (A6). The trace: a
     write's announcement holds the table's follow-up. The table is dropped. The undrop announces it and the lane
     answered `covered`, since the write's follow-up was still held. That follow-up fires between the announcement and
     the clear, is moved onto the empty queue, and its pass reads the table in the trash and holds no next time
     (`table_pass.run_table_pass`, `tasks.run_task`). The undrop then clears the record, and nothing holds the table. It
     needs the table to have no schedule of its own at the undrop (a table dropped before its first pass, or one whose
     schedule fired while it was dropped) and the clear to land after the older follow-up's fire and its pass's trash
     read: measured on the pinned server, a follow-up's pass started 3 to 13 ms after its time, 10 of 10.

     The guard: the lane maps a registration (`register_table`, which both undrop doors announce) to a follow-up that
     replaces the one held (`arrival.lane_task`, `services/maintenance/src/maintenance/services/arrival.py` 123, and
     `Arrivals._held`, 196), published without the guard (`TaskQueue.follow_up(..., replacing=True)`, queue.py 631-633),
     and answers `held`. Measured on the pinned server, a held follow-up replaced this way fired once, at the newer time,
     with the newer body. The undrop's own follow-up waits out `commitPassDelaySeconds` (300 s), which outlasts the
     door's two writes; the catalog's undrop door keeps its order (announce, then clear), whose outbox files the
     announcement before the clear on purpose ([[LH-576]]). Proof the guard is needed: `WorkQueueUndropFollowUp.cfg`.
     Its test: `test_an_undropped_table_is_held_when_an_older_follow_up_fires_before_the_clear`, which holds the clear
     until the write's follow-up's time has passed and the bus is quiet. Its cost: the older follow-up's pass comes up
     to `commitPassDelaySeconds` later; it still reads every commit it covered.

   **The four variants checked before the fix.** Each was a constant of the model, checked under its finding's setting
   with one fault, a commit and the workers distinct, every invariant and the three properties: `fromEnd` (the next
   time counted from the pass's end) held, 64,837 states, but only while the ack is answered inside `_SOONEST`, and the
   chart sets no ceiling on `ackWaitSeconds`; `nak` held, 64,669 states; `afterClear` (the undrop announcing after its
   clear) held, 268,484 states, but changes the catalog's undrop door, whose outbox files the announcement before the
   clear on purpose; `own` held, 335,996 states. `nak` and `own` were chosen: maintenance's own change, resting on a
   JetStream behaviour measured on the pinned server rather than on a timing argument. The rejected two and their
   configs were removed from the model with the fix. Since the fix, the audit's read of a fired schedule waiting on its
   `due` subject is in the model too (`Audit`), which is why the gate's counts are lower than these.

5. **The model was shown to bite** (before the fix). Six defects were planted one at a time in a copy, and each was
   reported: a claim created over a standing one (`OnePassPerTable`, 14 states), a claim that ages out while it is
   renewed (`ThePassHoldsTheClaim`, 7), a pass that holds no next time (`HeldUnlessBackstopped`, 8), a fired follow-up
   acked as covered like a schedule (`ACommitIsCovered`, 9), a give-up on the last delivery that holds nothing
   (`HeldUnlessBackstopped`, 10), and a dead worker's claim that never ages out (the properties over a whole behaviour,
   on `WorkQueueLive.cfg`). The two findings' configs are the same check for the fix's two guards.

## What the model assumes

- **A1.** A worker whose lease runs out is gone, every thread of it, before JetStream hands its delivery on or its claim
  ages out. Held by `Worker._tell` and `_renew` (queue.py 1101-1142), which end the process before the lease runs out,
  `_end_process` (877-883, `_VOUCH_MARGIN` 149 and `_EXIT_RESERVE` 152), the claim's max age being the ack wait
  (`chart/templates/nats-stream-job.yaml` 169) counted from a renewal sent no earlier than the lease, and the render's
  refusal of a heartbeat past half the ack wait (166-167). It is a timing argument: a process paused past its lease
  (a stopped node, a frozen VM) breaks it, and nothing the store arbitrates stands behind it.
- **A2.** The claim exists, is created only where none stands, and is renewed and released only under its holder's token
  (queue.py 482-589, 1023-1050).
- **A3.** The fired schedules' consumer redelivers for ever (`maintenance-due`, `max_deliver -1`,
  `chart/templates/_helpers.tpl` 416), so a fired follow-up or schedule waits out any pass.
- **A4.** The hourly audit runs (`maintenance.queue.auditSchedule`, `chart/values.yaml` 786; asserted again every 15
  minutes, `tasks.py` 200-215).
- **A5.** A fired schedule that finds a task holding its table waits for it, never acked as covered by it
  (`Worker._move`). A pass counts its next time from its start, so the time it holds may fire before it acks; nothing
  bounds that by timing.
- **A6.** An undrop's announcement holds a follow-up of its own, replacing an older one (`arrival.lane_task`,
  `TaskQueue.follow_up`), and that one fires only after the undrop's clear: its delay, `commitPassDelaySeconds`
  (300 s), outlasts the door's two writes. The second half is a timing argument; an older follow-up may fire between
  the two, which the first half makes harmless.
- One message per subject on the task stream (`max_msgs_per_subject 1`, `discard_new_per_subject`,
  `nats-stream-job.yaml` 151) and the guarded publish (queue.py 142, 446-464), as measured in queue.py 135-140.
- The task consumer allows one delivery past `maxDeliver` (`_helpers.tpl` 411-413), and a task whose worker died on
  every one stays on the queue until the stream's 24 h max age (`nats-stream-job.yaml` 151).

## Not modelled

The pass's effects on the store (expiry, the due record, the committed files, the cadence stamp, the history fence),
which are one step here; the orphan check, which shares the table's claim, and the trash task, on subjects of their own
(both now wait as a table's fired schedule does); the audit and the scan's shared claim and their cadences; the
store-event lane, a second source of follow-ups like the commit lane; a registration that is no undrop (it replaces a
held follow-up the same way); a task still deliverable that the stream's 24 h max age drops; the drain's hand-back; the
bus session's renewal; and how long any of it takes.

## The four facts

- **GitOps**: met, by both fixes. The streams, the consumers and the claims bucket, with every value A1 to A6 rest on,
  are the chart's stream Job's, and the worker creates none. Neither fix adds a value or an object: the nak's delay is
  the minute a fired follow-up already waited (`_HELD_TABLE_RETRY`), which no operator tunes, since a fired schedule
  waiting costs one refused publish a minute and the chart renders no value of that meaning; `commitPassDelaySeconds`
  is the chart's.
- **Cloud native**: met, by both fixes. Any number of replicas compete for the tasks and the fired schedules, a
  replica that cannot vouch for its task ends and Kubernetes restarts it, and a dead replica's task goes to another; a
  waiting fired schedule is a message on the bus, not state in a replica.
- **Lance-namespace/table-idiomatic**: met, untouched by both fixes. The pass's commits are the catalog's, through its
  doors (ADR 0204), and the catalog's undrop door keeps its order.
- **Zero trust**: partly met, unchanged by both fixes. The queue and the claim are written by one bus user,
  `maintenance-worker`, and the claim's token is no credential: mutual exclusion of two passes is a lease (A1), not a
  write the store refuses. No fencing token rides a pass's writes to the store, and the history fence admits two
  reclaims at once (`lakehouse.history_fences`), so a paused pass that resumes writes beside the next one. ADR 0198's
  measurement that two cleanups at once harm no table is what bounds that.

## Consequences

- The Rust worker keeps these steps in this order: the claim before the pass, the next time before the ack, the ack
  before the claim's release, every settle answered inside the lease or the process ended, a fired schedule that meets
  its table's task waiting for it, and a registration's follow-up replacing the one held. A change to that order
  changes this model first.
- What an operator sees: `maintenance.arrivals.settled` counts an undrop's or a registration's announcement as `held`
  where it counted `covered`; the `maintenance-due` consumer's ack-pending gauge (`maintenance.queue.ack_pending`,
  consumer `maintenance-due`) reads 1 more for each fired schedule waiting for a pass, for up to a minute past that
  pass, and its redelivered gauge 1 more while a delivery of it after the first is out (measured on nats-server 2.15.2,
  2026-11-17: a message naked with a delay is ack-pending, not redelivered, until it is delivered again, and stays on
  its subject); the audit's read of what holds a table counts that subject; and a table whose schedule fired during a
  pass is passed once more after it. No alert reads the `maintenance-due` consumer's gauges.
- `make tla-check` runs this model's gate and expected failures in about 35 s on four cores.
