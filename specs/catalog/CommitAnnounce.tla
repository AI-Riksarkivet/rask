--------------------------- MODULE CommitAnnounce ---------------------------
(***************************************************************************)
(* A write the catalog commits, and its announcement on lineage's feed.     *)
(*                                                                         *)
(* A door commits a version and then announces it. A crash between the     *)
(* two loses the announcement, so the table's own version log is the       *)
(* outbox: the commit reconciler reads each version after its mark, looks  *)
(* for it on the feed, and announces again what the feed lacks, signed for *)
(* the author the catalog recorded for that commit and for no other        *)
(* (catalog.services.commit_reconcile, ADR 0187, LH-497).                  *)
(*                                                                         *)
(* A door records the author in one of two orders                          *)
(* (catalog.services.commit_authors):                                      *)
(*   detached   record, then commit    (dataplane.commit_stamped, a stock  *)
(*              client's version/create);                                  *)
(*   one call   commit, then record    (the create door's write_dataset,   *)
(*              an unkeyed insert): Lance commits inside one call and      *)
(*              mints the transaction's id there.                          *)
(*                                                                         *)
(* What becomes of an announcement once it is staged or published is       *)
(* specs/lineage/EventPath.tla. Here it is one step: it lands on the feed, *)
(* or lineage keeps it off the feed (it refuses it, or cannot read it).    *)
(*                                                                         *)
(* WHAT THE MODEL ASSUMES, and where each is held:                         *)
(*   A1. Versions commit in order, one winner each (Lance's commit is a    *)
(*       compare-and-swap in the object store).                            *)
(*   A2. An announcement and a second one of the same commit are one row   *)
(*       on the feed: the run id is derived from the commit                *)
(*       (catalog.core.commit_ids), and the feed is insert-only by key.    *)
(*   A3. The reconciler looks at a version before the feed's retention has *)
(*       passed it (it runs with maintenance's hourly audit; the horizon   *)
(*       is the feed's retention, days). CommitAnnounceSlow.cfg removes    *)
(*       this, and an authored write is given up unannounced.              *)
(*                                                                         *)
(* NOT MODELLED: branches, a registered table's earlier versions, renames, *)
(* operations no door announces, and announcements that are not of a       *)
(* commit (grants, drops: catalog.services.announcement_intents).          *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS Last,        \* versions are 1..Last
          MaxFaults,   \* how many steps may fail or doors die in one behaviour
          Reconciles,  \* whether the reconciler runs at all
          InTime       \* whether it looks at a version before the feed's retention passes it (A3)

VARIABLES shape,      \* each version's door: "detached" or "onecall"
          accepted,   \* whether lineage will put the version's announcement on the feed
          door,       \* what each version's door does next
          committed,  \* versions that landed
          authored,   \* versions the catalog recorded an author for
          sent,       \* announcements handed to the outbox or the bus
          feed,       \* versions the feed holds a row for
          age,        \* how old the store says a version is: "young", "settled" (past the grace), "old" (past the horizon)
          mark,       \* the reconciler's mark: every version up to it is accounted for
          counted,    \* versions the reconciler counted as unauthored, and paged for
          again,      \* versions the reconciler announced again
          faults

vars == <<shape, accepted, door, committed, authored, sent, feed, age, mark, counted, again, faults>>

Versions == 1..Last

Init ==
  /\ shape \in [Versions -> {"detached", "onecall"}]
  /\ accepted \in [Versions -> BOOLEAN]
  /\ door = [v \in Versions |-> "start"]
  /\ committed = {} /\ authored = {} /\ sent = {} /\ feed = {} /\ counted = {} /\ again = {}
  /\ age = [v \in Versions |-> "young"]
  /\ mark = 0
  /\ faults = 0

Fault == faults < MaxFaults /\ faults' = faults + 1
NoFault == faults' = faults

\* The step after `step` for a door of `kind`.
Then(kind, step) ==
  IF kind = "detached"
    THEN CASE step = "start" -> "record" [] step = "record" -> "commit" [] step = "commit" -> "announce" [] step = "announce" -> "done"
    ELSE CASE step = "start" -> "commit" [] step = "commit" -> "record" [] step = "record" -> "announce" [] step = "announce" -> "done"

---------------------------------------------------------------------------
\* A door: record the author, commit and announce, in its shape's order.

Begin(v) ==
  /\ door[v] = "start" /\ NoFault
  /\ door' = [door EXCEPT ![v] = Then(shape[v], "start")]
  /\ UNCHANGED <<shape, accepted, committed, authored, sent, feed, age, mark, counted, again>>

Record(v) ==
  /\ door[v] = "record" /\ NoFault
  /\ authored' = authored \cup {v}
  /\ door' = [door EXCEPT ![v] = Then(shape[v], "record")]
  /\ UNCHANGED <<shape, accepted, committed, sent, feed, age, mark, counted, again>>

\* A version lands once the one before it has (A1).
Commit(v) ==
  /\ door[v] = "commit" /\ NoFault
  /\ v = 1 \/ (v - 1) \in committed
  /\ committed' = committed \cup {v}
  /\ door' = [door EXCEPT ![v] = Then(shape[v], "commit")]
  /\ UNCHANGED <<shape, accepted, authored, sent, feed, age, mark, counted, again>>

Announce(v) ==
  /\ door[v] = "announce" /\ NoFault
  /\ sent' = sent \cup {v}
  /\ door' = [door EXCEPT ![v] = "done"]
  /\ UNCHANGED <<shape, accepted, committed, authored, feed, age, mark, counted, again>>

\* The door dies, or its step fails and the request ends: nothing after this step happens.
DoorStops(v) ==
  /\ door[v] \in {"record", "commit", "announce"} /\ Fault
  /\ door' = [door EXCEPT ![v] = "done"]
  /\ UNCHANGED <<shape, accepted, committed, authored, sent, feed, age, mark, counted, again>>

---------------------------------------------------------------------------
\* Lineage, as EventPath.tla leaves it: an announcement it accepts lands on the feed.

Lands(v) ==
  /\ v \in sent /\ accepted[v] /\ v \notin feed /\ NoFault
  /\ feed' = feed \cup {v}
  /\ UNCHANGED <<shape, accepted, door, committed, authored, sent, age, mark, counted, again>>

---------------------------------------------------------------------------
\* Time, by the store's clock: a version is never older than the one before it.

Older(v) == IF v = 1 THEN TRUE ELSE age[v - 1] # "young"
Oldest(v) == IF v = 1 THEN TRUE ELSE age[v - 1] = "old"

\* Lineage would put the version on the feed and the reconciler can still get it there.
StillOwed(v) == v \in authored /\ accepted[v] /\ v \notin feed

PastGrace(v) ==
  /\ v \in committed /\ age[v] = "young" /\ Older(v) /\ door[v] = "done" /\ NoFault
  /\ age' = [age EXCEPT ![v] = "settled"]
  /\ UNCHANGED <<shape, accepted, door, committed, authored, sent, feed, mark, counted, again>>

\* The feed's retention passes the version. In time (A3), that is after the reconciler had its passes at it: it got
\* an authored version onto the feed where lineage accepts it, and counted one with no author.
PastHorizon(v) ==
  /\ age[v] = "settled" /\ Oldest(v) /\ NoFault
  /\ InTime => ~StillOwed(v) /\ (v \in authored \/ v <= mark)
  /\ age' = [age EXCEPT ![v] = "old"]
  /\ UNCHANGED <<shape, accepted, door, committed, authored, sent, feed, mark, counted, again>>

---------------------------------------------------------------------------
\* The reconciler, one version at a time (commit_reconcile._scan_ref).

OnFeed(v) == v \in feed \/ age[v] = "old"

\* A version the feed lacks whose author is recorded is announced again, as that author.
AnnounceAgain(v) ==
  /\ Reconciles /\ v \in committed /\ v > mark /\ age[v] = "settled" /\ NoFault
  /\ ~OnFeed(v) /\ v \in authored
  /\ sent' = sent \cup {v}
  /\ again' = again \cup {v}
  /\ UNCHANGED <<shape, accepted, door, committed, authored, feed, age, mark, counted>>

\* The mark passes the next version once it is on the feed, past the horizon, or counted for having no author.
Confirm ==
  /\ Reconciles /\ mark < Last /\ NoFault
  /\ LET v == mark + 1 IN
       /\ v \in committed /\ age[v] # "young"
       /\ OnFeed(v) \/ v \notin authored
       /\ counted' = IF ~OnFeed(v) THEN counted \cup {v} ELSE counted
       /\ mark' = v
  /\ UNCHANGED <<shape, accepted, door, committed, authored, sent, feed, age, again>>

---------------------------------------------------------------------------
Works ==
  \/ \E v \in Versions : Begin(v) \/ Record(v) \/ Commit(v) \/ Announce(v) \/ Lands(v) \/ PastGrace(v) \/ PastHorizon(v) \/ AnnounceAgain(v)
  \/ Confirm

Next == Works \/ \E v \in Versions : DoorStops(v)

Spec == Init /\ [][Next]_vars /\ WF_vars(Works)

---------------------------------------------------------------------------
\* What must always hold.

\* A change that did not happen is never announced.
OnlyCommittedVersionsAreAnnounced == sent \subseteq committed /\ feed \subseteq committed

\* The reconciler signs an announcement only for an author the catalog recorded.
AnnouncedAgainOnlyForARecordedAuthor == again \subseteq authored

\* The mark passes a version only when it is on the feed, past the feed's retention, or counted.
TheMarkPassesOnlyWhatIsAccountedFor == \A v \in 1..mark : v \in feed \/ age[v] = "old" \/ v \in counted

\* A write the catalog recorded an author for, and lineage accepts, is never given up: the mark passes it only on the feed.
AnAuthoredWriteIsNotGivenUp == \A v \in 1..mark : (v \in authored /\ accepted[v]) => v \in feed

\* A write with no recorded author is counted, and paged for, before the mark passes it: never aged out unseen.
AWriteWithNoAuthorIsCounted == \A v \in 1..mark : (v \notin authored /\ v \notin feed) => v \in counted

\* What must eventually hold once failures stop.

\* Every write that landed with its author recorded, and that lineage accepts, reaches the feed.
EveryAuthoredWriteReachesTheFeed == \A v \in Versions : [](v \in committed /\ v \in authored /\ accepted[v] => <>(v \in feed))

\* EVERY write that landed and that lineage accepts reaches the feed. This does NOT hold, by a ruling (owner,
\* 2026-11-01, catalog.services.commit_authors): a one-call door that dies between its commit and its record leaves a
\* version with no recorded author, which is counted and paged for, never announced for anyone.
EveryWriteReachesTheFeed == \A v \in Versions : [](v \in committed /\ accepted[v] => <>(v \in feed))

\* Every version that landed is accounted for in the end: on the feed, past retention, or counted.
EveryVersionIsAccountedFor == <>[](\A v \in Versions : v \in committed => v \in feed \/ age[v] = "old" \/ v \in counted)
=============================================================================
