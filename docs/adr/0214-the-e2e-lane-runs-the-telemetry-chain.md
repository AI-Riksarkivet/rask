# 0214. The e2e lane runs the telemetry chain and reads rask's signals behind the Collector (2026-11-16)

**Status:** accepted. Source: owner, 2026-11-16, two answers on XC-208 (#814, which holds the merged row XC-064 #213)
through the multi-question tool: the e2e-stack lane runs the `observability` suite, and that reverses decision 3 of
ADR 0167, which a new ADR supersedes. Supersedes ADR 0167 decision 3; its decisions 1 and 2 stand.

## Context

ADR 0167 decision 3 kept the telemetry chain out of the e2e-stack lane: no Collector, no store, and the
`observability` marker left out, so `tests/e2e-py/test_observability_e2e.py` ran only by hand against a deployed
estate. Its reason was that the store behind the Collector is the platform's, and a development stand-in for it should
not become part of rask's CI.

XC-208 changed what telemetry rask emits and what its alert rules read: metric names, labels, the HTTP conventions
the services are opted into (`OTEL_SEMCONV_STABILITY_OPT_IN=http`), and series created at 0 at start. Each of those is
right only if the signal that leaves a service, crosses rask's Collector and lands in a store carries the name the
rules select. promtool proves a rule's logic on synthetic series and the offline suite reads an in-memory reader; nothing
on push read a real export. A wrong series name leaves every rule over it silent, which is the failure the row exists
to remove, and it would be found at the next deploy's read-back at the earliest.

## Decision

1. **The e2e-stack lane installs the observability fixture and renders rask with `observability.enabled`**, exporting
   to the fixture's store at the endpoint and ingestion headers `deploy/values-local.yaml` names
   (`scripts/e2e_stack.sh`). The empty-cluster install (`scripts/install_from_empty.sh`) keeps the chain off.
2. **The lane runs the `observability` suite** with every other module it can serve: the marker is no longer left out.
   The suite's claims stay about rask's own signals (a service's domain metric, one trace across the catalog, the bus
   and lineage, the services' and the infra pods' logs); the store is where they are read.

## Considered options

- **Keep ADR 0167 decision 3, and read telemetry back per deploy.** Rejected by the owner: the lane already stands in
  for every other platform service with its development fixture (the bus, the issuer, the secret store, the object
  store), each named and none bundled, and the store behind the Collector is no different in kind. Without it nothing
  on push proves the path the alert rules depend on.
- **A Dagger lane of its own with the Collector's rendered config and a store as services.** Rejected: it runs the
  Collector outside Kubernetes, so the filelog leg (infra pod logs) cannot be proven there, and it is the larger change.

## Consequences

- The lane runs four more pods (the store, the dashboards, the rule evaluator and its alert router) inside its
  70-minute ceiling.
- A change to a metric's name, unit or label that the suite reads fails on push.
- The suite reads GreptimeDB's own query doors. A platform that swaps the store changes the fixture and the suite's
  reads together; rask's services and its Collector name no store either way.
