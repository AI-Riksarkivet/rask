# 0232. Maintenance's work queue and a table's pass are modelled in TLA+ before they are ported (2026-11-17)

**Status:** accepted; the fourth model of the port (ADR 0217 §5, ADR 0221). It describes the Python maintenance worker
as it runs today, before any of it is ported to Rust.

## Context

Maintenance runs each table's pass as a task on a JetStream work queue that every replica pulls (ADR 0198 §2 and its
amendments of 2026-11-09). What keeps two passes of one table apart, and what keeps a table from being forgotten, is
spread over four places: the stream's one message per subject, the delivery's ack wait and the worker's heartbeats, a
claim in the `MAINTENANCE_CLAIMS` bucket, and the schedules, follow-ups and hourly audit that put a task back on the
queue. The code argues each in comments and pins the bus's behaviour by measurements in those comments. Nothing checks
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
   - the schedules (`Worker.run_due`, `_move`): a fired schedule moved onto the queue, or acked as covered while a task
     holds the table; a fired follow-up moved once the table is free, naked meanwhile;
   - the commit lane (`arrival.Arrivals.hold_task`): an announcement holds the table's follow-up unless one is held;
   - the hourly audit (`audit._queue_what_holds_nothing`): it queues a live table that neither a schedule nor the queue
     holds;
   - a drop, and an undrop that announces the table before it clears the trash record
     (`endpoints/tables.undrop_table`, `filing.send(restored)` at 1598 before `trash.clear` at 1599).

   Faults are actions counted against a bound: a worker that dies between any two effects (a crash, a hung pass, a
   drain outrun, a heartbeat or renewal the bus stopped answering), a settle sent and not answered and held by the
   network, a pass that raises, a next time the bus refuses, and an announcement the lane gives up.

2. **What it proves** (`make tla-check`, four workers):
   - `WorkQueue.cfg` (two workers counted as interchangeable, two faults, two deliveries a task, a commit): 137,169
     states, every invariant below, 3 to 5 s.
   - `WorkQueueLive.cfg` (the same with one fault, the workers distinct): 64,837 states, the invariants and the three
     properties over a whole behaviour, 16 to 28 s.
   - `WorkQueueUndrop.cfg` (a drop and its undrop, one fault): 177,828 states, the invariants, 4 to 7 s.
   - `WorkQueueWide.cfg`, ON DEMAND (three workers, three faults, a drop and its undrop): 3,510,596 states, the
     invariants, about 70 s. Checked once with three deliveries a task and two commits: 12,282,771 states, 201 s, no
     error.

   The invariants, each a claim the code makes:
   - `OnePassPerTable`: two passes of one table never run at once, a bus cut, a late settle and a restart included
     (`Worker`'s docstring, queue.py 796-798).
   - `ThePassHoldsTheClaim`: a running pass holds its table's claim (`_claim`, `_renew`).
   - `HeldUnlessBackstopped`: something on the bus will pass a live table (its task, a schedule, a follow-up, an
     announcement still to be read) unless a fault or a give-up happened that the code leaves to the audit
     (`run_task`'s docstring, tasks.py 53-54; `_end`, queue.py 1127-1129).
   - `ACommitIsCovered`: after a commit, a pass that reads the table after it is on its way: the announcement, its
     follow-up, a give-back's minute, or a task not yet read (`queue.py` module docstring 22-26, `run_due` 1142-1144).

   The properties, once faults stop:
   - `TheTableStaysHeld`: no live table is forgotten; from some point on something always holds it.
   - `EveryCommitIsRead`: every commit is read by a later pass, unless the table goes.
   - `NothingDeadBlocks`: a pass that died leaves nothing that blocks the next for ever; its claim ages out and its
     delivery is handed on.

   Two passes of one table cannot both commit, because they never run at once (`OnePassPerTable`) under A1. Where A1
   fails, the two run beside each other (`WorkQueuePaused.cfg`), and what is left is ADR 0198's measurement: two
   cleanups of one table at once, 60 rounds on disk and S3 on pylance 13.0.0, harmed no table. The model does not
   model the pass's effects on the store, so it does not prove that.

3. **Four assumptions the design rests on are configs that must fail, and do:**
   - A1, `WorkQueuePaused.cfg` (`OnePassPerTable`, a trace of 9 states): a process paused past its lease runs on; the
     ack wait runs out and the claim ages out under it, and another worker passes the table beside it.
   - A2, `WorkQueueNoClaim.cfg` (`OnePassPerTable`, 15 states, one fault): without the claim, an ack the network held
     past its worker's end is applied to the next worker's delivery, frees the subject under that pass, and a fired
     schedule starts a second pass of the table.
   - A3, `WorkQueueDueLimit.cfg` (`ACommitIsCovered`, 9 states, no fault): with a delivery limit on the fired schedules'
     consumer, a commit's follow-up is given up while a pass that read the table before the commit still runs.
   - A4, `WorkQueueNoAudit.cfg` (temporal, one fault): without the hourly audit, a table whose first announcement the
     lane gave up is held by nothing for good.

4. **Two findings, each kept as a config whose first line reads `FINDING` and `EXPECT VIOLATION`, each a CANDIDATE.**
   Neither is shown against the running service: that needs a real NATS JetStream with message schedules, which is the
   `nats-auth` e2e lane's and was not run here. Each costs a table at most the hour until the audit finds it. Neither
   loses data, and neither lets two passes overlap.
   - `WorkQueueLateSchedule.cfg` (`HeldUnlessBackstopped`, 10 states, no fault). A pass reads the table and holds its next
     time (`tasks.py` 63) at `next_due`, which counts from the pass's start (`table_pass.py` 837, 968) and is never
     sooner than a minute after it (`_SOONEST`, 85). A pass longer than that minute that finds the table due again holds
     a time already past, which fires at once (queue.py 20). `_move` finds the pass's own task still in flight, takes it
     as covered and acks the fired schedule (queue.py 1261-1264). The pass then acks (queue.py 959). The schedule is
     spent, the task is gone, and nothing holds the table until the audit. The claim in `run_due`'s docstring ("a table
     that already has a task queued or in flight is covered by it, since its pass computes the table's next time")
     does not hold when that pass has already computed it. The race is between the fire, its delivery and the move,
     and the pass's `schedule_first` and ack; which wins on a real bus was not measured.
   - `WorkQueueUndropFollowUp.cfg` (`HeldUnlessBackstopped`, 12 states, no fault). An undrop announces the table
     (`tables.py` 1598) and then clears its trash record (1599), and the lane holds a follow-up for the announcement
     `commitPassDelaySeconds` out, so its pass reads the table after the clear (arrival.py 11-13). If a follow-up from a
     commit before the drop is still held, the announcement holds none of its own (`TaskQueue.follow_up`, queue.py
     608-623). That older follow-up can fire between the announcement and the clear; its pass reads the table as
     trashed and holds no next time (`table_pass.py` 849-852). Once the record is cleared, nothing holds the live table
     until the audit (or, later, the trash task's restored-table pass at the record's deadline, `trash_task.py` 63-71).
     It needs the table's own schedule to have fired while the table was dropped, and the undrop's two writes to land
     around the older follow-up's fire time.

   `WorkQueueUndrop.cfg` holds the undrop under A6 as the comment states it: no follow-up fires between the announcement
   and the clear.

5. **The model was shown to bite.** Six defects were planted one at a time in a copy, and each was reported: a claim
   created over a standing one (`OnePassPerTable`, 14 states), a claim that ages out while it is renewed
   (`ThePassHoldsTheClaim`, 7), a pass that holds no next time (`HeldUnlessBackstopped`, 8), a fired follow-up acked as
   covered like a schedule (`ACommitIsCovered`, 9), a give-up on the last delivery that holds nothing
   (`HeldUnlessBackstopped`, 10), and a dead worker's claim that never ages out (the properties over a whole behaviour,
   on `WorkQueueLive.cfg`).

## What the model assumes

- **A1.** A worker whose lease runs out is gone, every thread of it, before JetStream hands its delivery on or its claim
  ages out. Held by `Worker._tell` and `_renew` (queue.py 1071-1112), which end the process before the lease runs out,
  `_end_process` (847-853, `_VOUCH_MARGIN` 141 and `_EXIT_RESERVE` 144), the claim's max age being the ack wait
  (`chart/templates/nats-stream-job.yaml` 169) counted from a renewal sent no earlier than the lease, and the render's
  refusal of a heartbeat past half the ack wait (166-167). It is a timing argument: a process paused past its lease
  (a stopped node, a frozen VM) breaks it, and nothing the store arbitrates stands behind it.
- **A2.** The claim exists, is created only where none stands, and is renewed and released only under its holder's token
  (queue.py 474-556, 993-1020).
- **A3.** The fired schedules' consumer redelivers for ever (`maintenance-due`, `max_deliver -1`,
  `chart/templates/_helpers.tpl` 415-416).
- **A4.** The hourly audit runs (`maintenance.queue.auditSchedule`, `chart/values.yaml` 786; asserted again every 15
  minutes, `tasks.py` 200-215).
- **A5.** The time a pass holds comes after it acks. Not held: finding `WorkQueueLateSchedule`.
- **A6.** No follow-up fires between an undrop's announcement and its clear. Held only for a follow-up the announcement
  itself held: finding `WorkQueueUndropFollowUp`.
- One message per subject on the task stream (`max_msgs_per_subject 1`, `discard_new_per_subject`,
  `nats-stream-job.yaml` 151) and the guarded publish (queue.py 134, 438-456), as measured in queue.py 127-132.
- The task consumer allows one delivery past `maxDeliver` (`_helpers.tpl` 411-413), and a task whose worker died on
  every one stays on the queue until the stream's 24 h max age (`nats-stream-job.yaml` 151).

## Not modelled

The pass's effects on the store (expiry, the due record, the committed files, the cadence stamp, the history fence),
which are one step here; the orphan check, which shares the table's claim, and the trash task, on subjects of their own;
the audit and the scan's shared claim; the store-event lane, a second source of follow-ups like the commit lane; a task
still deliverable that the stream's 24 h max age drops; the drain's hand-back; the bus session's renewal; and how long
any of it takes.

## The four facts

- **GitOps**: met. The streams, the consumers and the claims bucket, with every value A1 to A3 rest on, are the chart's
  stream Job's, and the worker creates none.
- **Cloud native**: met. Any number of replicas compete for the tasks, a replica that cannot vouch for its task ends and
  Kubernetes restarts it, and a dead replica's task goes to another.
- **Lance-namespace/table-idiomatic**: not touched. The pass's commits are the catalog's, through its doors (ADR 0204).
- **Zero trust**: partly met. The queue and the claim are written by one bus user, `maintenance-worker`, and the claim's
  token is no credential: mutual exclusion of two passes is a lease (A1), not a write the store refuses. No fencing token
  rides a pass's writes to the store, and the history fence admits two reclaims at once
  (`lakehouse.history_fences`), so a paused pass that resumes writes beside the next one. ADR 0198's measurement that
  two cleanups at once harm no table is what bounds that.

## Consequences

- The Rust worker keeps these steps in this order: the claim before the pass, the next time before the ack, the ack
  before the claim's release, every settle answered inside the lease or the process ended. A change to that order
  changes this model first.
- The two findings are the owner's to rule on. A fix for the first could count `next_due` from the pass's end, or nak a
  fired schedule while the table's task is in flight as a fired follow-up is; for the second, the undrop could hold
  its own follow-up whatever is held. Each config turns into a plain gate config once its fix lands in the model and the
  code.
- `make tla-check` runs this model's gate and expected failures in about 40 s on four cores.
