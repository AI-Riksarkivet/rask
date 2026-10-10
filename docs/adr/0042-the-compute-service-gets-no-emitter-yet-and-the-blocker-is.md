# 0042. The compute service gets no emitter yet, and the blocker is identity (2026-08-16)

**Decision.** `services/compute` stays a read-only observability surface. A Ray job reaching FAILED
still notifies nobody, and that is DEFERRED with a stated blocker rather than left as an open gap
nobody named.

**Why not now — the blocker is not effort.** Every route in the service is a GET
(`compute/routes.py`), it publishes nothing, and `ray-kit` has no emitter. Adding one is mechanically
possible. What makes it pointless today is that **compute holds no identity for any job**: `RayJob` has
no author field, so an emitter here would produce events naming nobody — undeliverable by the plane's
own rule ("a state change that names nobody is not under-delivered, it is UNDELIVERABLE"), and the
notification plane would correctly discard every one of them. Building a producer whose output the
consumer is designed to drop is worse than building nothing, because it looks like coverage.

**The two lanes that DO have an identity already emit**, which is why this is a narrow gap rather than
a hole: a medallion stage job is watched by `medallion.workflow`, which emits a FAIL carrying
`lance.originator`; a training job emits its own OpenLineage lifecycle carrying the same field. Both
were closed on 2026-08-16. What is left is jobs submitted OUTSIDE those doors — by hand, or by a
KubeRay lane whose `runners/htr` emits no lineage at all.

**What would make it worth building, stated so the condition is checkable.** A submitter identity on
every Ray job, not just the medallion's. The mechanism now exists and is proven — Ray's own `metadata`
carries `rask.originator`, readable from `GET /api/jobs/<id>` after the job has died — but it is
stamped only by `medallion.services.ray_submit`. Requiring it at the CLUSTER's door (refusing a
submission that names no originator) is the change that turns compute into a producer worth having.
Until then an emitter here would need a terminal-transition detector and durable "already reported"
state — a new stateful surface in a service whose whole design is that it holds none — to publish
events addressed to no one.
