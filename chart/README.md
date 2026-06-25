# rask Helm chart

Deploys the full rask fleet to Kubernetes — the **single deploy artifact** for
both local k3s and production. In-cluster CloudNativePG (Postgres), RustFS
(object store), and KubeRay are optional: gate them with `*.enabled` toggles.

## Fleet

| Component | Port | Notes |
|---|---|---|
| `gateway` | 8888 | Reverse proxy; path-routes `/api/*` to per-domain services |
| `core-api` | 8801 | Batches, chunks, catalog endpoints |
| `orchestrator` | 8810 | Reconcile loop (`replicas: 1`, `Recreate`) |
| `volumes-api` | 8803 | S3/IIIF image + ALTO proxy (stateless) |
| `search-api` | 8802 | Lance FTS + S3 thumbnails |
| `ray-api` | 8804 | Ray dashboard proxy + `/api/serve/*` |
| `frontend` | 3000 | SvelteKit SSR (svelte-adapter-bun) |
| migration | — | pre-install/pre-upgrade Job: `alembic upgrade head` |
| Ingress (Traefik) | 80 | `/api` → gateway:8888, `/` → frontend:3000 |

## In-cluster dependencies (optional)

Each toggle gates **both** the operator subchart and the custom resource it manages.

| Toggle | Operator | What it provisions | Service |
|---|---|---|---|
| `cnpg.enabled=true` | CloudNativePG (`cloudnative-pg` 0.28.3) | `Cluster` named `rask-postgres` (instances, storage, image all under `cnpg.*`) | `rask-postgres-rw:5432` |
| `rustfs.enabled=true` | RustFS operator (vendored at `third_party/rustfs-operator/`, refreshed via `scripts/vendor-rustfs-operator.sh`) | `Tenant` named `rask-rustfs` — 1 pod / 4 PVCs (erasure-coding minimum); buckets provisioned natively via `spec.buckets` | `rask-rustfs-io:9000` (S3), `rask-rustfs-console:9001` (console) |
| `ray.enabled=true` | — | KubeRay `RayService` (head + GPU worker) | — |

Set all three to `true` for local k3s. Leave them `false` for production and
supply credentials via `existingSecret`.

## Local k3s quickstart

```bash
make k3s-install      # one-time: k3s + helm + NVIDIA device-plugin + KubeRay (sudo)
make k3s-build        # build fleet + frontend + ray images as :dev
make k3s-import       # side-load images into k3s (no registry needed)
make k3s-up           # helm upgrade --install rask ./chart --wait
# UI: http://rask.local/   API: http://rask.local/api/health
# (add "127.0.0.1 rask.local" to /etc/hosts)
make k3s-down         # uninstall   |   make k3s-purge  # + delete PVCs
```

## Production install

```bash
helm upgrade --install rask chart/ \
  --set existingSecret=rask-app \
  --set config.RAY_DASHBOARD_URL=http://<ray-head>:8265 \
  --set ingress.host=rask.example.org \
  --set cnpg.enabled=false \
  --set rustfs.enabled=false \
  --set ray.enabled=false
```

## Config and secrets

Sensitive config (database URL, S3 credentials, HF token) comes from an
operator-created Secret. The default expected name is `rask-app`; override with
`existingSecret=<name>`.

Non-sensitive config (service URLs, feature flags, orchestrator settings) flows
from `values.yaml` into a ConfigMap mounted by each Deployment.

## Critical constraints

- **`orchestrator` must be a singleton** (`replicas: 1`, `strategy: Recreate`)
  — the in-process reconcile loop must run in exactly one pod.
- The orchestrator loop starts only when `config.RASK_ORCHESTRATOR_AUTOSTART=true`
  or an operator calls `POST /api/v1/orchestrator/start`.

See `docs/architecture/deployment.md` for the full topology.
