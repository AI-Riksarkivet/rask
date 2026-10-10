# 0067. The three mesh headers stay in the spec document, and the quota has no upstream precedent (owner, 2026-09-20)

Two answers on [[LH-021]] and [[LH-074]], taken after the route and query-parameter halves of LH-021
had already closed.

**The three Dapr mesh headers STAY, documented.** `dapr-api-token`, `x-lance-service-identity` and
`dapr-caller-app-id` appear on all 56 spec-prefixed routes in the served OpenAPI. The row read their
presence in the DOCUMENT as the defect and their presence on the wire as correct, which would have
made `Header(include_in_schema=False)` a free documentation fix — the same move the five rask-only
QUERY parameters took the same day.

It is not free, and that is why the answer differs. Two of the three have typed frontend consumers
(`frontend/packages/api/src/runs-feed.ts:228-229`, `bff.ts:194`), and
`frontend/packages/zone-contract/src/generated-client-freshness.test.ts` exists BECAUSE a generated
client that lost `dapr-caller-app-id` shipped broken for 19 days on 27 operations. That gate compares
the spec to the client, so removing a header from the spec removes it from what the gate can check —
on the exact header whose absence the gate was written for. Hiding them would buy a cleaner document
by weakening the control that caught the last failure of this kind.

So LH-021's second clause closes as a STATED EXCEPTION rather than a clean sweep: these are how a
service authenticates inside the mesh, documenting them is honest, and a spec client meets three
headers it can ignore. `x-lance-originator`, `Idempotency-Key` and `X-Lance-Run-Facets` ride the same
answer. The exception is pinned by
`tests/integration/test_the_spec_surface_carries_only_spec_parameters.py`, which allowlists exactly
these and fails on a FOURTH — so the decision is a bound, not a licence.

**The byte quota has no upstream precedent to be idiomatic to.** [[LH-074]] asked where a quota
belongs, and the honest first finding is that neither comparable catalog defines one. Measured
2026-09-20: `quota`, `max_bytes`, `storage limit` and `capacity` appear ZERO times in the vendored
lance-namespace spec and docs, and zero times in Lakekeeper's 17,677-line management OpenAPI. rask
would be inventing the concept, not adopting it.

What IS established, and agrees across both, is the SHAPE of per-warehouse policy. Lakekeeper carries
warehouse-level settings as sub-resources — `/management/v1/warehouse/{id}/delete-profile`,
`/management/v1/warehouse/{id}/protection` — and rask already has that exact shape for protection on
the warehouse, namespace and table tiers, now mounted at `/management/v1` by LH-021's route work. So
if a quota lands, it belongs there: a warehouse-scoped sub-resource under the management prefix,
absent by default, never a field in a spec create payload. Storage accounting is already per BUCKET
and a warehouse maps to a bucket, so the quota sits where the measurement already lands.
