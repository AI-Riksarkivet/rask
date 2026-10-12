# The service fleet: ports, entrypoints, and which package each composes

The fleet (`make dev-micro`, driven by `scripts/dev-micro.sh`) is SIX processes:
gateway `:8888` + compute `:8804` + controlplane `:8820` + the explorer trio —
viewer `:8101`, search `:8102`, annotator `:8103`. (`PORT_OFFSET=<n>` shifts all six
so a second fleet can share a host.) The orchestrator process (`:8810`) died at P7a;
core-api/search-api/volumes-api died in the R6/R20 media wave (lance-ns-merge.md P7).

The frontend's Vite proxy targets `:8888` (the gateway) for `compute`/`studio`/`train`
**only**; `home`/`lakehouse` proxy `/api` to `LANCE_BACKEND` (`:8001`, which this fleet
does NOT start) and `explorer`/`annotator` have no `/api` proxy at all — they reach
`:8101`/`:8102`/`:8103` through their own BFF. See `rask-frontend`.

## Eleven HTTP entrypoints are thin shells over one of three factories

`compute`, `controlplane`, `flows` and `notifications` import routers (+ maybe a
lifespan) from a domain package and call `service_kit.make_service_app` — no business logic in
the entrypoint. Count by CALL SITE, not by import: `gateway` names the factory in three comments
and calls it nowhere. `gateway` builds `FastAPI(...)` itself (it is a proxy, not a router host),
though it does call `service_kit.setup_otel` directly, so it is on the shared telemetry path
even while it is off the shared app-factory path.

THE OTHER SEVEN ALSO COME OUT OF A FACTORY NOW, and this paragraph used to say they did not —
that they "build `FastAPI(...)` in `main.py`/`service.py`/`producer.py`/`stage_runner.py`". They did, all
seven, each repeating the same five-step boot in copied comments (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12), until
the stage runner was found to have lost its request-id layer in the copying. Today: `viewer`, `search` and
`annotator` build through `service_kit.media.app.build_media_app` over one shared
`service_kit.media.lifespan`; `catalog`, `lineage`, the two `medallion` apps and `maintenance` build
through `service_kit.lance_app.build_lance_service_app`. Both still read their own
`core/config.py` settings and keep the `api/v1/endpoints/` layout — the factory owns the boot, not
the layout. See rask-architecture's "Three factories are sanctioned".

| Service | Port | Composes package(s) | Lifespan | DB? | Notes |
|---|---|---|---|---|---|
| `gateway` | 8888 | none (httpx proxy) | own asynccontextmanager | no | path-routes `/api/*` longest-prefix-first, **no catch-all**; upstreams env-overridable (`RASK_COMPUTE_URL`, `RASK_CONTROLPLANE_URL`, `RASK_CATALOG_API_URL`, `RASK_LINEAGE_API_URL`, `RASK_MEDALLION_API_URL`, `RASK_EXPLORER_{VIEWER,SEARCH,ANNOTATOR}_URL`). There is **no** `RASK_MEDIA_*_URL` — the media→explorer rename took those names with it. |
| `compute` | 8804 | `ray-kit` | `compute.lifespan.make_lifespan` | no | Ray dashboard introspection; `proxy_router` mounts at root so `/api/serve/*` reaches Serve status API |
| `viewer` (explorer plane) | 8101 | `service-kit[lancekit]` + `storage` | own (lazy registry) | no | `/api/explorer/*` incl. the S3 objects browser ported from volumes-api |

## The batches/orchestrator plane is gone (P7a)

The reconcile→derive→submit loop, the two-lane prefetch/compute slot model, the
`batches` table and S3-sync were deleted at the compute-plane cutover
(lance-ns-merge.md P7a). The lakehouse runs no ingest of its own (ADR 0154): data enters only
through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
`DeclareTable` + vended credentials, `RegisterTable`), and ingest of an external source is a
bring-your-own client job (a sealed runner, CP-061) that commits through the catalog. It is the
CATALOG's own COMPLETE event that announces a write and that `/bronze-arrival` filters on: a
write to `<project>-bronze$events` publishes `medallion.bronze` with the catalog-vended location
on the trigger. `/publication-arrival` does the same for the catalog's `table_published` control
event, routed by `MEDALLION_TRANSFORM_ROUTES`; the media lane starts when its bronze table is
published through the catalog. The producer's only write door is `POST /train`, root-mounted and
token-guarded; a protocol-specific ingest door would make that protocol privileged. Its router
surface beside it is `promotions` (the quality gate's third answer), `stage_runner_ops`
(`/stage-runners/*`, show and terminate of a planned stage run), `rerun`
(`POST /stage-runners/stages/rerun`, the cascade's edge-addressed repair verb, 2026-09-04),
`cascade_lag_read` (`GET /cascade/stalled`) and `train_outcomes` (a training job's report).
`stage_runner_ops` and `rerun` share one prefix, `stage_runner_ops.STAGE_RUNNERS_PREFIX`, which the
gateway's `/api/stage-runners` row forwards to. Those are human control or a job's report, not
ingest. A workload's stages run as event-triggered stage runners on the unified Ray cluster
(P7b) — the cascade is modality-blind, so this is the same shape for every runner.

**Every bronze lane converges on one topic, so stage runners must discriminate.** A write to the
configured `bronze$events` and a write to a table a lane DECLARES as its input both end in a
catalog COMPLETE that `/bronze-arrival` turns into `medallion.bronze`, so every stage runner
subscribed to it sees every arrival. The trigger carries the lane that was actually written
(`ingest_trigger._bronze_write_dataset`) and `handle_stage` DROPs a name that is not its own
`from_dataset` — compared against the RAW setting, never the project-qualified one, since the
trigger is unqualified for every tenant. An ABSENT `dataset` makes no claim and proceeds. Without
that check one lane's arrival drives another lane's stage runner to completion: a real write plus a
COMPLETE attributed to the other lane's token.

## Why the fleet services never grow heavy deps

`compute` depends only on `service-kit` + `ray-kit` — **no DB**. `service-kit`'s
core stays dependency-light (no `ray`/`sqlmodel`; lance deps live behind the
`[lancekit]` extra the explorer plane opts into): pulling a heavy dep into the
core would force it onto every service and every test that imports the factory.

## Lifespan injection recap

`make_service_app(lifespan=...)` takes a `LifespanFactory =
Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]`.
Omit it → `default_lifespan` (puts `settings` on `app.state`, nothing else).
Stateful services pass a factory that opens Lance/S3/Ray clients on enter and
tears down on exit.
