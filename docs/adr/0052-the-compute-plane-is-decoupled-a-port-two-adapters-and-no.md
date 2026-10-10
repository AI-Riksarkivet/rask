# 0052. The compute plane is decoupled: a port, two adapters, and no engine in the platform (2026-09-04)

docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md "The compute plane is decoupled" (§7.4), closed. What the platform holds and what an adapter holds are now
different things, and the boundary is measurable rather than asserted:

```
ray IMPORT in packages/service-kit/src : 0      ray DEPENDENCY (uv tree)        : 0
ray IMPORT in services/catalog/src     : 0      engine nouns in the OpenAPI     : 0  (was 6)
```

**The port.** `WorkOrder` says what must happen, `Executor` says how the platform asks and learns the
outcome, `task_registry` says which engine may be asked, `attestation` says what a conforming output
is. All in `service-kit`, none naming an engine. `credential_ref` NAMES a credential and never carries
one, so an order is safe to log, queue or replay.

**Two adapters conform to the port.** `inprocess_executor` (synchronous — proving the port does not assume a
poll) and `rayjobs_api_executor` (the dashboard Jobs API on a standing cluster) each declare `capabilities` and
satisfy `Executor`. Every planned run reaches Ray through the second: a stage's and, since CP-044 (2026-10-05), a
training run's, both submitted as a `WorkOrder`. A further engine needs no platform change: register tasks for it,
host something that runs them. The catalog
validates a declaration against the registry without learning any engine's vocabulary, because
`command` is a string it forwards and never parses.

**WHO decides is the RECORD, not a flag.** A transform names a task; the task's registration names the
engine; `engine_choice` routes. A task registered for an engine this deployment does not host is
REFUSED, never run on whatever happens to be configured — that is how the wrong program rewrites a
tenant's data while every status says success. An estate that has declared nothing still follows
`MEDALLION_RAY_ENABLED`, so the opt-in default is unchanged.

**`DURABLE_RECORD` is the capability that earns the CR.** Ray's GCS is not fault-tolerant here, so the
Jobs-API watcher carries `MAX_UNSEEN_POLLS`/`MAX_RESUBMITS` and a poll ceiling. A `RayJob` is an etcd
object, so the adapter advertises durability and `may_resubmit` refuses to resubmit an `UNKNOWN`
handle. The machinery is switched off by a capability rather than deleted, which is the whole reason
that rule lives on the port.

**One premise of the plan was falsified.** It read "steps 1-3 are required for a second engine". A
second engine landed at step 2 with no `RayJob` anywhere. Step 3 is required for KUEUE and BYO
maintenance compute — admission and quota, not plurality.

**Step 5 is deferred with a precedent, not a shrug.** A `Transform` CRD belongs to `rask-operator`:
a CRD without its controller renders unreconciled CRs as objects stuck mid-provision, ruled 2026-08-16
for the `Project` CRD and re-verified live. It is the change that would also retire the dual source a
stage runner row still carries (`stageJob` beside the declaration that supersedes it); both rows live in
`open_lakehouse_diff_left.md`.
