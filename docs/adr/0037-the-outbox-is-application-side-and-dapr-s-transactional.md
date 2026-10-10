# 0037. The outbox is application-side, and Dapr's transactional outbox cannot replace it (2026-08-15)

**Decision.** `service_kit.lakehouse.outbox` — stage the event to object storage, publish, drop on ack,
relay any survivor — stays. Dapr's transactional outbox is not an available alternative, and the
proposal to reach it by "writing a transactional marker to Dapr state after the Lance write" does not
work. Recorded here because the working notes that posed it as an open question are deleted, and a
decision must outlive them.

**Why Dapr's cannot apply.** Its outbox is a property OF A DAPR STATE STORE: it publishes the message
inside the transaction Dapr is already running, which requires the write to go through Dapr's
transactions API into a transactional state store. The docs scope it exactly that way and add that the
guarantee stops at Dapr's own API boundary — *"direct queries of the state store are not governed by
Dapr concurrency control … Writes should be done via the Dapr state management or actors APIs."*
rask's authoritative writes do not go through it: a Lance dataset is written by the Lance library, and
`public.lineage_events` + the AGE graph by lineage's own `psycopg` pool. No state store in the estate
carries `outboxPublishPubsub`/`outboxPublishTopic`, and setting them would change nothing, because
Dapr is never asked to perform those writes.

**Why the marker variant does not rescue it.** A marker written to Dapr state AFTER the Lance commit
leaves the identical crash window: the commit succeeds, the process dies, no marker is written and no
event is published. It RELOCATES the gap rather than closing it. The same flaw kills the sibling
proposal to put cascade triggers behind Dapr Workflow — Workflow's durability begins only once the
schedule call RETURNS, so `write → start workflow` carries the same window as `write → publish`, and
the architecture page separately warns Workflow "may not be appropriate for latency-sensitive
workloads".

**The bound that makes all of this a trade rather than a bug.** Atomicity between object storage and a
message broker does not exist — no distributed transaction spans "commit a Lance dataset to S3" and
"publish to NATS", in Dapr or anywhere. So the goal is not atomicity; it is NO SILENT LOSS: every gap
either closes itself or announces itself.

**What the application-side outbox actually buys, stated without overclaiming.** It shrinks the window,
it does not close it: `stage_event` runs AFTER the Lance commit, so a crash in the commit→stage gap
still loses the event. That ordering is deliberate — staging after the commit means every surviving
object is a real committed write, so there are no phantom events. The trade bought *no phantoms* and
paid with *a small gap*. It is also not "transactional" in Dapr's sense and should not be described as
such.

**The long-term direction, if this is ever revisited.** A commit-EMBEDDED record: Delta CDF commits
change data inside the transaction log, and Lance already writes a per-commit transaction file. An
event derived from the commit cannot diverge from it. Today's staged object is a defensible interim.
