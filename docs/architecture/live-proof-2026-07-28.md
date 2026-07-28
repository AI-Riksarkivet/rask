# Live proof — the merged platform on kind (2026-07-28)

> **What this is.** The gate-8 acceptance record for the `feat/lance-ns-merge` goal: what was
> _observed running_ on a real cluster at commit `98ec728`, and what was NOT. Written for external
> re-verification — every claim below is a command output or a screenshot, not an inference.
> Rulings referenced here live in the table in [`lance-ns-merge.md`](lance-ns-merge.md).

## Environment

| | |
|---|---|
| Platform | kind cluster `rask` (kindest/node v1.36.1), context `kind-rask`, no sudo |
| Release | `rask`, helm rev 7 `deployed` |
| Values | `singleTenant.enabled=true`, `media.enabled=true`, `medallion.compute=true`, `auth.enabled=true`, `medallion.fgaEnabled=true`, `ray.auth.enabled=true`, `ray.gpuCount=0`, `RASK_SERVE_GPU_FRAC=0`, `rustfs.storageClass=standard` |
| Images | built from this tree, `kind load`ed (zones as `web-<zone>:dev` per R22's image-namespace fix) |
| Not present | GPU (nvdp + gpu-feature-discovery permanently `ContainerCreating` — no nvidia OCI runtime on kind); ingress controller (reproduced faithfully with per-zone port-forwards behind a `:3024` proxy carrying the Ingress path table) |

## PROVEN

### 1. IIIF → bronze, with lineage in Apache AGE

`POST /api/ingest-iiif` through the real gateway, volume `A0060198`, **`max_pages: 10`** (the API's
own cap — a theory test, not a harvest):

```
202 {"status":"ingested","token":"live-proof-a0060198-10p","dataset":"bronze$pages","pages":"10"}
```

Producer log: `No existing dataset at s3://lance-catalog/medallion/bronze-pages, it will be created`
→ 6× `POST /bronze-arrival 200`. Result: `bronze$pages` **version 1, `row_count: 10`**, real Lance
data on RustFS, schema `id:int64, payload:blob, source_uri, volume_id, page_key, …`.

Lineage, verbatim from AGE (`apache/age:release_PG16_1.5.0`, db `lineage`, graph `lineage`):

```cypher
LOAD 'age'; SET search_path = ag_catalog, public;
SELECT * FROM cypher('lineage', $$
  MATCH (r:Run {run_id: 'e8f93824-1f82-5667-8709-b4f8c6a96c49'}) RETURN r
$$) AS (run agtype);
```

```
{"label": "Run", "properties": {"job": "lance-medallion/iiif-ingest", "author": "ray",
 "run_id": "e8f93824-1f82-5667-8709-b4f8c6a96c49", "outputs": "bronze$pages",
 "operation": "iiif-ingest", "event_type": "COMPLETE",
 "event_time": "2026-07-28T06:58:05.410012+00:00", "events_count": 1, "error_message": ""}}::vertex
```

Graph labels/edges in play: `Run, Job, Dataset, Column` / `OF_JOB, READ, WROTE, DERIVED_FROM,
HAS_COLUMN`. The governed `/runs` API returns the same run (anonymous → `401 Missing bearer token`;
a real Dex id_token was minted via the password grant to read it).

**R23 confirmed in the artifact**: the output dataset is `bronze$pages` — bronze is the first
governed tier, the external `iiif://…` source is an input, and no raw table exists in the catalog.

### 2. Ray token auth — all four cases

| case | result |
|---|---|
| dashboard `GET /api/jobs/` **without** token | **401** `Unauthorized: Missing authentication token` |
| `ray job submit` **without** token | **`AuthenticationError: InvalidAuthToken`**, exit 1 |
| dashboard `GET /api/version` **with** Secret token | **200** `{"ray_version": "2.56.1", …}` |
| `ray job submit` **with** token | `Job 'raysubmit_TLuPSj1U2P66HzWQ' succeeded` |

Operator-injected pod env verified: `RAY_AUTH_MODE=token`, and pod `RAY_AUTH_TOKEN` **matches** the
`rask-ray-auth-token` Secret's `auth_token` key. Beyond the four: the **KubeRay 1.6.2 controller
authenticates its own `/api/serve` reconcile calls** with the same Secret — the RayService reached
`Ready=True` with the `htrflow` app RUNNING on CPU. This is exactly the coupling R3's commit
predicted (a pre-1.6.0 operator would have wedged at `WaitForServeDeploymentReady`).

### 3. All seven zones, live backend, no black pages

Screenshots in the session record (`live-*.png`). Highlights:

- **home** — the bell reads "Notifications, 2 unread" and the popover shows the **real** runs:
  `iiif-ingest · Completed` and `embed_features · Failed` with the exact Lance error. 0 console errors.
- **compute** — "Ray connected", NODES 1/1, SERVE APPS 1/1, the runs feed showing both runs, and
  RECENT RAYJOBS listing `SUCCEEDED raysubmit_TLuPSj1U2P66Hz` — the authed job from §2. 0 errors.
- **lakehouse** — the durable runs board prints both run_ids verbatim; `/lakehouse/storage` lists
  its buckets on the **media-plane** backend (the R6 re-home), honest-empty on this stack.
- **studio / train** — scaffolds render, 0 console errors (train's "Scaffold — not wired" label is
  the app's own honesty).
- **media / annotator** — render with an honest banner (`dataset 'transcripts_v2' not found`): no
  corpus staged on this node.

Documented degradations only (no-OIDC `capi/v1/me` 401s; empty corpus 404s). Zero black pages.

## NOT PROVEN (honest gaps)

1. **The cascade did not reach gold in the page lane.** `bronze$pages` arrival triggers the deployed
   **events-lane** mover (`MEDALLION_FROM_DATASET=bronze$events`), which FAILs deterministically —
   run `f42e0b35-…`, `Dataset at path medallion/bronze was not found`, retries exhausted → Dapr DLQ.
   The page-lane HTR movers are the **unlanded P7b runner re-cut** (the chart's own
   `movers[].stageJob` comment says so). The FAIL is itself correctly recorded in AGE as a `:Run`
   with `event_type FAIL` and the error string — the lineage plane behaved perfectly.
2. **No per-stage/per-actor child runs exist** — correct, because no compute stage ran. lineage-kit's
   job→stage→actor emission is unit-proven (including across a real subprocess boundary) but has not
   yet been observed in-cluster; that needs the P7b compute stage.
3. **GPU acceptance** is k3s-side by design; kind has no nvidia runtime.

## Defects the live run surfaced

Each is a real chart/app defect, not a kind quirk — recorded here as the proof's yield:

1. `MEDALLION_IIIF_BASE_URL` has no values knob; the code default is RA-internal, so any external
   deploy needs `kubectl set env`.
2. `rustfs.storageClass: "local-path"` does not exist on stock kind (`standard`) → Tenant PVCs wedge
   `Pending` with no diagnostic; a StorageClass alias was needed to proceed.
3. The htrflow serve actor demands a full GPU, so a GPU-less cluster leaves the RayService
   permanently `Initializing`, never creates the stable head Service, and the compute zone reads
   "Ray offline" despite a healthy head.
4. media + annotator poll `/api/health`, for which the gateway has no upstream → a console 404 per
   page load.
5. `media.enabled=true` requires `/var/media-corpus` to pre-exist on the node (hostPath type
   `Directory`) or the trio wedges `ContainerCreating` — the plan says no hostPath ships.
6. nvdp + gpu-feature-discovery DaemonSets render unconditionally on GPU-less clusters.
7. `services.lineage.reconcile` defaults OFF while the helm NOTES warns a lost event stays lost.

Install-flow notes: `helm install --wait` deadlocks against the OpenFGA-migrate hook (the documented
`scripts/e2e_stack.sh` behavior) — revs 1–2 `failed` before rev 4+ succeeded; and the IIIF head is
off by default (`medallion.compute=false` → `/ingest-iiif` 409s), which is correct but worth knowing.
