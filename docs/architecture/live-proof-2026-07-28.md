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
| Values | `singleTenant.enabled=true`, `media.enabled=true`, `medallion.compute=true`, `auth.enabled=true`, `medallion.fgaEnabled=true`, `ray.auth.enabled=true`, `ray.gpuCount=0`, `RASK_SERVE_GPU_FRAC=0`, `rustfs.storageClass=standard` — the last four were **manual workarounds for chart defects**, and are the chart's own defaults (or derived) since the defect fixes below; the same install today needs only the first six. |
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
- **lakehouse** — the durable runs board prints both run_ids verbatim; `/lakehouse/catalog/storage` lists
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

Each is a real chart/app defect, not a kind quirk — recorded here as the proof's yield. **All seven are
now fixed**; the "Fix" line names the change and the guard that keeps it fixed. Every guard fails on the
pre-fix chart/code (each was checked against the original render).

1. `MEDALLION_IIIF_BASE_URL` has no values knob; the code default is RA-internal, so any external
   deploy needs `kubectl set env`.
   **Fix:** `medallion.producer.iiifBaseUrl` (+ `iiifQueryParams`) → `MEDALLION_IIIF_BASE_URL` /
   `MEDALLION_IIIF_QUERY_PARAMS` in `medallion.yaml`; the values comment names the PUBLIC endpoint
   (`https://lbiiif.riksarkivet.se`) beside the RA-internal default. The one knob also reaches the Ray
   harvest branch (`ray_submit` already forwards `IIIF_BASE_URL`).
   *Guard:* `test_invariants.py::test_the_iiif_ingest_head_takes_its_endpoint_from_values`.
2. `rustfs.storageClass: "local-path"` does not exist on stock kind (`standard`) → Tenant PVCs wedge
   `Pending` with no diagnostic; a StorageClass alias was needed to proceed.
   **Fix:** `rustfs.storageClass` defaults to `""` → the key is omitted → the **cluster's default**
   StorageClass provisions. Same defect at a second site nobody had noticed: `rayservice.yaml` hardcoded
   `storageClassName: local-path` on the HF-cache PVC, taking the Ray head down with it — now
   `ray.hfCacheStorageClass`, also `""`.
   *Guard:* `test_no_pvc_hardcodes_a_provisioner_specific_storage_class` — scans EVERY rendered doc, not
   just `kind: PersistentVolumeClaim`, because the Tenant volumes are a `volumeClaimTemplate` inside a CR.
3. The htrflow serve actor demands a full GPU, so a GPU-less cluster leaves the RayService
   permanently `Initializing`, never creates the stable head Service, and the compute zone reads
   "Ray offline" despite a healthy head.
   **Fix:** `ray.gpuCount` is now the chart's ONE GPU signal (default **0**) and everything GPU-shaped
   derives from it via `rask.gpuEnabled` / `rask.serveGpuFrac`: the Serve fraction (in BOTH the
   ConfigMap and `serveConfigV2`), the head's `num-gpus` + `nvidia.com/gpu` limit, `runtimeClassName`,
   the `RuntimeClass` object, and the Kueue `nvidia.com/gpu` quota. No manual override is needed for the
   CPU path; the incoherent GPU pairings fail the render (`gpu-coherence.yaml`).
   *Guard:* `test_a_gpuless_estate_renders_a_gpuless_ray_serve` (both directions).
4. media + annotator poll `/api/health`, for which the gateway has no upstream → a console 404 per
   page load.
   **Root cause, corrected:** the routing was fine — `/media/api/health` and `/annotator/api/health` both
   reach the viewer through each zone's BFF catch-all, and the gateway's `/api/media` row maps to the
   viewer's `/api` too. The 404 came from the **viewer**: `/api/health` resolved the default dataset
   before answering, so an un-loaded corpus made the plane's liveness probe 404
   (`dataset 'transcripts_v2' not found under /media-corpus`). That is the wrong layer to fix at the edge
   — a probe must distinguish "service down" from "no corpus".
   **Fix:** `/api/health` always 200s; `db` is `DbFacts | None` with `db_error` carrying the reason.
   Frontend follows: the valibot schema accepts `db: null`, the badge says "No dataset loaded" (warning,
   not destructive — destructive is reserved for unreachable), and the descriptor store / annotator
   picker stop dereferencing `db`. Dataset-bound endpoints still 404, correctly.
   *Guards:* `tests/unit/test_media_health_degrades.py` (4 tests, incl. one proving `/api/columns` on the
   same app still 404s) + `frontend/packages/media-api/src/health-no-dataset.test.ts`.
5. `media.enabled=true` requires `/var/media-corpus` to pre-exist on the node (hostPath type
   `Directory`) or the trio wedges `ContainerCreating` — the plan says no hostPath ships.
   **Fix:** `media.corpus.mode` ∈ `emptyDir` (default — works on an unprepared node) | `pvc` (prod;
   existing `claimName`, else a chart-created `<release>-media-corpus` with `helm.sh/resource-policy:
   keep`) | `hostPath` (opt-in, and now `DirectoryOrCreate` so it cannot wedge). A typo'd mode fails the
   render instead of silently serving an empty corpus. This is why defect 4's fix is a prerequisite: the
   default corpus is now empty, so the probe MUST degrade.
   *Guard:* `test_no_workload_mounts_a_hostpath_that_must_pre_exist`.
6. nvdp + gpu-feature-discovery DaemonSets render unconditionally on GPU-less clusters.
   **Fix:** `nvdp.enabled` defaults **false**. Helm resolves a subchart `condition:` against a static
   values path and cannot express `ray.gpuCount > 0`, so the pair is enforced instead: `nvdp.enabled=true`
   with `ray.gpuCount=0` **fails the render** with the fix in the message. The reverse pair is legitimate
   (an externally-managed device plugin) and warns in NOTES.
   *Guard:* `test_the_gpu_device_plugin_cannot_render_without_gpu_workloads`.
7. `services.lineage.reconcile` defaults OFF while the helm NOTES warns a lost event stays lost.
   **Decision: default it ON — and `services.lineage.outbox` with it.** Staging and draining are one
   mechanism (#4); half of it is worse than either half (reconcile alone back-fills only that a version
   exists — author/inputs/columnLineage are gone; the outbox alone stages events nothing drains). Safe
   because ingest MERGEs on `run_id` (idempotent), the sweep is single-flighted, the only destructive
   behaviour (`:Run` pruning) is separately gated by `runRetentionDays: 0`, staleness only WARNs, and the
   cron binding renders only with Dapr sidecars. The NOTES warning now fires ONLY when an operator broke
   the chain, and says which half and what is lost.
   *Guard:* `test_the_lineage_durability_chain_is_on_by_default` — which renders the real NOTES through a
   throwaway probe chart, because `helm template` omits NOTES.txt and `--dry-run` needs a live cluster.

Install-flow notes: `helm install --wait` deadlocks against the OpenFGA-migrate hook (the documented
`scripts/e2e_stack.sh` behavior) — revs 1–2 `failed` before rev 4+ succeeded; and the IIIF head is
off by default (`medallion.compute=false` → `/ingest-iiif` 409s), which is correct but worth knowing.

---

## Defects found by DRIVING THE UI (2026-07-28, owner-witnessed)

Every one of these was invisible to a green test suite and to the agent reports; each surfaced only
because the owner opened the running estate through an SSH tunnel and clicked. They are recorded here
because they arrived AFTER the goal's ten gates were written, and a defect that lives only in a chat
transcript does not exist. Ruling **R28** covers the storage-registry half; the rest are listed here.

| # | defect | evidence | status |
|---|---|---|---|
| 1 | **Storage has no nav entry.** `lakehouseNav()` is area-scoped, so `/lakehouse/catalog/storage` is reachable only by typing the URL; the topnav's Lakehouse panel lists Catalog/Models/Lineage (+admin) but never Storage. | read `lib/nav.ts`; the area map contains `storage` but no other area links to it | **R28** |
| 2 | **The tiers are not storage.** The browser's bucket set is a hardcoded `Literal["images-batch","images-batch-alto"]` — the import sink and export sink. The governed bronze/silver/gold Lance datasets (`lance-catalog/medallion/*`) cannot be selected, though the code comment claims "the warehouse buckets". | `services/viewer/.../objects.py` + `lib/storage/storage.ts` | **R28** |
| 3 | **Nothing declares what a sink IS.** No registry states which storage is an import sink, an export sink, a tier store or observability — so the UI must hardcode names and cannot ask. The catalog already owns warehouse roots (`_warehouses/*.json`) and is the natural authority. | grep: no role/kind field anywhere in the storage path | **R28** |
| 4 | **A fabricated identity is rendered when signed out.** `navbar-user.svelte` falls back to `{ name: 'rask', email: 'local', initials: 'RA' }`, and `'RA'` is also the `AvatarFallback` default. A governance UI must never invent a logged-in user — signed-out shows signed-out. | `frontend/packages/ui/src/lib/shell/navbar-user.svelte:30-51` | OPEN |
| 5 | **Half-governed deploys are possible and silent.** The live stack runs `auth.enabled=true` (backend governed) while the zones carry ZERO oidc/session env — so the home project picker 401s and shows "This stack is governed — sign in to browse projects" with no door to walk through. Dex IS deployed and working (a real `alice@example.com` token was minted against it). Nothing makes backend auth and `frontend.oidc.enabled` move together. | `kubectl get deploy rask-web-home -o jsonpath=…env[*].name` → no oidc/session vars | OPEN |
| 6 | **Home renders a phantom sidebar** whose only leaf is "Projects → /" — a link to the page you are already on. Home is the catch-all with no areas; the rail costs width and implies navigation that does not exist. | `home/src/lib/nav.ts` — `leaves: [{ title: 'Projects', href: '/' }]` | OPEN |
| 7 | **Every cross-zone hop pays a 308 redirect.** The topnav links to the bare base (`/annotator`, `/compute`, `/train`, `/studio`, `/media`) but each zone serves the trailing-slash form — measured 308 on all five. Over a tunnel this reads as flicker, on top of the (correct, by-design) hard navigation and PixiJS canvas init. **The gate gap:** zone-contract verifies cross-zone links hard-navigate, but nothing checks the href a nav points at is the one the zone actually serves — a href↔resolved-base test would have caught all five. | `curl` per zone; `nav-config.ts` hrefs | OPEN |
| 8 | ~~**Object BYTES 500 while listing succeeds.**~~ **RETRACTED — this was TOOLING, not the product.** The 500 came from `scripts/kind-browse.sh`: its gateway port-forward (9888) had died, so `/api/*` fetches threw inside the Bun proxy and returned its ~67 KB HTML error page. Verified by isolation: the viewer answers `/api/object` **200** with correct metadata directly, and the gateway answers `/api/media/object` **200** too. The real lesson is a tooling defect — a dead port-forward must fail LOUDLY, not masquerade as a backend 500 — now fixed in the script (readiness check + explicit 502 naming the dead upstream). |

**Staged test data now exists** for anyone continuing: 10 real Riksarkivet page JPEGs (332 KB – 1.09 MB)
at `s3://images-batch/A0060198/`, read back out of the bronze blob column in-cluster and written as
objects — which also proves `read_blobs` works on live data when mapped by `row_address` rather than
positional zip (the null-drop landmine in `lance-blob-v2-findings.md`).

## Install-deadlock: FIXED and proven from zero (2026-07-28)

`helm install --wait` died twice today on `context deadline exceeded`: the app pods could not become
Ready until the OpenFGA migration ran, and the migration was a **post-install hook**, which cannot run
until `--wait` finishes. `scripts/e2e_stack.sh` existed purely to sequence around it.

**The fix is to stop making it a hook.** A `pre-install` hook would run before AGE Postgres exists; in
the ordinary wave helm waits for the Job *and* the server at the same time, and the Job's own
`wait-age` init container supplies the only ordering that was ever really needed.

Proof — throwaway kind cluster `rask-dl`, default chart, `--set singleTenant.enabled=true`:

```
==> helm install --wait (THE deadlock test: migrate is an ordinary resource, not a post-install hook)
HELM INSTALL EXIT=0
```

41 pods Running; the hook Jobs that could never fire before all Complete, including
`rask-openfga-migrate` and `rask-openbao-seed`. Two earlier attempts failed for reasons that were
MINE, not the chart's, and are recorded here so nobody re-reads them as evidence: the first had no
third-party preload against a 12-minute timeout (cold pulls), and the second was killed mid-install
when a disk cleanup deleted its cluster. `make kind-preload` exists to remove the first cause.

**Caught in the same run — a Job FAILS and the install still reports success.** The job table from the
proving cluster:

```
rask-dapr-inject-sweep-r1   Failed     0/1   15m
rask-nats-stream-r1         Complete   1/1
rask-openbao-seed-r1        Complete   1/1
rask-openfga-migrate-r1     Complete   1/1
rask-rustfs-mkbucket-r1     Complete   1/1
```

`helm install --wait` returned **0** with `rask-dapr-inject-sweep-r1` in `Failed`. Whatever that sweep
does (it exists to re-trigger Dapr sidecar injection — the exact race that wedged the FIRST deploy of
this session, where the media trio came up 1/2 without sidecars), a green install that silently
tolerates its failure is a gate that cannot catch the problem it was written for. Not investigated —
the throwaway cluster was deleted before this was noticed. **Open.**

## k3s acceptance — the two things kind CANNOT prove (2026-07-28)

Both run against the box's own k3s (`dmlpai01`, v1.36.2+k3s1). Note for anyone repeating this:
`/etc/rancher/k3s/k3s.yaml` is world-readable, so DRIVING k3s needs no sudo — only
`make k3s-import` does (it writes k3s's root-owned containerd image store).

### GPU — the device plugin actually schedules onto a Blackwell

kind has no nvidia OCI runtime, which is why `rask-nvdp` sat in `ContainerCreating` for the whole
kind session. On k3s:

```
node allocatable : {'cpu': '64', 'memory': '527780680Ki', 'nvidia.com/gpu': '3'}
nvidia-device-plugin-q4qsm   1/1   Running

# a pod with runtimeClassName: nvidia and limits {nvidia.com/gpu: 1}
phase=Succeeded
0, NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition
```

### NetworkPolicy — default-deny actually BLOCKS

kindnet accepts NetworkPolicy objects and silently ignores them, so the chart's 12 policies
(default-deny, the exclusive OpenBao lock, the rustfs client list) are decorative on kind. k3s ships
kube-router, which enforces. Same probe pod, same target Service, one policy applied in between:

```
BEFORE policy      : HTTP 200
AFTER default-deny : HTTP 000
BLOCKED (connection never established)
```

An earlier attempt at this probe produced NO output and I nearly read the silence as proof — it was
`kubectl run --rm` swallowing the result. Silence is not evidence; the run above uses a persistent
probe pod and `exec` so both sides print.

## Gate 5 — a green install was hiding a permanently-broken Job

Witnessed on the real k3s cluster, not a throwaway: `helm status rask` reported **`deployed`**
while a bootstrap Job had never completed and never could.

```
JOB                         COMPLETIONS   FAILED   ACTIVE
rask-dapr-inject-sweep-r1   <none>        <none>   1        <- crash-looping since install
rask-nats-stream-r1         1             <none>   <none>
rask-openbao-seed-r1        1             <none>   <none>
rask-openfga-migrate-r1     1             <none>   <none>
rask-rustfs-mkbucket-r1     1             <none>   <none>

release status: deployed        <- helm's verdict, with the above underneath it
```

**Two independent defects, both required for the failure to stay invisible.**

### 1. The sweep could never start — distroless image, `sh` entrypoint

```
Error: failed to create containerd task: ... exec: "sh": executable file not found in $PATH
Back-off restarting failed container sweep
```

`registry.k8s.io/kubectl` is **distroless** — no `/bin/sh`. The sweep's script is `sh -c`, so the
container never started on any pass. It was not flaky and not a race; it had a 0% success rate from
the day it was written. `kueue-queues` uses the same distroless image *correctly*, via `args:`
straight to kubectl's entrypoint — which is why that one works and hid the pattern.

Fixed by moving the two shell-using kubectl Jobs to `alpine/k8s:1.31.3` (shell + kubectl, verified by
running it). Jobs that need a shell and Jobs that don't now differ by image, deliberately.

### 2. `helm --wait` does not wait for Jobs

From helm's own flag help:

> `--wait`: will wait until all **Pods, PVCs, Services, and minimum number of Pods of a Deployment,
> StatefulSet, or ReplicaSet** are in a ready state…
> `--wait-for-jobs`: if set **and `--wait` enabled**, will wait until all Jobs have been completed…

Jobs are absent from `--wait`'s list. Every bootstrap Job in this chart is an *ordinary-wave*
resource (they left the hook lifecycle in the deadlock fix), and ordinary-wave Jobs are exactly what
`--wait` ignores — so the deadlock fix, correct on its own terms, moved the Jobs into helm's blind
spot. `--wait-for-jobs` is now set on both install paths (`k3s-up`, `kind-deploy`).

Hook Jobs (`kueue-queues`, `greptimedb-ttl`, `bootstrap-admin`) were never affected — helm fails a
release when a hook Job fails. The gap was only ever the ordinary wave.

### 3. A second dead image, found by the same check

`bitnami/kubectl:1.31` — the default for the RustFS VolumeSnapshot Job — now **404s**
(`docker.io/bitnami/kubectl:1.31: not found`); Bitnami retired the public catalog. That Job also runs
`sh -c`, so it moved to `alpine/k8s:1.31.3` with the sweep. It had not been exercised, so nothing had
reported it.
