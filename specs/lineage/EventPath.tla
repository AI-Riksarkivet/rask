----------------------------- MODULE EventPath -----------------------------
(***************************************************************************)
(* Lineage's event path: from a producer that announces a committed write  *)
(* to the feed row lineage keeps for it.                                   *)
(*                                                                         *)
(* An event reaches lineage by two routes, and may take both:              *)
(*   - the bus: the producer publishes it, a replica takes the delivery,   *)
(*     judges it and records it (lakehouse.bus_delivery.judged,            *)
(*     lineage.services.consumer.handle_cloud_event);                      *)
(*   - the outbox: the producer stages it before it publishes, and         *)
(*     lineage's relay records what is still staged, publishes it again    *)
(*     and drops it (lakehouse.outbox.publish_with_outbox,                 *)
(*     lineage.api.relay._drain_outbox).                                   *)
(*                                                                         *)
(* Recording is three writes, in this order: the run's binding, the        *)
(* edges, the feed row (ProvenanceStore._commit). Any step of anything may *)
(* fail, any process may die between two steps, and a write a handler gave *)
(* up on may still land later.                                             *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. A write of a row is insert-if-absent and atomic: two racing       *)
(*       writes of one key land one row (the catalog's primary-key         *)
(*       enforcement, catalog.services.primary_keys). A table is therefore *)
(*       a set here.                                                       *)
(*   A2. The bus redelivers a delivery that was not settled, without       *)
(*       limit: the consumer `lineage-ingest` is declared with             *)
(*       max_deliver -1 (chart/templates/_helpers.tpl, lance.busConsumers).*)
(*       The lane ends a delivery itself, by parking it.                   *)
(*       EventPathBusLimit.cfg gives the bus a limit, and NoLoss fails.    *)
(*   A4. The bus keeps a message until the lane settles it. The stream     *)
(*       drops a message older than nats.streamMaxAgeSeconds (7 days in    *)
(*       chart/values.yaml), so this holds while lineage is never away     *)
(*       for longer. EventPathBusForgets.cfg lets the bus drop a waiting   *)
(*       message, and NoLoss fails.                                        *)
(*   A3. The bus never hands a delivery to a second replica while the      *)
(*       first still handles it: a handling is cut 10 s short of the ack   *)
(*       wait (Lane.SETTLE_SECONDS). What can outlive a handling is its    *)
(*       write, which is the `late` set.                                   *)
(*                                                                         *)
(* NOT MODELLED: who may record an event beyond "allowed or refused" (the  *)
(* run's author, OpenFGA), the admin's replay door, the forwarder and the  *)
(* pruning of old rows. A second copy the relay publishes of one event is  *)
(* one message here: a third is handled as the second is.                  *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS Events,       \* the events a run of the model announces
          MaxAttempts,  \* deliveries an event is handled on before the lane parks it
          MaxFaults,    \* how many steps may fail or processes die in one behaviour
          BusDeliveries, \* how many times the bus hands one delivery out before it gives up; 0 for no limit (A2)
          BusForgets     \* whether the bus may drop a message nobody has settled (A4)

VARIABLES kind,       \* what each event is: good, refused (its writer may not write the table), or invalid
          prod,       \* where each event's producer is
          entrusted,  \* events the producer staged or published: lineage answers for these
          outbox,     \* events staged and not yet dropped
          msg,        \* each event's message on the bus, from the producer and from the relay
          handler,    \* what the replica handling a delivery does next
          rows,       \* the three tables an event is recorded in
          late,       \* writes a handler or the relay gave up on, which may still land
          parks,      \* deliveries the lane kept because it could not record them
          refusals,   \* staged events the relay kept because their writer was refused
          relayEv, relayPc,  \* the relay's event and what it does next
          faults      \* failures so far

vars == <<kind, prod, entrusted, outbox, msg, handler, rows, late, parks, refusals, relayEv, relayPc, faults>>

Kinds   == {"good", "refused", "invalid"}
Sources == {"producer", "relay"}
Msgs    == Events \X Sources
Ev(m)   == m[1]

\* The order one event's three writes are made in: the feed row last, so a row in the feed says the rest is there.
Order  == <<"runs", "edges", "feed">>
Tables == {Order[i] : i \in 1..3}
Position(t) == CHOOSE i \in 1..3 : Order[i] = t
\* The step after a write of table t: the next table, or "done" after the last.
After(t) == IF Position(t) = 3 THEN "done" ELSE Order[Position(t) + 1]

Init ==
  /\ kind \in [Events -> Kinds]
  /\ prod = [e \in Events |-> "new"]
  /\ entrusted = {} /\ outbox = {} /\ late = {} /\ parks = {} /\ refusals = {}
  /\ msg = [m \in Msgs |-> [state |-> "absent", attempt |-> 0]]
  /\ handler = [m \in Msgs |-> "none"]
  /\ rows = [t \in Tables |-> {}]
  /\ relayEv \in Events /\ relayPc = "idle"
  /\ faults = 0

Fault == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults

Written(t, e) == rows' = [rows EXCEPT ![t] = @ \cup {e}]

\* A write that failed where it was asked for may still land, or not.
MayStillLand(t, e) == late' \in {late, late \cup {<<e, t>>}}

---------------------------------------------------------------------------
\* The producer: stage, publish, drop. Each may fail, and it carries on.

Stage(e) ==
  /\ prod[e] = "new" /\ NoFault
  /\ outbox' = outbox \cup {e} /\ entrusted' = entrusted \cup {e}
  /\ prod' = [prod EXCEPT ![e] = "staged"]
  /\ UNCHANGED <<kind, msg, handler, rows, late, parks, refusals, relayEv, relayPc>>

StageFails(e) ==
  /\ prod[e] = "new" /\ Fault
  /\ prod' = [prod EXCEPT ![e] = "unstaged"]
  /\ UNCHANGED <<kind, entrusted, outbox, msg, handler, rows, late, parks, refusals, relayEv, relayPc>>

Publish(e) ==
  /\ prod[e] \in {"staged", "unstaged"} /\ NoFault
  /\ msg' = [msg EXCEPT ![<<e, "producer">>] = [state |-> "waiting", attempt |-> 1]]
  /\ entrusted' = entrusted \cup {e}
  /\ prod' = [prod EXCEPT ![e] = IF @ = "staged" THEN "published" ELSE "done"]
  /\ UNCHANGED <<kind, outbox, handler, rows, late, parks, refusals, relayEv, relayPc>>

\* The publish fails, or the producer dies before it: what was staged stays staged.
PublishFails(e) ==
  /\ prod[e] \in {"staged", "unstaged"} /\ Fault
  /\ prod' = [prod EXCEPT ![e] = "done"]
  /\ UNCHANGED <<kind, entrusted, outbox, msg, handler, rows, late, parks, refusals, relayEv, relayPc>>

Drop(e) ==
  /\ prod[e] = "published" /\ NoFault
  /\ outbox' = outbox \ {e}
  /\ prod' = [prod EXCEPT ![e] = "done"]
  /\ UNCHANGED <<kind, entrusted, msg, handler, rows, late, parks, refusals, relayEv, relayPc>>

DropFails(e) ==
  /\ prod[e] = "published" /\ Fault
  /\ prod' = [prod EXCEPT ![e] = "done"]
  /\ UNCHANGED <<kind, entrusted, outbox, msg, handler, rows, late, parks, refusals, relayEv, relayPc>>

---------------------------------------------------------------------------
\* The ingest lane: one delivery of one message, handled by one replica.

Settled(m) ==
  /\ msg' = [msg EXCEPT ![m].state = "settled"]
  /\ handler' = [handler EXCEPT ![m] = "none"]

\* A delivery that was not settled comes back, unless the bus has a limit and this was its last.
Redelivered(m) ==
  /\ msg' = [msg EXCEPT ![m] = IF BusDeliveries > 0 /\ @.attempt >= BusDeliveries
                                 THEN [@ EXCEPT !.state = "settled"]
                                 ELSE [state |-> "waiting", attempt |-> @.attempt + 1]]
  /\ handler' = [handler EXCEPT ![m] = "none"]

\* A handling that could not finish: asked for again, or parked on the last attempt.
Again(m) ==
  IF msg[m].attempt < MaxAttempts
    THEN Redelivered(m)
    ELSE handler' = [handler EXCEPT ![m] = "park"] /\ UNCHANGED msg

\* The stream drops a message that waited past its age.
Forgotten(m) ==
  /\ BusForgets /\ msg[m].state = "waiting" /\ NoFault
  /\ msg' = [msg EXCEPT ![m].state = "settled"]
  /\ UNCHANGED <<kind, prod, entrusted, outbox, handler, rows, late, parks, refusals, relayEv, relayPc>>

Deliver(m) ==
  /\ msg[m].state = "waiting" /\ NoFault
  /\ msg' = [msg EXCEPT ![m].state = "handling"]
  /\ handler' = [handler EXCEPT ![m] = IF msg[m].attempt > MaxAttempts THEN "park" ELSE "judge"]
  /\ UNCHANGED <<kind, prod, entrusted, outbox, rows, late, parks, refusals, relayEv, relayPc>>

Judge(m) ==
  /\ handler[m] = "judge" /\ NoFault
  /\ CASE kind[Ev(m)] = "invalid" -> Settled(m)
       [] kind[Ev(m)] = "refused" -> handler' = [handler EXCEPT ![m] = "park"] /\ UNCHANGED msg
       [] kind[Ev(m)] = "good"    -> handler' = [handler EXCEPT ![m] = Order[1]] /\ UNCHANGED msg
  /\ UNCHANGED <<kind, prod, entrusted, outbox, rows, late, parks, refusals, relayEv, relayPc>>

\* Who may write could not be asked.
JudgeFails(m) ==
  /\ handler[m] = "judge" /\ Fault /\ Again(m)
  /\ UNCHANGED <<kind, prod, entrusted, outbox, rows, late, parks, refusals, relayEv, relayPc>>

Write(m) ==
  /\ handler[m] \in Tables /\ NoFault
  /\ Written(handler[m], Ev(m))
  /\ IF After(handler[m]) = "done" THEN Settled(m) ELSE handler' = [handler EXCEPT ![m] = After(@)] /\ UNCHANGED msg
  /\ UNCHANGED <<kind, prod, entrusted, outbox, late, parks, refusals, relayEv, relayPc>>

WriteFails(m) ==
  /\ handler[m] \in Tables /\ Fault /\ Again(m)
  /\ MayStillLand(handler[m], Ev(m))
  /\ UNCHANGED <<kind, prod, entrusted, outbox, rows, parks, refusals, relayEv, relayPc>>

\* Parking keeps the delivery unless the feed already holds its event.
Park(m) ==
  /\ handler[m] = "park" /\ NoFault
  /\ parks' = IF Ev(m) \in rows["feed"] THEN parks ELSE parks \cup {Ev(m)}
  /\ Settled(m)
  /\ UNCHANGED <<kind, prod, entrusted, outbox, rows, late, refusals, relayEv, relayPc>>

ParkFails(m) ==
  /\ handler[m] = "park" /\ Fault /\ Redelivered(m)
  /\ UNCHANGED <<kind, prod, entrusted, outbox, rows, late, parks, refusals, relayEv, relayPc>>

\* The replica dies mid-handling: the bus hands the delivery out again once its ack wait passes.
ReplicaDies(m) ==
  /\ handler[m] # "none" /\ Fault /\ Redelivered(m)
  /\ IF handler[m] \in Tables THEN MayStillLand(handler[m], Ev(m)) ELSE UNCHANGED late
  /\ UNCHANGED <<kind, prod, entrusted, outbox, rows, parks, refusals, relayEv, relayPc>>

---------------------------------------------------------------------------
\* The relay: one staged event at a time. A step that fails leaves the event staged for the next tick.

Pick(e) ==
  /\ relayPc = "idle" /\ e \in outbox /\ NoFault
  /\ relayEv' = e /\ relayPc' = "judge"
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, rows, late, parks, refusals>>

RelayJudge ==
  /\ relayPc = "judge" /\ NoFault
  /\ relayPc' = CASE kind[relayEv] = "invalid" -> "drop" [] kind[relayEv] = "refused" -> "refuse" [] kind[relayEv] = "good" -> Order[1]
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, rows, late, parks, refusals, relayEv>>

RelayWrite ==
  /\ relayPc \in Tables /\ NoFault
  /\ Written(relayPc, relayEv)
  /\ relayPc' = IF After(relayPc) = "done" THEN "republish" ELSE After(relayPc)
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, late, parks, refusals, relayEv>>

RelayRepublish ==
  /\ relayPc = "republish" /\ NoFault
  /\ msg' = [msg EXCEPT ![<<relayEv, "relay">>] = IF @.state = "absent" THEN [state |-> "waiting", attempt |-> 1] ELSE @]
  /\ relayPc' = "drop"
  /\ UNCHANGED <<kind, prod, entrusted, outbox, handler, rows, late, parks, refusals, relayEv>>

RelayRefuse ==
  /\ relayPc = "refuse" /\ NoFault
  /\ refusals' = refusals \cup {relayEv}
  /\ relayPc' = "drop"
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, rows, late, parks, relayEv>>

RelayDrop ==
  /\ relayPc = "drop" /\ NoFault
  /\ outbox' = outbox \ {relayEv}
  /\ relayPc' = "idle"
  /\ UNCHANGED <<kind, prod, entrusted, msg, handler, rows, late, parks, refusals, relayEv>>

RelayFails ==
  /\ relayPc # "idle" /\ Fault
  /\ relayPc' = "idle"
  /\ IF relayPc \in Tables THEN MayStillLand(relayPc, relayEv) ELSE UNCHANGED late
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, rows, parks, refusals, relayEv>>

\* The relay's tick forgets a kept delivery whose event the feed has come to hold.
RetirePark(e) ==
  /\ e \in parks /\ e \in rows["feed"] /\ NoFault
  /\ parks' = parks \ {e}
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, rows, late, refusals, relayEv, relayPc>>

---------------------------------------------------------------------------
\* A write that was given up on lands after all, and its commit carries on to the next table; or it never lands.

LateLands(e, t) ==
  /\ <<e, t>> \in late /\ NoFault
  /\ Written(t, e)
  /\ late' = (late \ {<<e, t>>}) \cup (IF After(t) = "done" THEN {} ELSE {<<e, After(t)>>})
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, parks, refusals, relayEv, relayPc>>

LateNeverLands(e, t) ==
  /\ <<e, t>> \in late /\ NoFault
  /\ late' = late \ {<<e, t>>}
  /\ UNCHANGED <<kind, prod, entrusted, outbox, msg, handler, rows, parks, refusals, relayEv, relayPc>>

---------------------------------------------------------------------------
Works ==
  \/ \E e \in Events : Stage(e) \/ Publish(e) \/ Drop(e) \/ Pick(e) \/ RetirePark(e)
  \/ \E m \in Msgs : Deliver(m) \/ Judge(m) \/ Write(m) \/ Park(m)
  \/ RelayJudge \/ RelayWrite \/ RelayRepublish \/ RelayRefuse \/ RelayDrop
  \/ \E e \in Events, t \in Tables : LateLands(e, t) \/ LateNeverLands(e, t)

Fails ==
  \/ \E e \in Events : StageFails(e) \/ PublishFails(e) \/ DropFails(e)
  \/ \E m \in Msgs : JudgeFails(m) \/ WriteFails(m) \/ ParkFails(m) \/ ReplicaDies(m) \/ Forgotten(m)
  \/ RelayFails

Next == Works \/ Fails

Spec == Init /\ [][Next]_vars /\ WF_vars(Works)

---------------------------------------------------------------------------
\* What must always hold.

InTransit(e) == e \in outbox \/ \E s \in Sources : msg[<<e, s>>].state \in {"waiting", "handling"}
AtRest(e)    == e \in rows["feed"] \/ e \in parks \/ e \in refusals \/ kind[e] = "invalid"

\* An event the producer staged or published is never lost: it is still on its way, or it is recorded, or it is
\* kept where an admin finds it. (One that cannot be read at all is dropped, and counted.)
NoLoss == \A e \in entrusted : InTransit(e) \/ AtRest(e)

\* Nothing is recorded for an event whose writer was refused or that could not be read.
OnlyAllowedEventsAreRecorded == \A t \in Tables : \A e \in rows[t] : kind[e] = "good"

\* A feed row is the last write: its run's binding and its edges are already there.
AFeedRowIsWhole == \A e \in rows["feed"] : e \in rows["runs"] /\ e \in rows["edges"]

\* The relay keeps as refused only what was refused.
OnlyRefusedEventsAreKeptAsRefused == \A e \in refusals : kind[e] = "refused"

\* What must eventually hold once failures stop: nothing is left on its way.
Quiet ==
  /\ outbox = {} /\ late = {} /\ relayPc = "idle"
  /\ \A m \in Msgs : msg[m].state \in {"absent", "settled"}
  /\ \A e \in Events : prod[e] = "done"
EverythingComesToRest == <>[]Quiet

\* Bounds the delivery count TLC explores; every increase past MaxAttempts costs a fault.
Bounded == \A m \in Msgs : msg[m].attempt <= MaxAttempts + MaxFaults + 1
=============================================================================
