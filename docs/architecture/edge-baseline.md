# Edge routing baseline — captured 2026-08-03T11:41Z, BEFORE the kgateway migration

Phase 1 of `open_gateway.md` requires "identical routing". That is unprovable without a
before-picture, so this is it: every public path, through the edge that serves the cluster today.
Re-run the same table after the migration and diff. A status that was already wrong before the
change must not be mistaken for a regression caused by it.

```
PATH                               STATUS
/                                  200
/lakehouse/                        307
/compute/                          200
/media/                            200
/annotator/                        200
/studio/                           200
/train/                            200
/workbench/                        200
/api/catalog                       404
/api/lineage                       404
/api/ray                           404
/api/serve                         308
/api/projects                      503
/api/media                         404
/auth/login                        302
```

## The two premises `open_gateway.md` gets wrong for THIS cluster

1. **The local edge is Traefik, not ingress-nginx.** `chart/values.yaml:952` sets
   `className: ""` (k3s default Traefik) and the live Ingress reports
   `ingressClassName: traefik`. Only `values-prod.yaml:167` names nginx. So the migration is
   **Traefik → kgateway** locally and nginx → kgateway in prod; both must be considered.
2. **The live-stream timeout is a no-op locally.** `chart/values.yaml:979` sets
   `nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"`, and **Traefik ignores nginx
   annotations entirely**. So the 3600s that protects `query.live` streams is in force in
   production only — locally the streams survive on Traefik's own behaviour. "Preserve today's
   timeout" is therefore not a meaningful local bar; the bar is an absolute one (a stream stays
   connected >90s through the new edge).

## Already satisfied

**Gateway API CRDs are installed** (`gateways`, `httproutes`, `gatewayclasses`, … since
2026-07-27, shipped with k3s's Traefik). kgateway's prerequisite is met — and the toggle must
NOT install its own copies, or it will fight Traefik for CRDs it does not own.

## The `/api/*` statuses are NOT gateway routing gaps — read the bodies

Every `/api/*` row above answers with a **Dapr** error, not the gateway's own `no upstream`:

```
/api/catalog   ERR_DIRECT_INVOKE  "failed getting app id either from the URL path or the header dapr-app-id"
/api/ray       ERR_DIRECT_INVOKE  "failed to invoke, id: compute,      err: circuit breaker is open"
/api/media     ERR_DIRECT_INVOKE  "failed to invoke, id: viewer,       err: circuit breaker is open"
/api/projects  ERR_DIRECT_INVOKE  "failed to invoke, id: controlplane, err: circuit breaker is open"
```

Two distinct things, and neither is the edge:

- **`failed getting app id`** — the bare prefix carries no app-id, so Dapr cannot route it. Expected for a
  bare prefix; the real paths (`/api/catalog/v1/...`) carry one.
- **`circuit breaker is open`** — the Dapr resiliency `invokeBreaker` has LATCHED for `compute`, `viewer`
  and `controlplane`. A breaker that is open sheds every call without trying, and it stays open until its
  timeout elapses with a success. This is a PRE-EXISTING runtime state of the Dapr plane, unrelated to the
  edge — most likely latched during the churn of the tilt/live_update work (repeated pod restarts look
  exactly like upstream failure to a breaker).

**Why this matters for Phase 1:** re-running this table after the kgateway migration and seeing these same
statuses proves nothing about the edge either way, and seeing them CHANGE does not prove the edge caused
it. Before using this baseline as the parity check, either let the breakers close (they self-heal once the
pods are stable) or re-capture with a path that does not traverse Dapr. Comparing a latched-breaker
baseline against a healthy one would manufacture a phantom regression — or hide a real one.
