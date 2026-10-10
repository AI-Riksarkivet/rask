# 0130. A manual push to bronze is an authorization policy, not a tier guard (ingest 2, 2026-08-07)

Source: `docs/architecture/ingest-and-tier-movement.md` (decision 2 and "The two policies this plan was the only record
of"), ruled in `open_ingest_design.md` on 2026-08-07, migrated 2026-08-22. Corrected here against the code: the
source text says the policy is a human `writer` on `namespace:<proj>-bronze`, and that is not what the two push doors
check (see Consequences; the discrepancy was found by `docs/audits/2026-09-30/lakehouse-dataflow.md:316`).

## Context

The ask was "manual push to bronze only": a person may put data into bronze and nowhere above it. Nothing in the
catalog's FGA layer or the model distinguishes a human principal from a service one — every subject is `user:`, and
services are `user:service-*`. A code-level tier guard would therefore have to INVENT that distinction, and an
invented one drifts from the tuples that actually decide.

## Decision

- **The rule is expressed in tuples, not code.** Who may push where is decided by grants: a person is granted a write
  rung on the bronze namespace of a project and nothing above it, while the stage runners keep `can_create_table` /
  `can_promote` on the silver and gold namespaces under their own service identities, which is what they already
  check.
- **A manual push uses `merge_insert`, not a raw insert** (corrected 2026-08-07): `when_not_matched_insert_all()` is
  native insert-if-not-matched, so a re-push converges instead of duplicating. The catalog's `merge_insert` door is
  gated on `can_write_data` (`services/catalog/src/catalog/api/fga_deps.py:311-330`), which a table inherits from a
  `writer` on its namespace (`packages/service-kit/src/service_kit/governed/auth/model.fga:538`).

## Consequences

- **The two human-facing ingest doors do not check a bronze-namespace writer.** Both the medallion producer
  (`POST /produce`, `POST /ingest-media`) and the ingest service (`POST /v1/ingests`) authorize a person on
  `can_administer` on the PROJECT the call acts on: `services/medallion/src/medallion/api/produce_auth.py:59-73`
  and `services/ingest/src/ingest/auth.py:101-114`, with the door rationale at `auth.py:19-24`. So today a namespace
  writer can push to bronze only through the catalog's own data doors (`merge_insert`), and the bulk/harvest doors
  are project-admin doors. The tuple policy above governs the catalog door; it does not describe the producer or
  ingest doors.
- No seeding path grants the human bronze writer: `scripts/seed_estate.py` drives the real doors in hierarchy order
  and seeds service writers (e.g. `user:service-bronze-to-silver` as `writer` on `namespace:acme-silver`,
  `seed_estate.py:298`) but no human bronze writer, so the policy is unexercised rather than enforced or violated.
- The UI for a manual push is missing; the catalog door accepts it.
