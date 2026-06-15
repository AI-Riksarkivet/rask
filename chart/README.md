# rask Helm chart

Deploys the rask application services — **viewer** (FastAPI, singleton) and
**frontend** (SPA) — plus an Alembic migration hook. Postgres, S3/MinIO and the
KubeRay cluster are external; this chart only references them.

## Prerequisites

1. Images `rask-viewer` and `rask-frontend` pushed to a registry your cluster can
   pull (no CI builds these yet — build from `.docker/*.dockerfile`).
2. A Secret with: `DATABASE_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `HCP_ENDPOINT`, `HF_TOKEN`.

   ```bash
   kubectl create secret generic rask-secrets \
     --from-literal=DATABASE_URL='postgresql+asyncpg://…' \
     --from-literal=AWS_ACCESS_KEY_ID=… \
     --from-literal=AWS_SECRET_ACCESS_KEY=… \
     --from-literal=HCP_ENDPOINT=… \
     --from-literal=HF_TOKEN=…
   ```

## Install

```bash
helm install rask chart/ \
  --set existingSecret=rask-secrets \
  --set viewer.image.repository=<registry>/rask-viewer \
  --set frontend.image.repository=<registry>/rask-frontend \
  --set config.RAY_DASHBOARD_URL=http://<ray-head>:8265 \
  --set ingress.host=rask.example.org
```

## Critical constraints

- **Never set `viewer.replicas > 1`** — the orchestrator is an in-process
  singleton; concurrent viewers double-submit jobs.
- The orchestrator stays idle until `config.RASK_ORCHESTRATOR_AUTOSTART=true` or
  an operator calls `POST /api/v1/orchestrator/start`.

See `docs/architecture/deployment.md` and the design spec
`docs/superpowers/specs/2026-06-15-rask-helm-chart-design.md`.
