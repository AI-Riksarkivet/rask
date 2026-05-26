# Running FastAPI on Kubernetes

What you actually need to deploy a FastAPI service to k8s. The lifespan handles graceful shutdown — this file is about wiring k8s to use it correctly.

> **One-line rule**: don't install your own SIGTERM handler. uvicorn already does, and it triggers your lifespan's post-`yield` cleanup. Everything else here is just plumbing around that fact.

## Contents

- Shutdown sequence — what k8s actually does
- Full Deployment YAML — probes, resources, lifecycle, preStop
- Service
- PodDisruptionBudget
- HorizontalPodAutoscaler — RPS over CPU
- What NOT to do
- The math behind `terminationGracePeriodSeconds`
- Cross-references

## Process model — one worker per pod, never gunicorn

The single most important k8s + FastAPI rule:

- **Use plain `uvicorn` / `fastapi run` with no `--workers` flag.** One process per container.
- **Never put gunicorn in front** of uvicorn on k8s.
- **Scale with `replicas:`** in the Deployment, not in-process workers.

```dockerfile
# Dockerfile — correct
CMD ["fastapi", "run", "--host", "0.0.0.0", "--port", "8000", "app/main.py"]
# Equivalent: uvicorn app.main:app --host 0.0.0.0 --port 8000

# WRONG — multiplies pools and lifespan runs per pod
# CMD ["fastapi", "run", "--workers", "4", ...]
# CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "app.main:app"]
```

**Why** (each is enough on its own):

- Lifespan runs **once per process**. `--workers 4` → 4 DB pools, 4 HTTP clients, 4 model loads, 4× connection cap per pod.
- The kube scheduler / HPA can't see process-internal workers, so autoscaling decisions are based on per-pod metrics that lie about real load.
- A crashed worker inside the pod is invisible to k8s; a crashed pod is visible. Let the orchestrator do its job.
- Rolling updates, `terminationGracePeriodSeconds`, and `preStop` only work at pod granularity.

**When `--workers N` IS appropriate:** bare metal / single VM with no orchestrator. Not your case.

## Shutdown sequence — what k8s actually does

```
1. kubectl delete / rolling update / HPA scale-down
       │
2. Pod marked "Terminating", removed from Service endpoints
       │   (load balancer takes a few seconds to notice)
       │
3. preStop hook runs in the container
       │   ── this is your "grace window" before SIGTERM
       │
4. SIGTERM sent to PID 1 (uvicorn)
       │   ── uvicorn stops accepting new connections, finishes in-flight,
       │      then triggers lifespan post-`yield` cleanup
       │
5. terminationGracePeriodSeconds counts down
       │
6. SIGKILL if still alive
```

The 503-during-shutdown trick people add in app code is **redundant** — once the pod is "Terminating", the Service endpoints controller removes it, and new traffic stops arriving. The only real problem is the **gap between step 2 and step 4**: traffic in flight when the LB hasn't yet updated. That's what `preStop` solves.

## Full Deployment YAML

```yaml
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: viewer
  labels: { app: viewer }
spec:
  replicas: 3
  selector:
    matchLabels: { app: viewer }
  template:
    metadata:
      labels: { app: viewer }
    spec:
      # Budget: preStop (5s) + in-flight requests (≤20s) + lifespan cleanup (≤5s) + buffer.
      terminationGracePeriodSeconds: 40

      containers:
        - name: api
          image: registry.example.com/viewer:1.2.3
          imagePullPolicy: IfNotPresent
          ports:
            - { name: http, containerPort: 8000 }

          # ENV — see otel skill + per-domain BaseSettings.
          env:
            - { name: OTEL_SERVICE_NAME, value: viewer }
            - { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: http://otel-collector:4317 }
            - { name: OTEL_METRICS_EXPORTER, value: otlp }
            - { name: OTEL_PYTHON_FASTAPI_EXCLUDED_URLS, value: "/livez,/readyz,/metrics" }

          # Resources — set both. Without requests, scheduler over-packs.
          # Without limits, one pod can starve siblings on the node.
          resources:
            requests: { cpu: "250m", memory: "256Mi" }
            limits:   { cpu: "1",    memory: "512Mi" }

          # Liveness — am I alive? No deps.
          livenessProbe:
            httpGet: { path: /livez, port: http }
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 3       # 30s of failures → restart

          # Readiness — can I serve? Checks deps.
          readinessProbe:
            httpGet: { path: /readyz, port: http }
            initialDelaySeconds: 5
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 3       # 15s of failures → out of rotation
            successThreshold: 1

          # Startup — only if lifespan startup is slow (model loading, schema warmup).
          # Defers the liveness probe so a slow start isn't mistaken for a crash.
          startupProbe:
            httpGet: { path: /livez, port: http }
            initialDelaySeconds: 5
            periodSeconds: 5
            failureThreshold: 30      # tolerate 150s of startup

          lifecycle:
            preStop:
              exec:
                # Let the Service endpoints controller deregister us before SIGTERM.
                command: ["sh", "-c", "sleep 5"]
```

## Service

```yaml
apiVersion: v1
kind: Service
metadata: { name: viewer }
spec:
  selector: { app: viewer }
  ports:
    - { name: http, port: 80, targetPort: http }
```

## PodDisruptionBudget — almost free, often skipped

Protects against losing all replicas during a node drain or cluster upgrade.

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: viewer }
spec:
  minAvailable: 2          # or maxUnavailable: 1
  selector:
    matchLabels: { app: viewer }
```

Pick `minAvailable: N-1` for stateful tolerance, `maxUnavailable: 1` for fast rolling updates.

## HorizontalPodAutoscaler

For request-driven APIs, **CPU is a lagging signal** — by the time CPU is high, latency is already bad. If you have RPS or p95 latency exported via OTel, autoscale on that. CPU is a fallback.

```yaml
# hpa-cpu-fallback.yaml — only when you don't have request metrics yet
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: viewer }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: viewer }
  minReplicas: 3
  maxReplicas: 12
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 70 }
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300   # don't churn — 5 min cooldown
```

When you have a Prometheus adapter or Keda + OTel metrics, switch to:

```yaml
metrics:
  - type: Pods
    pods:
      metric: { name: http_server_request_duration_seconds_p95 }
      target: { type: AverageValue, averageValue: "200m" }   # 200ms p95
```

## What NOT to do

| Mistake                                                                   | Why                                                                                          |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Install your own `signal.signal(SIGTERM, ...)` handler in the FastAPI app | Races with uvicorn's handler. Either no-ops, or breaks lifespan cleanup. Let uvicorn own it. |
| Add a shutdown middleware that returns 503                                | Redundant with `/readyz` removal + preStop sleep. Extra branch on every request.             |
| `--workers N` inside the pod                                              | Multiplies pools / lifespan runs. Use `replicas:` (already covered in production-patterns).  |
| Long synchronous warm-up in lifespan startup without a startupProbe       | k8s declares the pod dead before it's done loading. Use `startupProbe` for slow boots.       |
| Skip `resources.requests`                                                 | Scheduler over-packs the node, then OOM-kills you under load.                                |
| `terminationGracePeriodSeconds: 30` with a 60s timeout endpoint           | SIGKILL mid-request, dropped responses. The grace must exceed your slowest expected request. |
| Liveness probe that hits the DB                                           | When the DB blips, k8s restarts every replica, blocking recovery. Liveness = process only.   |
| HPA on CPU for an I/O-bound API                                           | CPU stays low even as latency spikes. Use RPS or p95 latency once you have OTel metrics.     |
| Catching `SIGINT` in your code to "intercept Ctrl+C"                      | uvicorn handles it. Adding your own handler breaks `fastapi dev`'s reload too.               |
| Mixing `--reload` with production                                         | `fastapi dev` only. Production runs `fastapi run` (or `uvicorn`).                            |

## The math behind `terminationGracePeriodSeconds`

```
terminationGracePeriodSeconds  ≥  preStop sleep
                                + max in-flight request duration
                                + lifespan cleanup time
                                + buffer (5–10s)
```

Worked example for the viewer service:

| Component               | Time   |
| ----------------------- | -----: |
| preStop sleep           | 5s     |
| Slowest endpoint p99    | 20s    |
| Lifespan cleanup        | 5s     |
| Buffer                  | 10s    |
| **`terminationGracePeriodSeconds`** | **40s** |

If your slowest endpoint is much longer (e.g. a streaming export), either bump the grace period or move the long work to a background worker (see `python-infrastructure`).

## Cross-references

- **Lifespan + health endpoints**: [`production-patterns.md`](production-patterns.md) — what your FastAPI code needs to expose.
- **Tracing / metrics setup**: [`observability.md`](observability.md) + the `otel` skill.
- **Background work that survives shutdown**: `python-infrastructure` skill — NATS JetStream (`background-jobs.md`) for fanout/work-queue, Dapr Workflow (`dapr-workflows.md`) for multi-step sagas.
- **Dapr sidecar deployment** (`dapr.io/*` annotations, components.yaml, sidecar resource overhead): [`microservices.md`](microservices.md) § Dapr + Kubernetes interplay.
