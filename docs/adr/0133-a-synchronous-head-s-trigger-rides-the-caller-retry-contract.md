# 0133. A synchronous head's trigger rides the caller-retry contract (2026-08-15)

Source: `docs/architecture/medallion-cascade.md` §11 (DROPPED 2026-08-15 in `open_medallion_workflow.md`, commit
`6e35fe45`; migrated 2026-08-22).

## Context

An atomicity audit listed the three HTTP-initiated heads, `/ingest-media`, `/produce` and `/train`, as having no
durable carrier for the trigger they publish after committing: a pod death in between would strand healthy data with
the cascade stopped.

## Decision

The finding is dropped: the premise is false and the design is deliberate. **The caller IS the transaction boundary
for a synchronous head.**

- `media_produce.py` keeps the media-chain trigger a bare publish on purpose: the outbox re-ingests lineage, it never
  re-fires triggers, and trigger loss is the documented idempotency-token caller-retry contract
  (`services/medallion/src/medallion/services/media_produce.py:239-240`).
- `produce.py` documents the contract: the route's 503 tells the caller to retry; a retry that minted a FRESH token
  would double-fire the head as two unrelated runs, so the caller's `Idempotency-Key` is REUSED; every downstream
  `run_id` derives from it, so the graph merges the duplicate and the writes land the same data
  (`services/medallion/src/medallion/services/produce.py:117-120`). `/train` carries the same key-reuse contract
  (`services/medallion/src/medallion/services/train.py:186`; the header at `api/train.py:104`).
- The two failure domains differ: a failed EMIT means no run landed (a retry re-ingests, no duplicate possible); a
  failed TRIGGER after a landed emit still 503s, and the retry emits for a NEW bronze version, so every COMPLETE in
  the graph maps to a real committed write.

## Consequences

- **The residual risk, stated plainly:** a caller that does not retry strands the work. That is inherent to the
  synchronous shape, and is why the idempotency key is a skill rule: an operation whose route invites retry must pair
  it with one.
- **What would change this is an API decision, not a durability fix.** Making the obligation durable server-side
  means accepting the request, persisting the intent and returning 202, which turns three synchronous 503-retry heads
  into asynchronous accept-and-report heads: a contract change for every caller.
