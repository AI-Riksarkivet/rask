# 0064. The port's Ray adapter is the Jobs API, not a RayJob CR (2026-09-15)

Supersedes the compute-plane entry above (2026-09-04), which reads "a port, TWO adapters" and names
`rayjob_executor` as one of them. That adapter no longer exists. The 2026-09-04 record stands as what
was decided then; this is what replaced it.

**What was deleted and why.** `RayJobExecutor` submitted a `RayJob` CR against the Kubernetes API, with
Kueue admission and `DURABLE_RECORD` earned by the CR being an etcd object. It had **zero production
callers** — measured, it was constructed nowhere outside tests — and the CR's purpose, provisioning a
cluster per job, does not apply to the standing shared cluster this estate runs. A second, older seam
(`ray_submit`, the Jobs API) was what the cascade actually used, so the port sat in front of a lane
nobody ran while the lane that ran went around it.

A further reason surfaced from the wire gate and is the sharper one: that adapter could not have worked
against the jobs it would have launched. `WorkOrder.to_env()` supplies `RASK_SOURCE_URI`/`RASK_DEST_URI`/…
and `scripts/ray_stage_job.py` reads `FROM_URI`/`TO_URI`/… — **0 of 6 overlap**. A job submitted through
it would have started with none of its inputs bound, scanned nothing and written nothing, which no test
said. Wiring it would have shipped a working-looking path that produced empty tiers.

**What replaced it.** `RayJobsApiExecutor` wraps the submission path the cascade already uses: a
standing cluster addressed through the dashboard Jobs API. It takes one optional `httpx.AsyncClient`
(injected for tests, pooled in production) and no credential — the Ray pods carry their own. It
advertises `CANCEL` and `FAILURE_DETAIL` and **deliberately not `DURABLE_RECORD`**: without a CR there
is no etcd object to survive a GCS loss, and that absence is what keeps the resubmit machinery
(`MAX_UNSEEN_POLLS`/`MAX_RESUBMITS`) load-bearing rather than decorative. A capability withheld is the
port doing its job.

**The count did not change and the shape did.** There are still two adapters — `InProcessExecutor` and
`RayJobsApiExecutor` — but the Ray one is now the lane that runs, so "a port with a dead adapter is a
decoupling claim, not a decoupled system" stopped being true of this estate rather than being argued
away. `engine_registry` resolves both names.

**WHAT THIS ENTRY DOES NOT CLAIM: that both lanes go through the port.** They do not. `executor_for`
still has ZERO production callers, and `transform.py` hand-builds `InProcessExecutor` because it then
reads `executor.result(handle)` — and `result()` is deliberately NOT on the port, its own docstring
calling it "only ever an optimisation" while that caller raises when the value is absent. So an
optimisation is consumed as a requirement, and routing the in-process lane through the port forces a
choice between re-deriving (a second stats read for numbers identical by construction), narrowing back
to the concrete type (which defeats resolving), or widening the port with an optional result capability
(honest, and a `service-kit` change every future adapter inherits). That is an open decision, tracked as
LH-158, not a thing this entry settles. **It was settled on 2026-09-17 — see the next entry.**
