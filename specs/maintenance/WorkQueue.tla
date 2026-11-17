--------------------------- MODULE WorkQueue ---------------------------
(***************************************************************************)
(* Maintenance's work queue and one table's pass (ADR 0198 §2, its         *)
(* amendments of 2026-11-09).                                              *)
(*                                                                         *)
(* One table, its task subject maintenance.task.table.<uuid> and the       *)
(* workers that pull it (maintenance.services.queue):                      *)
(*   - the queue holds one message per subject; a publish expecting the    *)
(*     subject empty is refused while a task is queued or in flight        *)
(*     (TaskQueue.enqueue, the stream's discard_new_per_subject);          *)
(*   - a delivery is a worker's while its heartbeats are answered; one     *)
(*     the bus stops answering ends its process before the ack wait the    *)
(*     last answer restarted runs out (Worker._tell, _end_process);        *)
(*   - a pass first claims its table in MAINTENANCE_CLAIMS, created only   *)
(*     where none stands and aged out one ack wait after its last renewal  *)
(*     (Worker._claim, TaskQueue.claim/renew/release), and gives the task  *)
(*     back while another's claim stands (Worker._give_back);              *)
(*   - the pass reads the table, holds its next time (run_task's           *)
(*     schedule_at), and acks; the claim goes last (Worker._handle);       *)
(*   - a settle the network held past its sender's end is applied late,    *)
(*     by sequence, to whatever delivery of that message is out then.      *)
(* Around it: a commit's announcement holds the table's follow-up unless   *)
(* one is held, and a registration (an undrop's announcement among them)   *)
(* holds its own, replacing any held (Arrivals.hold_task,                  *)
(* TaskQueue.follow_up); a fired schedule or follow-up is delivered by the *)
(* fired schedules' consumer (maintenance-due) and moved onto the queue    *)
(* once no task holds the table; one that meets a task holding it is held  *)
(* again a minute out on its own schedule subject by a guarded publish,    *)
(* then acked (Worker._move); the hourly audit, whose cadence fires on     *)
(* that consumer too, queues a table that neither a schedule, held or      *)
(* fired, nor the queue holds (audit._queue_what_holds_nothing); a drop,   *)
(* and an undrop that announces the table before it clears its trash      *)
(* record (arrival.py's docstring).                                        *)
(*                                                                         *)
(* A message's sequence is not a number here: a worker's wcur says whether *)
(* the delivery it handles is of the message the stream holds now, and a   *)
(* claim's ccur whether its token is its holder's current handling's.      *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. EndsInTime: a worker whose lease runs out is gone, every thread   *)
(*       of it, before JetStream hands its delivery on or its claim ages   *)
(*       out (Worker._end_process flushes until _EXIT_RESERVE before the   *)
(*       lease and os._exit's; the claim's max age is the ack wait, from   *)
(*       a renewal sent no earlier than the lease counts). A process       *)
(*       paused past its lease breaks it: WorkQueuePaused.cfg.             *)
(*   A2. Claims: the claim exists. Without it a late settle starts a       *)
(*       second pass beside the first: WorkQueueNoClaim.cfg.               *)
(*   A3. DueUnlimited: the consumer of fired schedules redelivers for      *)
(*       ever (maintenance-due, max_deliver -1), so a fired follow-up or   *)
(*       schedule whose move the bus refused is moved once it takes one:   *)
(*       WorkQueueDueLimit.cfg.                                            *)
(*   A4. Audits: the hourly audit runs. It is what finds a table whose     *)
(*       schedule a failure lost: WorkQueueNoAudit.cfg.                    *)
(*   A5. FiredWaits: a fired schedule that finds a task holding its        *)
(*       table comes again, never acked as covered by it (Worker._move).   *)
(*       A pass counts its next time from its start, so one longer than    *)
(*       that time holds one already past, which fires while the pass      *)
(*       runs; acked as covered, it leaves the table held by nothing once  *)
(*       the pass acks: WorkQueueLateSchedule.cfg.                         *)
(*   A6. UndropHoldsOwn: an undrop's announcement (a registration) holds   *)
(*       a follow-up of its own, replacing an older one, and that one      *)
(*       fires only after the undrop's clear of the trash record, its      *)
(*       delay (commitPassDelaySeconds) outlasting the door's two writes.  *)
(*       An older follow-up the announcement found held may fire between   *)
(*       the two; covered by it, the announcement leaves the table to a    *)
(*       pass that read it in the trash: WorkQueueUndropFollowUp.cfg.      *)
(*   A7. WaitByRehold: a fired time that waits for its table's task holds  *)
(*       no delivery of the fired schedules' consumer while it waits: it   *)
(*       is held again a minute out on its own schedule subject, then      *)
(*       acked. Naked to come again instead, it stays ack-pending until    *)
(*       it is delivered again, and DueCeiling of them (max_ack_pending)   *)
(*       stop every other delivery on that consumer, the audit's cadence   *)
(*       among them: WorkQueueDueNak.cfg. Its order and its guards:        *)
(*       ReholdFirst, the re-hold before the ack, or a mover that dies     *)
(*       between the two leaves the table held by nothing                  *)
(*       (WorkQueueAckFirst.cfg); FollowUpReholdGuarded, a follow-up held  *)
(*       again only where none is held, or the minute replaces an undrop's *)
(*       own follow-up and fires before its clear                          *)
(*       (WorkQueueReholdFollowUpUnguarded.cfg); ScheduleReholdGuarded, a  *)
(*       schedule held again only where none is held, which keeps the      *)
(*       time and body a pass held (an orphan check's interval rides its   *)
(*       body): no property here rests on it, and with it removed every    *)
(*       gate config still holds, so it has no config of its own.          *)
(*                                                                         *)
(* NOT MODELLED: the pass's own effects on the store (they are one Read    *)
(* step here), the orphan check and the trash task (other subjects; the    *)
(* orphan check shares the table's claim), the audit and the scan's        *)
(* shared claim, the store-event lane (a second source of follow-ups like  *)
(* the commit lane), the stream's 24 h max age on a task still             *)
(* deliverable, the drain and its hand-back, the bus session's renewal,    *)
(* the due consumer's ack wait on a mover that died (its delivery is       *)
(* handed on at once here), and every other table, whose share of the      *)
(* consumer's ceiling is the same as this one's.                           *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Workers,
          NoOne,                 \* no worker
          MaxFaults,             \* how many faults one behaviour may hold
          MaxDeliver,            \* maintenance.queue.maxDeliver; the consumer allows one delivery more
          MaxCommits,            \* how many commits land on the table
          Drops,                 \* how many drops (each may be undropped)
          EndsInTime,            \* A1
          Claims,                \* A2
          DueUnlimited,          \* A3
          Audits,                \* A4
          FiredWaits,            \* A5
          UndropHoldsOwn,        \* A6
          WaitByRehold,          \* A7: TRUE holds a waiting fired time again; FALSE naks it
          ReholdFirst,           \* A7's order: the re-hold before the ack
          ScheduleReholdGuarded, \* A7: a schedule held again only where none is held (schedule_first)
          FollowUpReholdGuarded, \* A7: a follow-up held again only where none is held (follow_up)
          DueCeiling             \* the fired schedules' consumer's deliveries awaiting an ack (max_ack_pending)

VARIABLES table,      \* "live", "dropped", or "undropping" (announced, its trash record not yet cleared)
          owed,       \* a commit landed that no pass has read the table after
          hint,       \* an announcement waiting for the commit lane
          followup,   \* the follow-up held: "none", "early", or "late" (held after an undrop's announcement)
          dueFollow,  \* a fired follow-up on its due subject
          sched,      \* the table's schedule is held
          soon,       \* ... and it was held by a give-back, a minute out, for a task no pass ran
          dueSched,   \* a fired schedule on its due subject
          dueSoon,    \* ... and it was a give-back's
          schedOut,   \* the fired schedule's delivery on the due consumer: "none" (none out), "reheld" (its mover held
                      \* the time again and has not acked), "acked" (acked, the time not yet held again), "waiting"
                      \* (naked to wait for the task), "retrying" (naked after the bus refused its move)
          followOut,  \* the same for the fired follow-up
          mst,        \* the task message: "none", "avail" (queued), "out" (delivered)
          mholder,    \* the worker it is out to
          mn,         \* how many times it has been delivered
          ws,         \* each worker's step
          wcur,       \* whether a worker's delivery is of the message the stream holds now
          wn,         \* the delivery count a worker's handling started on
          lapsed,     \* a worker's lease ran out while its process runs on (only without A1)
          cwho,       \* the claim's holder, or NoOne
          ccur,       \* whether the claim's token is its holder's current handling's
          net,        \* settles the network holds for the message the stream holds now
          faults,
          backstop,   \* a fault or a give-up happened that the code leaves to the audit or the recheck
          commits,
          drops

vars == <<table, owed, hint, followup, dueFollow, sched, soon, dueSched, dueSoon, schedOut, followOut, mst, mholder, mn,
          ws, wcur, wn, lapsed, cwho, ccur, net, faults, backstop, commits, drops>>

moves == <<schedOut, followOut>>
bus == <<followup, dueFollow, sched, soon, dueSched, dueSoon, moves, mst, mholder, mn, net>>
workers == <<ws, wcur, wn, lapsed>>
claim == <<cwho, ccur>>
env == <<table, owed, hint, commits, drops>>

Handling == {"got", "claimed", "scheduled", "done", "failed", "ending"}
InPass == {"claimed", "scheduled", "done", "failed"}

Init ==
  /\ table = "live" /\ owed = TRUE /\ hint = TRUE     \* the table's first write, announced
  /\ followup = "none" /\ dueFollow = FALSE
  /\ sched = FALSE /\ soon = FALSE /\ dueSched = FALSE /\ dueSoon = FALSE
  /\ schedOut = "none" /\ followOut = "none"
  /\ mst = "none" /\ mholder = NoOne /\ mn = 0
  /\ ws = [w \in Workers |-> "idle"] /\ wcur = [w \in Workers |-> FALSE] /\ wn = [w \in Workers |-> 0]
  /\ lapsed = [w \in Workers |-> FALSE]
  /\ cwho = NoOne /\ ccur = FALSE
  /\ net = {}
  /\ faults = 0 /\ backstop = FALSE /\ commits = 0 /\ drops = 0

Fault == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults

---------------------------------------------------------------------------
\* The message leaving the stream: no delivery a worker holds is of the one the stream holds next, and no settle the
\* network holds matches its sequence.

Gone ==
  /\ mst' = "none" /\ mholder' = NoOne /\ mn' = 0
  /\ wcur' = [w \in Workers |-> FALSE] /\ net' = {}

\* A guarded publish (TaskQueue.enqueue): taken only while the subject holds nothing.
Enqueued ==
  /\ mst = "none"
  /\ mst' = "avail" /\ mholder' = NoOne /\ mn' = 0
  /\ UNCHANGED <<wcur, net>>

\* A worker's ack or term, applied by sequence: it removes the message only if it is still the one the worker was handed.
Removes(w) == IF wcur[w] /\ mst # "none" THEN Gone ELSE UNCHANGED <<mst, mholder, mn, wcur, net>>

\* A worker's nak: the message is queued again if it is still the one the worker was handed and is out. The worker
\* holds no delivery of it after.
Requeues(w) ==
  /\ IF wcur[w] /\ mst = "out"
       THEN mst' = "avail" /\ mholder' = NoOne
       ELSE UNCHANGED <<mst, mholder>>
  /\ wcur' = [wcur EXCEPT ![w] = FALSE]
  /\ UNCHANGED <<mn, net>>

\* Whether the claim's token is w's current handling's.
Holds(w) == cwho = w /\ ccur

---------------------------------------------------------------------------
\* A worker's handling of one delivery (Worker.run_tasks, _handle).

Pull(w) ==
  /\ ws[w] = "idle" /\ mst = "avail" /\ mn < MaxDeliver + 1 /\ NoFault
  /\ mst' = "out" /\ mholder' = w /\ mn' = mn + 1
  /\ wcur' = [wcur EXCEPT ![w] = TRUE]
  /\ wn' = [wn EXCEPT ![w] = mn + 1]
  \* Past max_deliver the task is ended unrun (_handle's first branch).
  /\ ws' = [ws EXCEPT ![w] = IF mn + 1 > MaxDeliver THEN "ending" ELSE "got"]
  \* A new handling mints a new token: a claim this worker left from an earlier one is another's now.
  /\ ccur' = IF cwho = w THEN FALSE ELSE ccur
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, moves, net, lapsed, cwho, backstop>>

\* Ended without running (_end): a term, then the next try held recheckHours out.
EndUnrun(w) ==
  /\ ws[w] = "ending" /\ NoFault
  /\ Removes(w)
  /\ sched' = TRUE /\ soon' = FALSE
  /\ backstop' = TRUE
  /\ ws' = [ws EXCEPT ![w] = "idle"]
  /\ UNCHANGED <<env, followup, dueFollow, dueSched, dueSoon, moves, wn, lapsed, claim>>

\* The claim is created only where none stands (KeyValue.create). Without A2 there is nothing to wait for.
Claim(w) ==
  /\ ws[w] = "got" /\ NoFault
  /\ IF Claims THEN cwho = NoOne /\ cwho' = w /\ ccur' = TRUE ELSE UNCHANGED claim
  /\ ws' = [ws EXCEPT ![w] = "claimed"]
  /\ UNCHANGED <<env, bus, wcur, wn, lapsed, backstop>>

\* A worker leaves a handling: nothing of it is kept.
Leaves(w, to) ==
  /\ ws' = [ws EXCEPT ![w] = to]
  /\ wn' = [wn EXCEPT ![w] = 0]
  /\ lapsed' = [lapsed EXCEPT ![w] = FALSE]

\* Another's claim stood for the whole wait: the task is given back with a delay, or, on its last delivery, ended
\* uncounted and its table held a minute out (_give_back).
GiveBack(w) ==
  /\ Claims /\ ws[w] = "got" /\ cwho # NoOne /\ ~Holds(w) /\ NoFault
  /\ IF wn[w] < MaxDeliver
       THEN Requeues(w) /\ UNCHANGED <<sched, soon>>
       ELSE Removes(w) /\ sched' = TRUE /\ soon' = TRUE
  /\ Leaves(w, "idle")
  /\ UNCHANGED <<env, followup, dueFollow, dueSched, dueSoon, moves, claim, backstop>>

\* The pass reads the table (run_table_pass: the claim at its location, trash.keeps, then the datasets), and holds its
\* next time (run_task: schedule_at, replacing any held), or, for a table the trash keeps, none (next_due is None). A
\* pass that reads the table live has read it after every commit before this step; a commit after it is the next pass's.
Read(w) ==
  /\ ws[w] = "claimed" /\ NoFault
  /\ owed' = IF table = "live" THEN FALSE ELSE owed
  /\ IF table = "live"
       THEN sched' = TRUE /\ soon' = FALSE /\ ws' = [ws EXCEPT ![w] = "scheduled"]
       ELSE UNCHANGED <<sched, soon>> /\ ws' = [ws EXCEPT ![w] = "done"]
  /\ UNCHANGED <<table, hint, commits, drops, followup, dueFollow, dueSched, dueSoon, moves, mst, mholder, mn, net, wcur, wn, lapsed, claim, backstop>>

\* The pass read the table live and the bus did not take its next time: logged, and left to the audit (run_task's
\* QueueUnavailableError).
ScheduleRefused(w) ==
  /\ ws[w] = "claimed" /\ table = "live" /\ Fault
  /\ owed' = FALSE
  /\ backstop' = TRUE
  /\ ws' = [ws EXCEPT ![w] = "done"]
  /\ UNCHANGED <<table, hint, commits, drops, bus, wcur, wn, lapsed, claim>>

\* The pass raised.
PassFails(w) ==
  /\ ws[w] = "claimed" /\ Fault
  /\ ws' = [ws EXCEPT ![w] = "failed"]
  /\ UNCHANGED <<env, bus, wcur, wn, lapsed, claim, backstop>>

\* A failed run is handed back after its back-off, or ended on its last delivery with its next try held recheckHours out
\* (_retry_or_end, _end).
Retry(w) ==
  /\ ws[w] = "failed" /\ NoFault
  /\ IF wn[w] < MaxDeliver
       THEN Requeues(w) /\ UNCHANGED <<sched, soon, backstop>>
       ELSE Removes(w) /\ sched' = TRUE /\ soon' = FALSE /\ backstop' = TRUE
  /\ ws' = [ws EXCEPT ![w] = "acked"]
  /\ UNCHANGED <<env, followup, dueFollow, dueSched, dueSoon, moves, wn, lapsed, claim>>

Ack(w) ==
  /\ ws[w] \in {"scheduled", "done"} /\ NoFault
  /\ Removes(w)
  /\ ws' = [ws EXCEPT ![w] = "acked"]
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, moves, wn, lapsed, claim, backstop>>

\* The claim goes last, and only while it is this handling's (TaskQueue.release at its revision, or its token's).
Release(w) ==
  /\ ws[w] = "acked" /\ NoFault
  /\ IF Holds(w) THEN cwho' = NoOne /\ ccur' = FALSE ELSE UNCHANGED claim
  /\ Leaves(w, "idle")
  /\ wcur' = [wcur EXCEPT ![w] = FALSE]
  /\ UNCHANGED <<env, bus, backstop>>

---------------------------------------------------------------------------
\* Faults of a worker.

\* The process ends between two effects: a crash, a hung pass, a drain outrun, a heartbeat or renewal the bus stopped
\* answering (under A1 the last is an end, never a pass running on unvouched).
Die(w) ==
  /\ ws[w] \notin {"idle", "dead"} /\ Fault
  /\ Leaves(w, "dead")
  /\ wcur' = [wcur EXCEPT ![w] = FALSE]
  /\ UNCHANGED <<env, bus, claim, backstop>>

\* Its settle was sent and not answered: the worker ends, and the network holds the settle, to be applied late.
SettleHeld(w) ==
  /\ ws[w] \in {"scheduled", "done", "failed"} /\ Fault
  /\ net' = IF wcur[w] /\ mst # "none"
              THEN net \cup {IF ws[w] = "failed" THEN (IF wn[w] < MaxDeliver THEN "nak" ELSE "term") ELSE "ack"}
              ELSE net
  /\ Leaves(w, "dead")
  /\ wcur' = [wcur EXCEPT ![w] = FALSE]
  /\ backstop' = TRUE
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, moves, mst, mholder, mn, claim>>

\* Without A1: the process is paused past its lease, and resumes with its pass running on.
Lapse(w) ==
  /\ ~EndsInTime /\ ws[w] \in Handling /\ ~lapsed[w] /\ Fault
  /\ lapsed' = [lapsed EXCEPT ![w] = TRUE]
  /\ UNCHANGED <<env, bus, ws, wcur, wn, claim, backstop>>

Restart(w) ==
  /\ ws[w] = "dead" /\ NoFault
  /\ ws' = [ws EXCEPT ![w] = "idle"]
  /\ UNCHANGED <<env, bus, wcur, wn, lapsed, claim, backstop>>

---------------------------------------------------------------------------
\* The bus's own clocks.

\* A worker is vouched for while it handles the message the stream holds and its lease has not run out.
Vouched(w) == ws[w] \in Handling /\ wcur[w] /\ ~lapsed[w]

\* The ack wait runs out on a delivery nobody vouches for, and the task is queued again.
AckWaitRunsOut ==
  /\ mst = "out" /\ ~Vouched(mholder) /\ NoFault
  /\ mst' = "avail" /\ mholder' = NoOne
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, moves, mn, net, workers, claim, backstop>>

\* A claim its holder no longer renews ages out (the bucket's max age, one ack wait).
ClaimRenewed == ccur /\ ws[cwho] \in Handling \cup {"acked"} /\ ~lapsed[cwho]

ClaimAgesOut ==
  /\ Claims /\ cwho # NoOne /\ ~ClaimRenewed /\ NoFault
  /\ cwho' = NoOne /\ ccur' = FALSE
  /\ UNCHANGED <<env, bus, workers, backstop>>

\* A settle the network held lands, applied to the message by its sequence whoever holds it now.
LateSettle ==
  /\ net # {} /\ NoFault
  /\ \E v \in net :
       IF v \in {"ack", "term"}
         THEN Gone
         ELSE /\ net' = net \ {v}
              /\ IF mst = "out" THEN mst' = "avail" /\ mholder' = NoOne ELSE UNCHANGED <<mst, mholder>>
              /\ UNCHANGED <<mn, wcur>>
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, moves, ws, wn, lapsed, claim, backstop>>

\* A task no delivery is left for (its worker died on every one) stays on the queue, refusing every other task of the
\* table, until the stream's max age (24 h) drops it; the audit then finds the table.
AgesOff ==
  /\ mst = "avail" /\ mn >= MaxDeliver + 1 /\ NoFault
  /\ Gone
  /\ backstop' = TRUE
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, moves, ws, wn, lapsed, claim>>

---------------------------------------------------------------------------
\* The fired schedules' consumer (maintenance-due): a delivery out holds one of its DueCeiling slots until it is acked;
\* one naked with a delay holds it until it is delivered again and settled (measured on nats-server 2.15.2, ADR 0232).
\* Every other table's fired times, and the audit's cadence, are delivered on the same slots.

Out == {"reheld", "acked", "waiting", "retrying"}
SlotsHeld == (IF schedOut \in Out \ {"acked"} THEN 1 ELSE 0) + (IF followOut \in Out \ {"acked"} THEN 1 ELSE 0)
\* A first delivery takes a slot; one naked is delivered again on the slot it holds.
Delivers(o) == o = "none" => SlotsHeld < DueCeiling
SlotsWaiting == (IF schedOut = "waiting" THEN 1 ELSE 0) + (IF followOut = "waiting" THEN 1 ELSE 0)

\* A held time fires, the one a pass held included while that pass still runs: it counts from the pass's start. Fired
\* while its last firing's delivery is still out, the two are one fired message here, delivered anew.
FireSchedule ==
  /\ sched /\ NoFault
  /\ sched' = FALSE /\ soon' = FALSE
  /\ dueSched' = TRUE /\ dueSoon' = soon
  /\ schedOut' = IF schedOut = "reheld" THEN "none" ELSE schedOut
  /\ UNCHANGED <<env, followup, dueFollow, followOut, mst, mholder, mn, net, workers, claim, backstop>>

\* The time held again for a fired schedule that waits for its table's task: guarded, the publish is refused while a
\* schedule is held, which then holds the table (schedule_first); unguarded, it replaces what is held (schedule_at).
RehHoldSchedule ==
  IF ScheduleReholdGuarded /\ sched THEN UNCHANGED <<sched, soon>> ELSE sched' = TRUE /\ soon' = dueSoon

\* The time held again for a fired follow-up: guarded, refused while a follow-up is held, which covers what this one
\* followed (follow_up); unguarded, it replaces what is held (follow_up(replacing=True)). Its minute is no undrop's
\* delay: it may fire between an undrop's announcement and its clear.
RehHoldFollowUp ==
  IF FollowUpReholdGuarded /\ followup # "none" THEN UNCHANGED followup ELSE followup' = "early"

\* A fired schedule is delivered and moved onto the queue once no task holds the table. One that meets a task: with A7
\* held again a minute out and acked (in that order with ReholdFirst), or without A7 naked to come again, holding its
\* delivery. Without A5 it is acked as covered by the task.
MoveSchedule ==
  /\ dueSched /\ schedOut \in {"none", "waiting", "retrying"} /\ Delivers(schedOut) /\ NoFault
  /\ IF mst = "none" \/ ~FiredWaits
       THEN /\ IF mst = "none" THEN Enqueued ELSE UNCHANGED <<mst, mholder, mn, wcur, net>>
            /\ dueSched' = FALSE /\ dueSoon' = FALSE /\ schedOut' = "none"
            /\ UNCHANGED <<sched, soon>>
       ELSE /\ UNCHANGED <<mst, mholder, mn, wcur, net>>
            /\ IF ~WaitByRehold
                 THEN schedOut' = "waiting" /\ UNCHANGED <<sched, soon, dueSched, dueSoon>>
                 ELSE IF ReholdFirst
                   THEN RehHoldSchedule /\ schedOut' = "reheld" /\ UNCHANGED <<dueSched, dueSoon>>
                   ELSE schedOut' = "acked" /\ dueSched' = FALSE /\ UNCHANGED <<sched, soon, dueSoon>>
  /\ UNCHANGED <<env, followup, dueFollow, followOut, wn, ws, lapsed, claim, backstop>>

\* The mover's second effect: the ack after the re-hold, or the re-hold after the ack.
SettleMovedSchedule ==
  /\ schedOut \in {"reheld", "acked"} /\ NoFault
  /\ IF schedOut = "reheld"
       THEN dueSched' = FALSE /\ UNCHANGED <<sched, soon>>
       ELSE RehHoldSchedule /\ UNCHANGED dueSched
  /\ dueSoon' = FALSE /\ schedOut' = "none"
  /\ UNCHANGED <<env, followup, dueFollow, followOut, mst, mholder, mn, net, workers, claim, backstop>>

\* The commit lane holds the table's follow-up unless one is held, which covers this commit. An undrop's announcement, a
\* registration, holds its own, replacing an older one (A6); without A6 an older one covers it too.
Lane ==
  /\ hint /\ NoFault
  /\ followup' = IF table = "undropping" THEN (IF UndropHoldsOwn \/ followup = "none" THEN "late" ELSE followup)
                 ELSE IF followup = "none" THEN "early" ELSE followup
  /\ hint' = FALSE
  /\ UNCHANGED <<table, owed, commits, drops, dueFollow, sched, soon, dueSched, dueSoon, moves, mst, mholder, mn, net, workers, claim, backstop>>

\* The lane gives an announcement up (its last delivery, or a hint it could not read): the recheck backs it.
LaneLoses ==
  /\ hint /\ Fault
  /\ hint' = FALSE /\ backstop' = TRUE
  /\ UNCHANGED <<table, owed, commits, drops, bus, workers, claim>>

\* A follow-up fires a delay after it was held. Between an undrop's announcement and its clear only one held before the
\* announcement may: the one the announcement held waits out its delay, which outlasts the door's two writes (A6).
FireFollowUp ==
  /\ followup # "none" /\ NoFault
  /\ table = "undropping" => followup = "early"
  /\ followup' = "none" /\ dueFollow' = TRUE
  /\ followOut' = IF followOut = "reheld" THEN "none" ELSE followOut
  /\ UNCHANGED <<env, sched, soon, dueSched, dueSoon, schedOut, mst, mholder, mn, net, workers, claim, backstop>>

\* A fired follow-up is delivered and queued once nothing holds the table; one that meets a task waits as a fired
\* schedule does (A7).
MoveFollowUp ==
  /\ dueFollow /\ followOut \in {"none", "waiting", "retrying"} /\ Delivers(followOut) /\ NoFault
  /\ IF mst = "none"
       THEN Enqueued /\ dueFollow' = FALSE /\ followOut' = "none" /\ UNCHANGED followup
       ELSE /\ UNCHANGED <<mst, mholder, mn, wcur, net>>
            /\ IF ~WaitByRehold
                 THEN followOut' = "waiting" /\ UNCHANGED <<followup, dueFollow>>
                 ELSE IF ReholdFirst
                   THEN RehHoldFollowUp /\ followOut' = "reheld" /\ UNCHANGED dueFollow
                   ELSE followOut' = "acked" /\ dueFollow' = FALSE /\ UNCHANGED followup
  /\ UNCHANGED <<env, sched, soon, dueSched, dueSoon, schedOut, workers, claim, backstop>>

SettleMovedFollowUp ==
  /\ followOut \in {"reheld", "acked"} /\ NoFault
  /\ IF followOut = "reheld"
       THEN dueFollow' = FALSE /\ UNCHANGED followup
       ELSE RehHoldFollowUp /\ UNCHANGED dueFollow
  /\ followOut' = "none"
  /\ UNCHANGED <<env, sched, soon, dueSched, dueSoon, schedOut, mst, mholder, mn, net, workers, claim, backstop>>

\* The bus refused a move's publish, the enqueue or the re-hold, or did not answer it: the fired time is naked with a
\* growing delay to be moved again (_move's QueueUnavailableError).
MoveRefused ==
  /\ Fault
  /\ \/ /\ dueSched /\ schedOut \in {"none", "retrying"} /\ Delivers(schedOut)
        /\ schedOut' = "retrying" /\ UNCHANGED followOut
     \/ /\ dueFollow /\ followOut \in {"none", "retrying"} /\ Delivers(followOut)
        /\ followOut' = "retrying" /\ UNCHANGED schedOut
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, mst, mholder, mn, net, workers, claim, backstop>>

\* The process moving a fired time ends between its two effects: its delivery is handed on once the consumer's ack wait
\* runs out (at once here), and what it had not done is not done.
MoverDies ==
  /\ Fault
  /\ \/ /\ schedOut \in {"reheld", "acked"}
        /\ schedOut' = "none" /\ dueSoon' = (schedOut = "reheld" /\ dueSoon) /\ UNCHANGED followOut
     \/ /\ followOut \in {"reheld", "acked"}
        /\ followOut' = "none" /\ UNCHANGED <<schedOut, dueSoon>>
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, mst, mholder, mn, net, workers, claim, backstop>>

\* Without A3: the consumer gives a fired time it delivered and was handed back up.
FollowUpGivenUp ==
  /\ ~DueUnlimited /\ dueFollow /\ followOut \in {"waiting", "retrying"} /\ NoFault
  /\ dueFollow' = FALSE /\ followOut' = "none"
  /\ UNCHANGED <<env, followup, sched, soon, dueSched, dueSoon, schedOut, mst, mholder, mn, net, workers, claim, backstop>>

ScheduleGivenUp ==
  /\ ~DueUnlimited /\ dueSched /\ schedOut \in {"waiting", "retrying"} /\ NoFault
  /\ dueSched' = FALSE /\ dueSoon' = FALSE /\ schedOut' = "none"
  /\ UNCHANGED <<env, followup, dueFollow, followOut, sched, soon, mst, mholder, mn, net, workers, claim, backstop>>

\* The hourly audit, its cadence's firing delivered on the same consumer, queues a live table that neither a schedule,
\* held or fired, nor the queue holds (TaskQueue.held).
Audit ==
  /\ Audits /\ SlotsHeld < DueCeiling /\ table = "live" /\ ~sched /\ ~dueSched /\ mst = "none" /\ NoFault
  /\ Enqueued
  /\ UNCHANGED <<env, followup, dueFollow, sched, soon, dueSched, dueSoon, moves, ws, wn, lapsed, claim, backstop>>

---------------------------------------------------------------------------
\* The table's writers, through the catalog's doors.

Commit ==
  /\ table = "live" /\ commits < MaxCommits /\ NoFault
  /\ owed' = TRUE /\ hint' = TRUE /\ commits' = commits + 1
  /\ UNCHANGED <<table, drops, bus, workers, claim, backstop>>

Drop ==
  /\ table = "live" /\ drops < Drops /\ NoFault
  /\ table' = "dropped" /\ owed' = FALSE /\ drops' = drops + 1
  /\ UNCHANGED <<hint, commits, bus, workers, claim, backstop>>

\* An undrop registers the table and announces it, then clears its trash record.
UndropAnnounces ==
  /\ table = "dropped" /\ NoFault
  /\ table' = "undropping" /\ owed' = TRUE /\ hint' = TRUE
  /\ UNCHANGED <<commits, drops, bus, workers, claim, backstop>>

UndropClears ==
  /\ table = "undropping" /\ NoFault
  /\ table' = "live"
  /\ UNCHANGED <<owed, hint, commits, drops, bus, workers, claim, backstop>>

---------------------------------------------------------------------------
Step(w) == Claim(w) \/ GiveBack(w) \/ Read(w) \/ Retry(w) \/ EndUnrun(w) \/ Ack(w) \/ Release(w) \/ Restart(w)

Next ==
  \/ \E w \in Workers : Pull(w) \/ Step(w) \/ ScheduleRefused(w) \/ PassFails(w) \/ Die(w) \/ SettleHeld(w) \/ Lapse(w)
  \/ AckWaitRunsOut \/ ClaimAgesOut \/ LateSettle \/ AgesOff
  \/ FireSchedule \/ MoveSchedule \/ SettleMovedSchedule \/ Lane \/ LaneLoses \/ FireFollowUp \/ MoveFollowUp
  \/ SettleMovedFollowUp \/ MoveRefused \/ MoverDies \/ FollowUpGivenUp \/ ScheduleGivenUp
  \/ Audit
  \/ Commit \/ Drop \/ UndropAnnounces \/ UndropClears

Fairness ==
  /\ \A w \in Workers : WF_vars(Step(w))
  /\ WF_vars(\E w \in Workers : Pull(w))
  /\ WF_vars(AckWaitRunsOut) /\ WF_vars(ClaimAgesOut) /\ WF_vars(LateSettle) /\ WF_vars(AgesOff)
  /\ WF_vars(FireSchedule) /\ WF_vars(Lane) /\ WF_vars(FireFollowUp) /\ WF_vars(UndropClears)
  /\ WF_vars(SettleMovedSchedule) /\ WF_vars(SettleMovedFollowUp)
  \* Each is enabled only while the queue is empty, which the table's own schedule refills, or a slot is free: strong
  \* fairness.
  /\ SF_vars(MoveFollowUp) /\ SF_vars(Audit) /\ SF_vars(MoveSchedule)

Spec == Init /\ [][Next]_vars /\ Fairness

\* The workers are interchangeable: a config checking safety alone may count them so.
WorkerSymmetry == Permutations(Workers)

---------------------------------------------------------------------------
\* What must always hold.

\* Two passes of one table never run at once, a bus cut, a late settle and a restart included.
OnePassPerTable == Cardinality({w \in Workers : ws[w] \in InPass}) <= 1

\* A running pass holds its table's claim.
ThePassHoldsTheClaim == Claims => \A w \in Workers : ws[w] \in InPass => Holds(w)

\* Something on the bus will pass a live table: its task, a schedule, a follow-up, or an announcement still to be read,
\* unless a fault or a give-up the code leaves to the audit or the recheck happened. A mover between its ack and its
\* re-hold (only without ReholdFirst) holds the table in a live process, as a worker does its pass.
Held == mst # "none" \/ sched \/ dueSched \/ followup # "none" \/ dueFollow \/ hint \/ schedOut = "acked" \/ followOut = "acked"
HeldUnlessBackstopped == table = "live" /\ ~Held => backstop

\* A pass that reads the table after a commit is on its way: the announcement, its follow-up, a give-back's minute, a
\* task not yet read, or one whose holder died before it read. A pass that read before the commit is no such pass.
ReadAfter ==
  \/ hint \/ followup # "none" \/ dueFollow \/ followOut = "acked" \/ (sched /\ soon) \/ ((dueSched \/ schedOut = "acked") /\ dueSoon)
  \/ mst = "avail"
  \/ \E w \in Workers : wcur[w] /\ ws[w] \in {"got", "claimed", "failed"}
  \/ (mst = "out" /\ ~\E w \in Workers : wcur[w] /\ ws[w] \in {"scheduled", "done"})
ACommitIsCovered == owed /\ table = "live" => ReadAfter \/ backstop

\* A fired time that waits for its table's task holds none of the fired schedules' consumer's deliveries: every other
\* table's share is the same, so however many tables wait, the consumer keeps every slot for the firings that are
\* moved, the audit's cadence among them.
AWaitingFiringHoldsNoSlot == SlotsWaiting = 0

\* What must eventually hold once faults stop.

\* No live table is forgotten: from some point on, something always holds it.
TheTableStaysHeld == <>[](table = "live" => Held)

\* Every commit is read by a later pass, unless the table goes.
EveryCommitIsRead == [](owed /\ table = "live" => <>(~owed \/ table # "live"))

\* A pass that died leaves nothing that blocks the next for ever: its claim ages out, its delivery is handed on.
StaleClaim == Claims /\ cwho # NoOne /\ ~(ccur /\ ws[cwho] \in Handling \cup {"acked"})
StaleDelivery == mst = "out" /\ ~(wcur[mholder] /\ ws[mholder] \in Handling)
NothingDeadBlocks == [](StaleClaim => <>~StaleClaim) /\ [](StaleDelivery => <>~StaleDelivery)
=============================================================================
