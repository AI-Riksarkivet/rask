# 0074. A duration is not evidence of the mechanism that produced it (2026-09-22)

[[LH-190]] was filed HIGH on a measured 324s gap in the maintenance work lane after a worker restart,
and attributed it to the consumer's `ackWait: 720s`. The number was real. The attribution was not:
nothing in the original trace separated "the lane is refusing to deliver" from "the pods are not
there", and 720s was simply the nearest configured number of the right order.

Re-measured on the live lane with 5s sampling from the NATS monitoring port, both restart shapes:

* **one replica of two** — delivery frozen for a single sample, `num_redelivered` 26 at +13s, full
  throughput by +39s;
* **both replicas** — `delivered` frozen for exactly the 113s the pods were absent, then redelivery
  and full throughput **5 seconds after readiness**.

Neither waited the ack timer. NATS observes the subscriber's connection drop and redelivers; the 720s
timer is the fallback for a subscriber that is still connected and silent, which a dead pod is not.

**The remedy the row proposed would have fixed the case that already worked.** A graceful SIGTERM
drain only exists for a voluntary shutdown — and `retry_when_draining` already answers RETRY there,
while a PDB (`minAvailable: 1`) and a rollout that floors to 0 unavailable make a both-replica
voluntary loss unreachable. The case that costs time is the one where no process survives to drain.

**The rule.** A row that names a mechanism must carry a measurement that could have distinguished it
from its neighbours, not only one consistent with it. Here the distinguishing instrument was
`num_redelivered` — zero while stalled means the timer never fired — and it cost one extra column in
the same sample.

**The rule caught its own author within the hour.** The same traces were read a second time as a
standing stall — `num_ack_pending` pinned at exactly `max_ack_pending`, `ack_floor` advancing only
when a worker was killed — and filed HIGH. It was wrong for precisely the reason above. `ack_floor`
carries two fields: `consumer_seq` counts DELIVERIES and moves in jumps, `stream_seq` names a
position. The stream floor (261,394) tracked the stream's own `first_seq` (261,395) exactly, so every
acked unit had already been removed and nothing was held; and a backlogged queue sitting at its
flow-control bound is what health looks like, not what a stall looks like. The distinguishing
instrument was one field over, in the object already being read.

What survives is a number rather than a defect, and it belongs to [[LH-191]]: the lane drains ~4.5
units/s against the 4.75/s the planner injects, so it needs ~127s to clear a tick and gets 120.
