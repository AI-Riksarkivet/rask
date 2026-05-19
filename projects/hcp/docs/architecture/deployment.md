# Deployment

## Containerization

The project uses [Dagger](https://dagger.io/) for reproducible container builds and CI/CD pipelines (see `dagger.json` and `.dagger/`). A `docker-compose.yml` is also provided for local multi-service development:

```bash
docker compose -f .docker/docker-compose.yml up
```

This starts the backend, frontend, and Redis together with health checks and automatic service linking.

## Mock Server

For development without an HCP system, the backend includes a mock server:

```mermaid
graph LR
    subgraph "Mock Server"
        DISP["mapi_state.py<br/>Request dispatcher"]
        FIX["fixtures.py<br/>Seed data"]
        STATE["In-memory state<br/>Dict-based storage"]
    end

    API3["FastAPI"] -->|"same interface as<br/>MapiService"| DISP
    FIX -->|"initial data"| STATE
    DISP -->|"CRUD operations"| STATE
```

The mock server implements the same interface as the real MAPI service, allowing the frontend to be developed and tested independently. Start it with `make run-api-mock`.

## Publishing Container Images

The project uses a Dagger pipeline (`.dagger/publish.go`) to build and push images to Docker Hub. Three Make targets are available:

```bash
# Publish both backend and frontend
make publish TAG=v0.1.0

# Publish individually
make publish-backend TAG=v0.1.0
make publish-frontend TAG=v0.1.0
```

Credentials are read from `.env`:

| Variable | Description |
|----------|-------------|
| `DOCKER_USERNAME` | Docker Hub username |
| `DOCKER_PASSWORD` | Docker Hub password or access token |

Published images:

| Image | Default repository |
|-------|--------------------|
| Backend | `riksarkivet/ra-hcp` |
| Frontend | `riksarkivet/ra-hcp-frontend` |

## Helm Chart

A Helm chart is provided in `charts/helm-ra-hcp-v0.1.0/` for Kubernetes deployment. Install with:

```bash
helm install ra-hcp charts/helm-ra-hcp-v0.1.0 \
  --set env.HCP_DOMAIN=hcp.example.com \
  --set secret.API_SECRET_KEY=your-secret-key
```

Key configuration values (see `charts/helm-ra-hcp-v0.1.0/values.yaml` for the full reference):

| Value | Default | Description |
|-------|---------|-------------|
| `image.repository` | `riksarkivet/ra-hcp` | Backend image |
| `image.tag` | `""` (uses `appVersion`) | Image tag |
| `backend.workers` | `1` | Gunicorn worker processes per pod |
| `replicaCount` | `2` | Number of backend pods |
| `service.type` | `NodePort` | Backend service type |
| `service.port` | `8000` | Backend service port |
| `service.nodePort` | `30081` | Backend NodePort |
| `frontend.enabled` | `false` | Enable frontend deployment |
| `frontend.service.nodePort` | `30517` | Frontend NodePort |
| `redis.enabled` | `false` | Enable Redis sidecar |
| `opentelemetry.enabled` | `false` | Enable OTEL export |

Enable the frontend and Redis:

```bash
helm install ra-hcp charts/helm-ra-hcp-v0.1.0 \
  --set frontend.enabled=true \
  --set redis.enabled=true \
  --set env.HCP_DOMAIN=hcp.example.com
```

## Production Architecture

A production deployment consists of a frontend, a backend, an optional Redis cache, and the HCP system it manages. A load balancer or ingress controller sits in front and routes traffic.

```mermaid
graph TB
    USERS["Users"]

    subgraph "Kubernetes Cluster"
        ING["Ingress / Load Balancer"]

        subgraph "Frontend Pods"
            FE1["Frontend #1<br/>SvelteKit + Deno"]
            FE2["Frontend #2<br/>SvelteKit + Deno"]
        end

        subgraph "Backend Pods"
            BE1["Backend #1<br/>FastAPI + gunicorn"]
            BE2["Backend #2<br/>FastAPI + gunicorn"]
            BE3["Backend #3<br/>FastAPI + gunicorn"]
        end

        REDIS["Redis<br/>Shared cache"]
    end

    HCP["HCP System<br/>MAPI :9090 + S3 :443"]

    USERS --> ING
    ING -->|"/ (UI traffic)"| FE1
    ING -->|"/ (UI traffic)"| FE2
    ING -->|"/api/* (API traffic)"| BE1
    ING -->|"/api/* (API traffic)"| BE2
    ING -->|"/api/* (API traffic)"| BE3

    FE1 & FE2 --> BE1 & BE2 & BE3
    BE1 & BE2 & BE3 <--> REDIS
    BE1 & BE2 & BE3 --> HCP
```

| Component | Technology | Port |
|-----------|-----------|------|
| Frontend | SvelteKit 2 + Svelte 5, Deno | 5173 (dev), 8000 (container) |
| Backend | FastAPI, Python 3.13+, uv | 8000 |
| Storage adapters | HcpStorage (aioboto3) — pluggable via StorageProtocol | — |
| Cache | Redis 7+ (optional) | 6379 |
| HCP MAPI | Hitachi Content Platform | 9090 |
| S3 endpoint | S3-compatible endpoint (HCP, MinIO, Ceph, AWS) | 443 |

### Scaling

There are two ways to scale the backend. They are independent and can be combined.

#### Vertical scaling — gunicorn workers (processes per pod)

Each backend pod runs gunicorn with uvicorn worker processes. Gunicorn's primary benefit is **reliability** — automatic worker restarts, memory leak protection, and graceful reloads — not speed. A single async uvicorn worker already handles hundreds of requests/second. The default is 1 worker per pod with 2 replicas — scaling is done via pod replicas so each gets independent liveness/readiness probes:

```
Pod (1 replica, 1 worker):
┌──────────────────────────────────────────────┐
│  gunicorn (master)                           │
│    └─ uvicorn worker 1  ─→ handles requests  │
└──────────────────────────────────────────────┘
```

Gunicorn manages the worker processes (restarts crashed workers, graceful reloads, pre-fork model). Uvicorn handles the async requests inside each worker. This is the [recommended production setup](https://www.uvicorn.org/deployment/#gunicorn) from both FastAPI and uvicorn.

The Dockerfile runs:

```dockerfile
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--max-requests", "10000", \
     "--max-requests-jitter", "1000", \
     "--timeout", "120", \
     "--keep-alive", "5", \
     "--access-logfile", "-"]
```

| Flag | Value | Why |
|------|-------|-----|
| `--workers` | 1 | One worker per pod — scale with replicas in Kubernetes (configurable via Helm) |
| `--max-requests` | 10000 | Recycle workers after 10K requests to prevent memory leaks |
| `--max-requests-jitter` | 1000 | Randomize recycling so workers don't restart simultaneously |
| `--timeout` | 120 | Kill workers that hang for 2 minutes (covers slow HCP responses) |
| `--keep-alive` | 5 | Keep idle HTTP connections open for 5 seconds (reduces handshake overhead) |
| `--access-logfile` | `-` | Log access requests to stdout (picked up by Kubernetes logging) |

The number of workers is configurable via the Helm chart:

```yaml
# values.yaml
backend:
  workers: 1  # 1 worker per pod — scale with replicaCount instead
```

The Helm deployment template passes this value to gunicorn's `--workers` flag.

!!! tip "Why 1 worker per pod?"
    In Kubernetes, each pod has independent liveness and readiness probes. If a pod loses HCP connectivity, Kubernetes removes it from the load balancer while healthy pods keep serving. With multiple workers inside one pod, an unhealthy worker is invisible to probes — the pod stays in rotation even if half its capacity is broken. One worker per pod gives Kubernetes full visibility.

    If you run the server **outside Kubernetes** (bare-metal, Docker Compose), increase `--workers` to `2 × CPU cores + 1` — there are no probes to leverage, so in-process scaling makes sense. Each worker uses ~50-100 MB RAM.

#### Horizontal scaling — replica count (pods)

Adding replicas creates multiple independent pods, each running their own set of workers. Kubernetes load-balances requests across them:

```
Default (2 replicas × 1 worker):              Scaled (4 replicas × 1 worker):
┌──────────────────────────┐                  ┌──────────────────────────────┐
│ Pod 1                    │                  │ Pod 1                        │
│  gunicorn → 1 worker     │                  │  gunicorn → 1 worker         │
├──────────────────────────┤                  ├──────────────────────────────┤
│ Pod 2                    │                  │ Pod 2                        │
│  gunicorn → 1 worker     │                  │  gunicorn → 1 worker         │
└──────────────────────────┘                  ├──────────────────────────────┤
= 2 processes total, each with                │ Pod 3                        │
  independent health probes                   │  gunicorn → 1 worker         │
                                              ├──────────────────────────────┤
                                              │ Pod 4                        │
                                              │  gunicorn → 1 worker         │
                                              └──────────────────────────────┘
                                              = 4 processes total
```

Both frontend and backend are **stateless** and can be horizontally scaled:

- **Frontend**: Each replica runs SvelteKit with SSR. No shared state — any request can go to any replica. Scale when you have many concurrent browser sessions.
- **Backend**: Each replica runs FastAPI. All replicas connect to the same Redis and HCP system. Scale when API throughput needs increase or HCP response times are high.
- **Redis**: Runs as a single instance. All backend replicas share it, so a cache fill from one replica is available to all others. For most deployments, a single Redis instance is sufficient.

```bash
# Scale backend to 5 replicas
kubectl scale deployment ra-hcp --replicas=5

# Scale frontend to 3 replicas
kubectl scale deployment ra-hcp-frontend --replicas=3
```

Or via the Helm chart:

```yaml
# values.yaml
replicaCount: 4           # 4 pods, each with independent probes
backend:
  workers: 1              # 1 worker per pod (default)
```

Autoscaling is also supported — set `autoscaling.enabled: true` to let Kubernetes scale replicas based on CPU utilization (see `values.yaml` for thresholds).

#### Which scaling approach to use?

!!! info "The default (2 replicas, 1 worker each) is enough for most use cases"
    A single async uvicorn worker handles hundreds of requests/second. The SDK sends ~2 presign requests/second during bulk transfers. More replicas help with **multi-user concurrency** and **fault tolerance**, not single-user transfer speed. Transfer speed is limited by network bandwidth, not the API server.

| Scenario | Recommendation |
|----------|---------------|
| 1-2 users doing bulk transfers | Default (`2 replicas, 1 worker`) — more than enough |
| Multiple concurrent users or API clients | Horizontal (`replicaCount: 4`) — each pod gets independent probes |
| High availability requirement | Horizontal (`replicaCount: 3+` with `podDisruptionBudget`) — survives node failures |
| Running outside Kubernetes | Increase `--workers` to `2 × CPU cores + 1` — no probes, so scale in-process |

!!! note "SDK `bulk_workers` vs server workers"
    The SDK's `bulk_workers` setting controls how many files are transferred in parallel on **your machine**. Server workers (gunicorn/replicas) control how many API requests the **backend** handles in parallel. These are completely different — SDK workers talk directly to HCP S3 for file data. The backend is only involved for presigning URLs (~2 requests/second during bulk transfers). See [Performance tuning](../sdk/cli.md#performance-tuning) in the SDK docs for details.

### Health Probes

The backend exposes health endpoints for Kubernetes liveness and readiness probes:

| Endpoint | Purpose | Checks |
|----------|---------|--------|
| `GET /liveness` | Liveness probe | Always returns 200 — the process is alive |
| `GET /readiness` | Readiness probe | Checks HCP MAPI reachability and Redis connectivity |
| `GET /health` | Legacy | Returns cache status |

The Helm chart configures these probes automatically. A backend pod that can't reach HCP is marked unready and removed from the load balancer until connectivity is restored.

## Environment Isolation

### One Stack Per HCP Domain

Each environment (development, acceptance, production) gets its **own isolated deployment**: its own frontend, backend, Redis, and HCP domain configuration. Environments never share components or cross-connect.

```mermaid
graph TB
    subgraph "Development (dev.hcp.example.com)"
        direction LR
        FE_D["Frontend<br/>1 replica"] --> BE_D["Backend<br/>1 replica"]
        BE_D --> RD_D["Redis"]
        BE_D --> HCP_D["HCP Dev"]
    end

    subgraph "Acceptance (acc.hcp.example.com)"
        direction LR
        FE_A["Frontend<br/>1 replica"] --> BE_A["Backend<br/>2 replicas"]
        BE_A --> RD_A["Redis"]
        BE_A --> HCP_A["HCP Acc"]
    end

    subgraph "Production (hcp.example.com)"
        direction LR
        FE_P["Frontend<br/>2 replicas"] --> BE_P["Backend<br/>5 replicas"]
        BE_P --> RD_P["Redis"]
        BE_P --> HCP_P["HCP Prod"]
    end

    style Development fill:#e8f4e8,stroke:#4a4
    style Acceptance fill:#fff3e0,stroke:#f90
    style Production fill:#fce4ec,stroke:#c33
```

The **1:1 relationship** between a deployment and an HCP domain is enforced by design: the `HCP_DOMAIN` environment variable is set at startup and determines which HCP system the backend communicates with. There is no runtime domain switching.

### Why Isolate?

| Concern | How isolation helps |
|---------|-------------------|
| **Data safety** | A dev frontend can never reach prod data — the backend only knows its configured domain |
| **Independent lifecycle** | Upgrade acceptance while production stays on the current version |
| **Independent scaling** | Production runs 5 backend replicas; development runs 1 |
| **Blast radius** | A misconfiguration in dev cannot affect prod |
| **Compliance** | Clear audit trail — each environment has its own logs, traces, and cache |

### Deploying Multiple Environments

Use separate Helm releases with environment-specific values files:

```bash
# Development — minimal resources, mock-friendly
helm install hcp-dev charts/helm-ra-hcp-v0.1.0 \
  -n hcp-dev --create-namespace \
  -f values-dev.yaml \
  --set env.HCP_DOMAIN=dev.hcp.example.com \
  --set secret.API_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))")

# Acceptance — moderate resources, SSL enabled
helm install hcp-acc charts/helm-ra-hcp-v0.1.0 \
  -n hcp-acc --create-namespace \
  -f values-acc.yaml \
  --set env.HCP_DOMAIN=acc.hcp.example.com \
  --set env.HCP_VERIFY_SSL=true \
  --set secret.API_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))")

# Production — full resources, all security features
helm install hcp-prod charts/helm-ra-hcp-v0.1.0 \
  -n hcp-prod --create-namespace \
  -f values-prod.yaml \
  --set env.HCP_DOMAIN=hcp.example.com \
  --set env.HCP_VERIFY_SSL=true \
  --set env.CORS_ORIGINS=https://hcp-ui.example.com \
  --set secret.API_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))")
```

!!! tip "Use Kubernetes namespaces"
    Deploy each environment to its own namespace (`hcp-dev`, `hcp-acc`, `hcp-prod`). This provides network isolation, independent RBAC, and clean resource accounting.

### Example: Production values file

```yaml
# values-prod.yaml
replicaCount: 5

backend:
  workers: 1  # 5 pods × 1 worker = 5 processes, each with independent probes

frontend:
  enabled: true
  replicaCount: 2

redis:
  enabled: true

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: hcp-ui.example.com
      paths:
        - path: /api
          pathType: Prefix
          backend: api
        - path: /
          pathType: Prefix
          backend: frontend
  tls:
    - secretName: hcp-tls
      hosts:
        - hcp-ui.example.com

env:
  HCP_DOMAIN: hcp.example.com
  HCP_VERIFY_SSL: "true"
  CORS_ORIGINS: "https://hcp-ui.example.com"

resources:
  requests:
    memory: "256Mi"
    cpu: "250m"
  limits:
    memory: "512Mi"
    cpu: "1000m"
```

## Security Hardening Checklist

Before going to production, verify these settings:

| Item | What to check | Risk if skipped |
|------|--------------|-----------------|
| `API_SECRET_KEY` | Set to a unique, random 64+ character value per environment | JWTs can be forged — full admin access |
| `HCP_VERIFY_SSL` | Set to `true` in production | Man-in-the-middle attacks on HCP communication |
| `CORS_ORIGINS` | Set to your specific frontend URL(s) | Cross-origin requests from malicious sites |
| Container security | Verify `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `drop: ALL` | Container escape or privilege escalation |
| Redis network | Redis should only be accessible from backend pods (ClusterIP service) | Cache data exposure |
| Kubernetes namespace | Each environment in its own namespace with network policies | Cross-environment access |
| Ingress TLS | Terminate TLS at the ingress with a valid certificate | Traffic interception |
| HCP credentials | Use dedicated service accounts, not personal admin accounts | Over-privileged access, no audit trail |

!!! warning "The default `API_SECRET_KEY` is `change-me-in-production`"
    This is intentionally insecure for local development. **You must change it** in any non-local deployment. Generate a secure key with:

    ```bash
    python -c "import secrets; print(secrets.token_urlsafe(64))"
    ```
