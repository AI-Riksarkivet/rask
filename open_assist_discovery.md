# open-assist-discovery — the Serve-native producer registry

Working design doc, **2026-08-09** — same convention as the other `open_*` files: this file is
deleted (or graduates to `docs/`, the owner's call) when the design fully lands, which here
means a REAL model deployed on the cluster and discovered end-to-end. Implemented and tested
against the mock/config path; the live-cluster half is unexercised. The ruling this design
serves, verbatim from the owner: *"endpoints of
models will and always be models by Ray Serve from our ray-cluster, and the discovery based on
models for labeling."* Grounded in the Ray Serve REST API reference, the KubeRay RayService
guide and the RayService HA guide (read in full against Ray 2.56.1 / KubeRay ≥ 1.6), and in
the installed `ray.serve.schema` source. Implementation:
`services/annotator/src/annotator/api/v1/endpoints/serve_discovery.py` (+ `assist.py` merge),
config in `service_kit/media/config.py`, chart wiring in `chart/templates/explorer.yaml`,
tests in `tests/unit/test_serve_discovery.py`.

## The design in one paragraph

The assist producer registry's primary source is the Ray Serve **control plane**: the
annotator reads `GET /api/serve/applications/` on the Ray dashboard (`ServeInstanceDetails`)
and offers every **RUNNING** application whose deployment carries a **`labeling` block in its
`user_config`**. Deploying a model **is** registering it; there is no annotator-side
configuration per model. The env registry (`MEDIA_ASSIST_BACKENDS`) remains as the operator
override — *config is intent, discovery is observation* — and an unreachable control plane
degrades to config + the honest in-repo mock, never an error.

## The declaration convention

```yaml
# In the RayService CR's serveConfigV2 (or any Serve deployment config):
deployments:
  - name: Segmenter
    user_config:
      labeling:
        producer: sam          # optional — deployment name, lowercased, is the default
        returns: [polygon]     # canonicalised like a response (rectangle → bbox)
        inputs: [points, region]
```

`user_config` is chosen deliberately: Serve passes it to the deployment's `reconfigure()` and
it is **updatable in place, without replica restarts** (verified in the RayService guide's
mango-price walkthrough) — so a wrong declared contract is a `kubectl apply`, not a redeploy.
The declaration drives everything downstream with no further wiring: the assist panel's
contract line (`inputs → returns`), task-compatibility computation, and routing. Requests to
the discovered backend carry the task ontology's `output_schema` (see the generation-schema
seam in `annotator/projects/generation_schema.py`) for vLLM `guided_json` decoding; responses
are validated and task-filtered on the way back.

## Topology and URLs (KubeRay)

Two stable Services per RayService, **both surviving zero-downtime upgrades** (KubeRay
repoints their selectors to the new cluster):

| Service | Port | Role |
| --- | --- | --- |
| `<rayservice>-head-svc` | 8265 | Dashboard = the control plane discovery READS |
| `<rayservice>-serve-svc` | 8000 | Serve HTTP ingress = where producer CALLS go |

The serve-svc load-balances across **every pod holding an HTTP proxy** (the head always runs
one; workers only when they hold Serve replicas — the HA guide's endpoint math). The head's
own `:8000` reaches a single proxy, which is why the proxy URL is pinned explicitly rather
than derived in-cluster. Config: `MEDIA_SERVE_DISCOVERY_URL` (dashboard base) +
`MEDIA_SERVE_PROXY_URL` (ingress base; unset ⇒ derived from the dashboard host + the port
Serve reports in its own `http_options`). The chart wires both by the estate's established
precedence: external `ray.dashboardUrl` wins (rask's Ray is managed outside this repo — and
the external dashboard host serves the apps too, verified live 2026-08-06); the in-cluster
pair only under `singleTenant`; neither ⇒ discovery off, yesterday's behaviour exactly.

## Semantics worth not re-deriving

- **App-level `RUNNING` is the whole health check** — Serve documents RUNNING as "all
  deployments are healthy", so no per-deployment filter is needed (and none should be added).
- **`PUT /api/serve/applications/` REPLACES** ("removes all applications not listed"), and
  KubeRay's controller is itself a client of that API — so ONE writer owns the Serve config
  (under KubeRay: the RayService CR's `serveConfigV2`). An ad-hoc `serve deploy` against the
  cluster would delete `/transcribe` + `/htrflow`. Discovery only ever reads.
- **Autoscaler-managed fields are ignored on the CR**: RayService edits to `replicas` /
  `minReplicas` / `maxReplicas` / `workersToDelete` do not propagate at all.
- **TTL cache 15 s** (`DISCOVERY_TTL_S`): discovery rides the listing and assist request
  paths; the answer changes on deploy cadence, not request cadence.
- **No `ray` import in the service**: the response is walked structurally, so additive schema
  evolution across Ray upgrades cannot break parsing. Ray upgrades should still re-run
  `tests/unit/test_serve_discovery.py` — the REST API carries **no documented stability
  guarantee**.

## Production posture for labeling models (operator guidance, not chart-encoded)

From the HA guide, for the externally managed cluster when real labeling models deploy:
GCS fault tolerance (Redis-backed), `num-cpus: "0"` on the head's `rayStartParams` so no
Serve replicas schedule on the head, and `max_replicas_per_node: 1` to spread replicas —
then a head-pod death costs at most in-flight requests through the head's own proxy.
Deliberately not encoded in this repo's chart: its RayService is the single-tenant dev path.

## The vLLM path

`ray.serve.llm.build_openai_app` (+ `LLMConfig`) is the documented way to stand up an
OpenAI-compatible vLLM app on Serve — deploy it with a `labeling` block and it is discovered,
contract-declared, and receives `output_schema` for constrained decoding with zero
annotator-side changes. `serve.multiplexed` + `LoraConfig` are the future shape for per-task
fine-tuned adapters (many adapters, one deployment, selected per request) and slot into this
convention unchanged.
