# 0172. A location claim's release replaces the claim it read, and nothing deletes a `_locations/` record (2026-10-19)

**Status:** accepted. Source: owner, 2026-10-19, choosing between two designs while LH-458 (#571) was triaged.
Refines ADR 0097.

## Context

ADR 0097 gives each dataset location one claim record on the control root, created put-if-not-exists and handed from a
rename's source to its destination under its ETag. A release read the holder and then deleted the record with no
precondition, so a hand-over that landed between that read and the delete was deleted with it: the destination resolved
to the location with no claim, and a third id could take the location and register over the same bytes. That is two
live ids on one dataset, the loss ADR 0097 exists to prevent, and a destructive drop of either deletes what the other
resolves to. Reproduced deterministically with the real `service_kit.lakehouse.location_claims` on a local control
root (2026-10-19, at `23460156`).

## Decision

- A release replaces its table's claim with the same claim stamped `released_at`, under the ETag of the claim it read
  (`If-Match`, through `records.mutate_json`, the primitive the hand-over writes with). A claim another taker wrote
  after that read fails the replace; the release judges the claim it then reads, leaves it, and answers that it
  released nothing.
- `location_claims.holder` and `location_claims.take` read a released claim as a free location.
- Nothing deletes a `_locations/` record. A released location keeps one small record.

## Considered options

- Delete the record under `If-Match`: not chosen. The estate requires two conditional writes of its object store,
  `If-None-Match: *` and `If-Match` on PutObject (`docs/audits/2026-10-18/rustfs-zero-trust.md` § What rask must demand
  of ANY platform store, item 5); a conditional delete is not one of them, and would need the development store and the
  platform's S3 measured first.
- Remove released records later, by a sweep: rejected. An unconditional delete of a released record removes a claim
  taken over it since it was read, which is the same race.

## Consequences

- After a location's record is created, every write to it is conditional on the record its writer judged.
- Every location ever claimed keeps a record on the control root.
- Pinned by `packages/service-kit/tests/test_a_release_frees_only_the_claim_it_read.py`.
