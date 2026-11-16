# 0216. The events poll reads the control stream by sequence, and any catalog replica answers it (2026-11-16)

**Status:** accepted. Source: LH-601 (#841) with LH-583 (#810), whose Q2 the audit of 2026-11-09 decided (ADR 0199
reopened ADR 0027 and ADR 0028: "the poll is to read the durable stream by sequence"); started by the owner, 2026-11-16
("why not fix both", "do these now"). Supersedes ADR 0027's ring buffer and ADR 0028 in whole.

## Context

Each catalog replica kept the control events it followed in a ring buffer of its own, numbered by a counter of its
own, and `GET /management/v1/events` served that buffer. A cursor one replica issued meant nothing to another (measured
2026-09-18: a cursor ahead of a replica's counter answered an empty page, not a reset), so the catalog Service pinned
each client address to one replica (`sessionAffinity: ClientIP`). The pin pinned every caller, not only the poll: the
load test of 2026-11-16 (#812) put nearly every request from the edge and from lineage on one of four replicas, which
ran at its CPU limit while the others idled, and four replicas served under twice what one did.

## Decision

1. **The poll reads the control topic's stream by sequence** (`lakehouse.bus.Bus.read`, the `$JS.API.STREAM.MSG.GET`
   read the follow already makes): the messages after the caller's cursor, at most `LANCE_CONTROL_PAGE_SIZE`, and for a
   new caller the last that many. **The cursor is the stream's sequence**, the same on every replica, so any replica
   answers any poller. `reset` says the stream no longer holds what came after the cursor (retention removed it, or the
   stream was made again).
2. **The poll serves what the control doors admit** (`catalog.api.control_lane.judge`, ADR 0171), judged at the read.
   An event whose signers' keys cannot be read yet ends the page before it, so it is served once they can, never passed
   over. Only the lakehouse's own catalog and maintenance identities may publish on the topic, so an event that holds the
   page under a made-up key id needs a compromised one, and holds it until the key reader reads the list again
   (`KEY_REFRESH_INTERVAL_SECONDS`, 30 s), when it is refused and passed.
3. **The lane is the refusals' alarm.** Each replica still follows the topic, judges each event once and counts a
   refusal in `catalog.signature.refused`, which pages whether or not a console polls. It buffers nothing. It waits for
   keys as it did, once in a while, so a made-up key id is refused and counted once the list is read again; an event it
   still cannot judge is given up and counted as a failed delivery.
4. **The catalog Service has no session affinity.** Nothing a replica holds is needed by another.

## Considered options

- **A shared buffer in NATS KV** (ADR 0028's named path). The stream already is that record; a second copy adds a writer
  and a consistency problem.
- **Keep the affinity, add replicas.** Measured: the affinity is why replicas add nothing.

## Consequences

- The four facts: cloud native (stateless replicas, any can serve), GitOps (no live state to carry), zero trust (the
  poll judges each signature, as the buffer did), Lance-idiomatic (untouched).
- A poll costs one read of the subject's last message and one read per message after the caller's cursor; a console at
  the head reads only the last message.
- Every replica judges each event twice, once following and once serving; judging is a signature check.
