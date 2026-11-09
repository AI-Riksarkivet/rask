# 0195. The three kits fold into one lakehouse library (2026-11-07)

**Reopened 2026-11-09** by [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md): the one package stands; "organised by module" becomes subpackages under a layers contract, and "swapped by Dapr component" is retired, since NATS is the one bus.

**Status:** accepted. Source: the owner's questions of 2026-11-07 ("all this _kit package? Any reason?", then "Should
they be folded into one? How does Lakekeeper structure its code?"), answered with the evidence below and started as the
first step of the XC-196 (#780) / XC-198 (#782) bundle on the owner's `/implement`. Builds on the owner's ruling of
2026-11-06 that service-kit and lineage-kit are the lakehouse's own internals and that a leaving client takes its own
copy or a stock library ([ADR 0193](0193-the-inbox-is-retired-and-the-console-joins-the-product.md) for the last client
it served).

## Context

Measured 2026-11-07 at main `040a6a87a`:

- **Three Python packages, split by audience.** `service-kit` (9,153 source lines) was "the platform a client pins by
  lakehouse tag" (XC-186): exceptions, middleware, probes, logging, the auth plane behind a `[governed]` extra, the
  Arrow guards behind `[arrow]`, the event contract (XC-188) and a `pytest11` plugin for a client's suite (XC-190).
  `lakehouse-kit` (10,005) was "the kernel no client imports", with its data plane behind `[data]`. `lineage-kit`
  (1,806) was the OpenLineage emission and Ed25519 signing seam, "light" so a client process could verify the
  lakehouse's events.
- **Four import contracts held the split, and only the split:** "importing service_kit.exceptions loads neither the
  logging setup nor dotenv", "service_kit and lineage_kit never import each other", "service_kit imports nothing from
  lakehouse_kit" and "lakehouse_kit and lineage_kit never import each other".
- **The split forced seams that existed only for it**: a door-mode `Literal` restating `DoorMode`'s values, the body
  cap's spec code hard-coded as `13`, a second copy of the projected-token read rule in the lineage emitter, an
  envelope module justified by an optional extra, and the signing key's constructor and parser passed as callables.
- **Every importer was the lakehouse.** Since XC-195 deleted the inbox and the client app factory, catalog, lineage and
  maintenance were the only code importing any of the three, and each installed all three with every extra.

The reference catalog, read from source (Lakekeeper `e230d00`, `crates/`): one core crate, `lakekeeper`, about 150k Rust
lines, holds all product logic (api, server, service: authn, authz, admission, catalog store, events, secrets,
storage, tasks, maintenance). A separate crate exists only for a backend swapped behind a core trait, which depends on
the core and never the reverse (`authz-openfga`, `lakekeeper-storage-postgres`, `lakekeeper-secrets-kv2`,
`lakekeeper-events-kafka`, `lakekeeper-events-nats`), a leaf the core uses (`io`, `iceberg-ext`), a language necessity
(the audit proc-macros), and the binary (`lakekeeper-bin`, with the console as its `ui` feature). No crate exists for an
audience: a client gets the OpenAPI document and the documented CloudEvents, and no library. None of those reasons
applies to rask's three: events and secrets are swapped by Dapr component, not by package; OpenFGA is the one
authorizer; Lance is the only format.

## Decision

1. **One package, `packages/lakehouse`, import name `lakehouse`.** service-kit's and lakehouse-kit's modules move to
   `lakehouse.<module>` unchanged (no two of them shared a name), and lineage-kit becomes the subpackage
   `lakehouse.lineage`, its public names unchanged. Every import site moved by prefix in the same change: 2,377
   references in 690 files, with no shim and no old name kept working.
2. **No extras.** Every importer installs all of it, so an extra would guard nothing. The library declares the union of
   the three packages' dependencies at the highest floor each named.
3. **No audience contracts.** The four contracts above are deleted; the lakehouse's own contracts stay: no client or
   compute engine by any chain, no workflow engine, and the catalog's API speaks the spec's error taxonomy.
4. **A client imports nothing of the lakehouse's.** It reaches the lakehouse through the published doors
   (`docs/*-openapi.json`) and events (`docs/COMMIT-EVENTS.md`, the facet schemas under `spec/facets/rask/`). The
   release no longer names a platform package, and the `pytest11` plugin is deleted: its fixtures, which reset the
   process-global state the library and its Dapr SDK keep, are autouse fixtures of the root `conftest.py`.
5. **The seams the split forced go with it**: the door mode is `DoorMode`, the body cap names `ErrorCode.INVALID_INPUT`,
   the lineage emitter reads its token through `lakehouse.governed.machine_identity.read_identity_token`, and the
   envelope and the key holder say what they are for rather than which package could not import which.
6. **The image gates the library itself.** The chart runs modules of it directly (the issuer mirror, the
   authorization model's writer), so `.docker/rest-catalog.dockerfile` import-gates `lakehouse` beside the three
   services.
7. **The three services stay the entrypoints**: one image run with three commands, as Lakekeeper's one binary.

## Consequences

- A new package is made only for a reason of its own, as in the reference: a backend swapped behind an interface, a
  leaf with no lakehouse imports, a language necessity. Never for an audience.
- The test seam travels with this repository, not with a package: a client builds its own.
- `dagger call test-package` is deleted. It ran one member's suite on that member's scoped sync, and the library's
  suite needs the root development group (respx, moto) that such a sync leaves out; `dagger call test` is the lane.
- The three earlier records of the split stay as written: [ADR 0118](0118-common-merges-into-service-kit-r19-2026-07-27.md),
  [ADR 0120](0120-one-compute-lineage-layer-lineage-kit-r21-2026-07-27.md) and
  [ADR 0152](0152-service-kit-ships-py-typed-and-skips-report-zeros.md) describe the packages this one replaces.
