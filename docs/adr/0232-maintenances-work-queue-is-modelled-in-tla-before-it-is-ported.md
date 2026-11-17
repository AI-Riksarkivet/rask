# 0232. Maintenance's work queue and a table's pass are modelled in TLA+ before they are ported (2026-11-17)

**Status:** accepted; the fourth model of the port (ADR 0217 §5, ADR 0221). It describes the Python maintenance worker
as it runs today, before any of it is ported to Rust. Its two findings were fixed in the worker the same day (§4), and
so was a third, which an independent review found in the first fix (§4, `WorkQueueDueNak`).

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
   - the schedules (`Worker.run_due`, `_move`, `_hold_again`): a fired schedule or follow-up delivered by the fired
     schedules' consumer (`maintenance-due`, a delivery out holding one of its `DueCeiling` slots until it is acked) and
     moved onto the queue once no task holds the table; one that meets a task is held again a minute out on its own
     schedule subject by a guarded publish, then acked, the two as two steps a mover may die between; a move the bus
     refuses is naked to come again; a time a pass held fires whenever it comes, while that pass still runs included;
   - the commit lane (`arrival.Arrivals.hold_task`): an announcement holds the table's follow-up unless one is held; an
     undrop's announcement, a registration, holds its own, replacing the one held;
   - the hourly audit (`audit._queue_what_holds_nothing`), its cadence's firing delivered on a free slot of the same
     consumer: it queues a live table that neither a schedule, held or fired, nor the queue holds (`TaskQueue.held`);
   - a drop, and an undrop that announces the table before it clears the trash record
     (`api/v1/endpoints/tables.undrop_table`, `filing.send(restored)` at 1593 before `trash.clear` at 1595).

   Faults are actions counted against a bound: a worker that dies between any two effects (a crash, a hung pass, a
   drain outrun, a heartbeat or renewal the bus stopped answering), a settle sent and not answered and held by the
   network, a pass that raises, a next time the bus refuses, an announcement the lane gives up, a move the bus refuses,
   and a mover that dies between its two effects.

2. **What it proves** (`make tla-check`; times measured 2026-11-17 on two TLC workers, four cores shared at a load of
   6 to 8). Every config holds the fired schedules' consumer to one delivery out for the table (`DueCeiling` 1), and
   every gate config checks `AWaitingFiringHoldsNoSlot` with the other invariants:
   - `WorkQueue.cfg` (two workers counted as interchangeable, two faults, two deliveries a task, a commit): 212,664
     states, every invariant below, 7 s.
   - `WorkQueueLive.cfg` (the same with one fault, the workers distinct): 81,845 states, the invariants and the three
     properties over a whole behaviour, 43 s.
   - `WorkQueueUndrop.cfg` (a drop and its undrop, one fault, symmetry): 237,335 states, the invariants, 6 s.
   - `WorkQueueUndropLive.cfg` (a drop and its undrop, no fault, a commit, the workers distinct): 22,762 states, the
     invariants and the three properties: the three guards of §4 together over a whole behaviour, 14 s.
   - `WorkQueueLiveWide.cfg`, ON DEMAND (the three guards together with a fault: one fault, a commit, a drop and its
     undrop, the workers distinct): 462,309 states, the invariants and the three properties, 12 min at a load of 12.
   - `WorkQueueWide.cfg`, ON DEMAND (three workers counted as interchangeable, three faults, a commit, a drop and its
     undrop): 6,352,577 states, the invariants, 6.5 min at a load of 12.

   The gate's maintenance configs, the nine that must fail included, run in about 70 s at that load.

   The invariants, each a claim the code makes:
   - `OnePassPerTable`: two passes of one table never run at once, a bus cut, a late settle and a restart included
     (`Worker`'s docstring, queue.py 832-833).
   - `ThePassHoldsTheClaim`: a running pass holds its table's claim (`_claim`, `_renew`).
   - `HeldUnlessBackstopped`: something on the bus will pass a live table (its task, a schedule held or fired, a
     follow-up, an announcement still to be read) unless a fault or a give-up happened that the code leaves to the audit
     (`run_task`'s docstring; `_end`).
   - `ACommitIsCovered`: after a commit, a pass that reads the table after it is on its way: the announcement, its
     follow-up, a give-back's minute, or a task not yet read (`queue.py` module docstring; `run_due`).
   - `AWaitingFiringHoldsNoSlot`: a fired time that waits for its table's task holds none of the fired schedules'
     consumer's deliveries (`Worker._move`). It is checked for one table; every other table's share is the same, so it
     holds however many tables wait, and the consumer keeps its whole ceiling for the firings it moves.

   The properties, once faults stop:
   - `TheTableStaysHeld`: no live table is forgotten; from some point on something always holds it.
   - `EveryCommitIsRead`: every commit is read by a later pass, unless the table goes.
   - `NothingDeadBlocks`: a pass that died leaves nothing that blocks the next for ever; its claim ages out and its
     delivery is handed on.

   Two passes of one table cannot both commit, because they never run at once (`OnePassPerTable`) under A1. Where A1
   fails, the two run beside each other (`WorkQueuePaused.cfg`), and what is left is ADR 0198's measurement: two
   cleanups of one table at once, 60 rounds on disk and S3 on pylance 13.0.0, harmed no table. The model does not
   model the pass's effects on the store, so it does not prove that.

3. **Nine configs remove an assumption or a guard the design rests on, and must fail, and do** (each first line
   `EXPECT VIOLATION`):
   - A1, `WorkQueuePaused.cfg` (`OnePassPerTable`, a trace of 10 states): a process paused past its lease runs on; the
     ack wait runs out and the claim ages out under it, and another worker passes the table beside it.
   - A2, `WorkQueueNoClaim.cfg` (`OnePassPerTable`, 16 states, one fault): without the claim, an ack the network held
     past its worker's end is applied to the next worker's delivery, frees the subject under that pass, and a fired
     schedule starts a second pass of the table.
   - A3, `WorkQueueDueLimit.cfg` (`ACommitIsCovered`, 5 states, one fault): with a delivery limit on the fired
     schedules' consumer, a commit's follow-up whose move the bus refused is given up while a pass that read the table
     before the commit still runs. Since A7 a waiting fired time is acked on its first delivery, so only a refused move
     is delivered more than once, and the config needs that fault.
   - A4, `WorkQueueNoAudit.cfg` (temporal, 2,544 states, one fault): without the hourly audit, a table whose first
     announcement the lane gave up is held by nothing for good.
   - A5, `WorkQueueLateSchedule.cfg` (`HeldUnlessBackstopped`, 10 states, no fault): the first finding's guard removed (§4).
   - A6, `WorkQueueUndropFollowUp.cfg` (`HeldUnlessBackstopped`, 12 states, no fault): the second finding's guard
     removed (§4).
   - A7, `WorkQueueDueNak.cfg` (`AWaitingFiringHoldsNoSlot`, 5 states, no fault): the third finding's design, the nak,
     put back (§4).
   - A7's order, `WorkQueueAckFirst.cfg` (`HeldUnlessBackstopped`, 11 states, one fault): the fired time acked before it
     is held again. The table's pass holds its next time already past, it fires, the mover finds the pass's task and
     acks, the pass acks, and the mover dies before its re-hold: nothing holds the table, and a death is no fault the
     code leaves to the audit.
   - A7's follow-up guard, `WorkQueueReholdFollowUpUnguarded.cfg` (`HeldUnlessBackstopped`, 20 states, no fault): a
     waiting follow-up held again by replacing what is held (`follow_up(replacing=True)`). A write's follow-up fires
     while a pass runs, the table is dropped and undropped, the undrop's announcement holds its own follow-up, and the
     waiting one's minute replaces it, fires before the clear, and its pass reads the table in the trash and holds no
     next time.

   A7's schedule guard (`schedule_first`, a re-hold refused while a schedule is held) has no such config: with it
   removed every gate config still holds (212,664 / 239,519 / 84,967 states for the gate's safety, undrop and liveness
   settings, ad hoc, 2026-11-17), since the model's properties ask only that something holds the table. It stays for
   what the model leaves out: replacing would move the pass's own later time a minute in, a pass the table does not
   need, and roll an orphan check's interval, which rides the schedule's body, back to the fired one's.

4. **Three findings, each CONFIRMED against the running worker and FIXED on 2026-11-17.** Each was shown by
   `tests/integration/test_a_live_table_stays_held_on_the_bus.py`: the real worker, task runner and commit lane on the
   pinned nats-server 2.15.2 (`scripts/nats_server.sh`), its streams, consumers and claims bucket declared as the chart's
   stream Job declares them, one step held at the moment the trace names, and the harm read as the audit reads it
   (`TaskQueue.held` over the table's schedules and its task), or, for the third, by what reaches the queue. Each test
   failed on its harm assertion before its fix (five runs of five for the first two, three of three for the third),
   and passes after it; with the fix reverted it fails again. No finding lost data or let two passes overlap: the
   first two left the table to the hourly audit (`auditSchedule` `@hourly`, `chart/values.yaml` 786), its next pass up
   to 60 minutes late, 30 on average; the third stopped the audit itself while it lasted.
   - **`WorkQueueLateSchedule`, FIXED: a fired schedule waits for the task that holds its table** (A5). The trace: the
     worker pulls the task and claims the table. The pass reads the table and counts its next time from its start
     (`table_pass.run_table_pass`): `now + _SOONEST` (60 s) for a dataset due during the pass, or `now + retry_seconds`
     (900 s) for one it could not decide. A pass longer than that holds a time already past (`tasks.run_task`,
     `TaskQueue.schedule_at`), which fires at once: measured on the pinned server, a past `@at` fired 248-261 ms after
     its publish was answered, 20 of 20. The move finds the pass's own task in flight, the guarded publish is refused,
     and the fired schedule was acked as covered; the pass then acked and released its claim, and nothing held the table.
     The harm needed the pass's ack answered more than about 250 ms after its next time was held, as on a bus that
     answers slowly; unheld, the ack won 30 of 30 on one local server.

     The guard: `Worker._move` (`services/maintenance/src/maintenance/services/queue.py` 1293-1313) never acks a fired
     schedule the queue refuses while a task holds its table as covered, for every kind a task of which holds its own
     next time (`_SCHEDULED_KINDS`: table, trash record, orphan check), as it already did a fired follow-up: it holds the
     time again `_HELD_TABLE_RETRY` (60 s, queue.py 138) out (the third finding's design, below). A cadence's firing (the
     audit's, the scan's) is still acked as covered: the cadence holds its next firing itself. Proof the guard is needed:
     `WorkQueueLateSchedule.cfg`. Its test: `test_a_pass_that_holds_a_time_already_past_leaves_its_table_held`, which
     holds the pass's ack until the move has settled the fired schedule, reads the table held once the pass has acked,
     then follows the payoff: the time held again fires after the task is gone, the table's task is queued, a second
     pass runs and holds its next time (the wait held 2 s out in the test only to bring that fire within it). With the
     guard removed it fails three runs of three on its first harm assertion. Its cost: one more pass of a table whose
     schedule fires while another task passes it, up to a minute after that task ends.
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
     `Arrivals._held`, 196), published without the guard (`TaskQueue.follow_up(..., replacing=True)`, queue.py 635-637),
     and answers `held`. Measured on the pinned server, a held follow-up replaced this way fired once, at the newer time,
     with the newer body. The undrop's own follow-up waits out `commitPassDelaySeconds` (300 s), which outlasts the
     door's two writes; the catalog's undrop door keeps its order (announce, then clear), whose outbox files the
     announcement before the clear on purpose ([[LH-576]]). Proof the guard is needed: `WorkQueueUndropFollowUp.cfg`.
     Its test: `test_an_undropped_table_is_held_when_an_older_follow_up_fires_before_the_clear`, which holds the clear
     until the write's follow-up's time has passed and the bus is quiet. Its cost: the older follow-up's pass comes up
     to `commitPassDelaySeconds` later; it still reads every commit it covered.
   - **`WorkQueueDueNak`, FIXED: a fired time that waits for its table's task holds no delivery of the fired schedules'
     consumer** (A7). Found by an independent review of the first fix as it shipped, which naked a waiting fired
     schedule or follow-up `_HELD_TABLE_RETRY` out. A delivery naked with a delay stays ack-pending until it is
     delivered again, and each nak puts it back, so a waiting table held one of `maintenance-due`'s `max_ack_pending`
     (1000, `chart/templates/_helpers.tpl` 417) deliveries for as long as its task lived, bounded only by that task's
     deliveries and back-off or the tasks stream's 24 h max age. Once 1000 tables waited, JetStream handed out no new
     delivery on `maintenance.due.>`: no other table's schedule, no trash release, no orphan check, and not the audit's
     or the scan's cadence, so the backstop A4 names stopped too; redeliveries are served ahead of pending messages, so
     the stall held while the 1000 kept waiting. Its trace needs more tables with a task queued or in flight when their
     schedule fires than the ceiling: a burst of commits over many tables that outruns the workers, or a fleet not
     ready (`run_due` is not gated on readiness, `run_tasks` is). Measured on the pinned server (2026-11-17): after
     five deliveries naked 20 s out on a consumer of `max_ack_pending` 5, `num_ack_pending 5, num_pending 0`; another
     table's fired schedule and `maintenance.due.audit` were published and not delivered in 3 s; after the delay the
     five came back first, and the two stayed pending. The ADR's own text stated the cost as "one refused publish a
     minute", which was wrong.

     The guard: `Worker._move` holds a waiting fired time again `_HELD_TABLE_RETRY` out on the subject it fired from,
     then acks it (`Worker._hold_again`, queue.py 1315): a fired schedule by `TaskQueue.schedule_first`, a fired
     follow-up by `TaskQueue.follow_up` without `replacing`, each a publish JetStream refuses while a time is held there
     (codes 10071 and 10164, queue.py 139-145), which then holds the table already. The order is the guard's: a mover
     that dies after the re-hold leaves the table held, and its delivery, handed on after the consumer's ack wait (30 s),
     is moved again, its re-hold refused by the one standing, so a duplicate costs one refused publish; a mover that
     acked first and died would leave nothing (`WorkQueueAckFirst.cfg`). A follow-up's re-hold must not replace: the
     follow-up held may be an undrop's own, and the minute would fire before its clear
     (`WorkQueueReholdFollowUpUnguarded.cfg`). A move the bus refuses, the enqueue or the re-hold, is naked with a
     growing delay as before (`record_schedule_not_queued`), which holds a delivery only while the bus refuses. What
     the bus sees for a waiting table: one guarded publish on its schedule subject, one delivery and one ack a minute,
     for as long as its task lives, and no delivery of `maintenance-due` held between them. Proof the guard is needed:
     `WorkQueueDueNak.cfg`. Its test: `test_tables_waiting_for_their_tasks_leave_the_fired_schedules_consumer_delivering`,
     which declares `maintenance-due` with `max_ack_pending` 3, runs a worker that is not ready (its tasks stay queued)
     and has three tables' schedules fire onto their queued tasks, then another table's schedule and the audit's
     cadence: both must reach the queue, and the three stay held. With the nak put back it fails three runs of three
     (`num_pending=2 num_ack_pending=3`). No test kills a mover between its two effects or replaces an undrop's
     follow-up with a waiting one's minute: the order and the follow-up guard rest on their configs alone.

     `run_due` stays ungated on readiness: a move runs no pass and emits nothing, and with A7 a fleet that is not ready
     holds no delivery while its tables wait, so gating it would only stop the audit's cadence and every trash release
     from reaching the queue too.

   **The four variants checked before the fix.** Each was a constant of the model, checked under its finding's setting
   with one fault, a commit and the workers distinct, every invariant and the three properties: `fromEnd` (the next
   time counted from the pass's end) held, 64,837 states, but only while the ack is answered inside `_SOONEST`, and the
   chart sets no ceiling on `ackWaitSeconds`; `nak` held, 64,669 states; `afterClear` (the undrop announcing after its
   clear) held, 268,484 states, but changes the catalog's undrop door, whose outbox files the announcement before the
   clear on purpose; `own` held, 335,996 states. `nak` and `own` were chosen: maintenance's own change, resting on a
   JetStream behaviour measured on the pinned server rather than on a timing argument. The rejected two and their
   configs were removed from the model with the fix. The model then had no consumer ceiling, so `nak` holding said
   nothing about the slots it held; the third finding added `DueCeiling` and `AWaitingFiringHoldsNoSlot`, under which
   `nak` fails (`WorkQueueDueNak.cfg`, 5 states), and the re-hold replaced it. Raising `max_ack_pending` is no
   alternative: the per-table invariant fails at any ceiling, and a larger one only needs more waiting tables. Ending a
   waiting fired time (acked as covered, or termed) leaves the table to the audit, which is A5's finding again.

   **The audit's `due` read stays.** `TaskQueue.held("schedule", kind)` reads `maintenance.due.<kind>.*` beside the
   schedule subjects (queue.py 664). With nothing waiting there, the model holds without it too (280,677 / 320,087 /
   117,415 / 37,983 states for the gate's safety, undrop, liveness and undrop-liveness settings, ad hoc), since an
   audit that queues a table a fired time is about to queue costs only one more pass. It stays because what it says is
   still true: a fired time not yet delivered, or delivered to a worker that died before its move was acked, can be the
   only thing holding its table, and read as held by nothing the table would be queued by the audit and passed once
   more a minute after, when that fired time meets the audit's task.

5. **The model was shown to bite** (before the fix). Six defects were planted one at a time in a copy, and each was
   reported: a claim created over a standing one (`OnePassPerTable`, 14 states), a claim that ages out while it is
   renewed (`ThePassHoldsTheClaim`, 7), a pass that holds no next time (`HeldUnlessBackstopped`, 8), a fired follow-up
   acked as covered like a schedule (`ACommitIsCovered`, 9), a give-up on the last delivery that holds nothing
   (`HeldUnlessBackstopped`, 10), and a dead worker's claim that never ages out (the properties over a whole behaviour,
   on `WorkQueueLive.cfg`). The three findings' configs, and A7's order and follow-up guard's, are the same check for
  the fixes' guards.

## What the model assumes

- **A1.** A worker whose lease runs out is gone, every thread of it, before JetStream hands its delivery on or its claim
  ages out. Held by `Worker._tell` and `_renew` (queue.py 1106-1147), which end the process before the lease runs out,
  `_end_process` (882-888, `_VOUCH_MARGIN` 153 and `_EXIT_RESERVE` 156), the claim's max age being the ack wait
  (`chart/templates/nats-stream-job.yaml` 169) counted from a renewal sent no earlier than the lease, and the render's
  refusal of a heartbeat past half the ack wait (166-167). It is a timing argument: a process paused past its lease
  (a stopped node, a frozen VM) breaks it, and nothing the store arbitrates stands behind it.
- **A2.** The claim exists, is created only where none stands, and is renewed and released only under its holder's token
  (queue.py 486-593, 1028-1055).
- **A3.** The fired schedules' consumer redelivers for ever (`maintenance-due`, `max_deliver -1`,
  `chart/templates/_helpers.tpl` 416), so a fired follow-up or schedule whose move the bus refused is moved once the
  bus takes it.
- **A4.** The hourly audit runs (`maintenance.queue.auditSchedule`, `chart/values.yaml` 786; asserted again every 15
  minutes, `tasks.py` 200-215).
- **A5.** A fired schedule that finds a task holding its table waits for it, never acked as covered by it
  (`Worker._move`). A pass counts its next time from its start, so the time it holds may fire before it acks; nothing
  bounds that by timing.
- **A7.** A fired time that waits for its table's task holds no delivery of the fired schedules' consumer: it is held
  again on its schedule subject, by a publish refused where a time is held, before it is acked (`Worker._move`,
  `_hold_again`). The consumer's ceiling is a stated assumption of the design, not of this table: `maintenance-due`
  has `max_ack_pending` 1000 (`chart/templates/_helpers.tpl` 417, rendered by the chart's stream Job), the model checks
  one table at `DueCeiling` 1, and with no table's waiting time holding a slot, the ceiling bounds only the deliveries
  being moved at once (each for the milliseconds of its move, or for the consumer's ack wait, 30 s, after its worker
  died). `MaintenanceDueStuck` (`deploy/alerting/rules.yml`) warns when that count stays at 900 or more.
- **A6.** An undrop's announcement holds a follow-up of its own, replacing an older one (`arrival.lane_task`,
  `TaskQueue.follow_up`), and that one fires only after the undrop's clear: its delay, `commitPassDelaySeconds`
  (300 s), outlasts the door's two writes. The second half is a timing argument; an older follow-up may fire between
  the two, which the first half makes harmless.
- One message per subject on the task stream (`max_msgs_per_subject 1`, `discard_new_per_subject`,
  `nats-stream-job.yaml` 151) and the guarded publish (queue.py 146, 450-468), as measured in queue.py 139-144.
- The task consumer allows one delivery past `maxDeliver` (`_helpers.tpl` 411-413), and a task whose worker died on
  every one stays on the queue until the stream's 24 h max age (`nats-stream-job.yaml` 151).

## Not modelled

The pass's effects on the store (expiry, the due record, the committed files, the cadence stamp, the history fence),
which are one step here; the orphan check, which shares the table's claim, and the trash task, on subjects of their own
(both now wait as a table's fired schedule does); the audit and the scan's shared claim and their cadences; the
store-event lane, a second source of follow-ups like the commit lane; a registration that is no undrop (it replaces a
held follow-up the same way); a task still deliverable that the stream's 24 h max age drops; the drain's hand-back; the
bus session's renewal; the due consumer's ack wait on a delivery whose mover died (handed on at once here); every other
table, whose share of the consumer's ceiling is the same as this one's; and how long any of it takes.

## The four facts

- **GitOps**: met, by the three fixes. The streams, the consumers and the claims bucket, with every value A1 to A7
  rest on (`maintenance-due`'s `max_ack_pending` among them), are the chart's stream Job's, and the worker creates none;
  the alert on that consumer's ceiling is in `deploy/alerting/rules.yml` with its promtool case. No fix adds a chart
  value: the re-hold's minute (`_HELD_TABLE_RETRY`) is no operator's to tune. It is the soonest a pass holds its
  table's next time (`table_pass._SOONEST`) and the give-back's minute; a waiting table costs one publish, one delivery
  and one ack a minute and no consumer slot, which a fleet of any size bears, so no value of the estate trades it
  against anything, and a shorter one would pass a table again sooner than its own pass would ask.
  `commitPassDelaySeconds` is the chart's.
- **Cloud native**: met, by the three fixes. Any number of replicas compete for the tasks and the fired schedules, a
  replica that cannot vouch for its task ends and Kubernetes restarts it, and a dead replica's task goes to another; a
  waiting fired time is a schedule on the bus, not state in a replica, and holds no delivery of a shared consumer.
- **Lance-namespace/table-idiomatic**: not applicable to the third fix, which touches only the bus; met and untouched
  by all three. The pass's commits are the catalog's, through its doors (ADR 0204), and the catalog's undrop door keeps
  its order.
- **Zero trust**: partly met, unchanged by the three fixes. The re-hold publishes on `maintenance.schedule.<kind>.<uuid>`
  and `maintenance.schedule.followup.table.<uuid>` and acks on `$JS.ACK.MAINTENANCE_SCHEDULES.maintenance-due.>`, both
  already granted to the bus user `maintenance-worker` (`chart/values.yaml` 1454 and 1458, `maintenance.schedule.>` and
  the ack subject), which every schedule the worker holds already uses; no grant is added. The queue and the claim are written by one bus user,
  `maintenance-worker`, and the claim's token is no credential: mutual exclusion of two passes is a lease (A1), not a
  write the store refuses. No fencing token rides a pass's writes to the store, and the history fence admits two
  reclaims at once (`lakehouse.history_fences`), so a paused pass that resumes writes beside the next one. ADR 0198's
  measurement that two cleanups at once harm no table is what bounds that.

## Consequences

- The Rust worker keeps these steps in this order: the claim before the pass, the next time before the ack, the ack
  before the claim's release, every settle answered inside the lease or the process ended, a fired schedule that meets
  its table's task held again before it is acked and never acked as covered, a follow-up held again without replacing
  one, and a registration's follow-up replacing the one held. A change to that order changes this model first.
- What an operator sees: `maintenance.arrivals.settled` counts an undrop's or a registration's announcement as `held`
  where it counted `covered`; `maintenance.schedules.reheld` counts each fired time that met its table's task, by
  `kind`, `fired` (`schedule`, `follow_up`) and `outcome` (`held`, or `standing` where a time held there already holds
  the table): one a minute for each table that waits; the `maintenance-due` consumer's ack-pending gauge
  (`maintenance.queue.ack_pending`, consumer `maintenance-due`) reads only the moves in course, however many tables
  wait, and `MaintenanceDueStuck` warns when it stays at 900 or more for 15 minutes; a waiting table shows on its
  `schedule` subject, not its `due` one; and a table whose schedule fired during a pass is passed once more after it.
- `make tla-check` runs this model's gate and expected failures in about 70 s on four cores at a load of 6.
