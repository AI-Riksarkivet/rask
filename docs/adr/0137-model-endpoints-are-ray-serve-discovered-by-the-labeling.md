# 0137. Model endpoints are Ray Serve, discovered by the `labeling` user_config (2026-08-09)

Source: `open_assist_discovery.md` (repo root at `ac158590`; its live-cluster exit criterion is PARK-ANNO-1, #473; working design of 2026-08-09). The ruling, verbatim from the owner:
*"endpoints of models will and always be models by Ray Serve from our ray-cluster, and the discovery based on models
for labeling."* Grounded in the Ray Serve REST API reference and the KubeRay RayService and RayService HA guides, read
against Ray 2.56.1 / KubeRay ≥ 1.6.

## Context

The annotator's assist panel and the bulk grid's recipe columns both need a list of models they may call. Ray Serve
is shared platform infrastructure: one cluster hosts apps that have nothing to do with annotation (a batch scorer, an
embedding endpoint, a workload's own model service), and an app's name, route prefix and replica shape are the
deployer's free choice, not a contract.

## Decision

- **Every model endpoint is a Ray Serve application.** There is no second model plane: detectors, segmenters, HTR and
  LLM/VLM recipes are all Serve apps, resolved by producer NAME. vLLM is one kind of Serve app
  (`ray.serve.llm.build_openai_app`), not an alternative to it.
- **Discovery reads the Serve control plane.** The annotator reads `GET /api/serve/applications/` on the Ray dashboard
  and offers every **RUNNING** application whose deployment carries a **`labeling` block in its `user_config`**
  (`services/annotator/src/annotator/api/v1/endpoints/serve_discovery.py:125`). Deploying a model IS registering it.
- **The label is a purpose discriminator.** `user_config.labeling` present means the app is an annotation backend;
  absent means it is not ours and is never offered. Each consuming plane claims its own apps under its own key; there
  is no shared `purpose:` enum. The label is not authorization (the FGA gate on the task stays) and not health (it is
  ANDed with Serve's `RUNNING`).
- **Who configures what:** model authors declare by deploying (`user_config` is hot-updatable without a replica
  restart); operators pin two URLs once (`MEDIA_SERVE_DISCOVERY_URL`, the dashboard, and `MEDIA_SERVE_PROXY_URL`, the
  serve ingress); users configure nothing topological. `MEDIA_ASSIST_BACKENDS` stays as the operator override —
  config is intent, discovery is observation, and a name declared in both is won by config.

## Consequences

- Discovery only ever reads: `PUT /api/serve/applications/` replaces the whole set and KubeRay's controller is itself
  a client of that API, so one writer (the RayService's `serveConfigV2`) owns the Serve config.
- The answer is cached for 15 s (`DISCOVERY_TTL_S`, `serve_discovery.py:57`); an unreachable dashboard degrades to
  config plus the in-repo mock within one window, never an error. The response is walked structurally with no `ray`
  import, because the REST API carries no documented stability guarantee; `tests/unit/test_serve_discovery.py`
  should be re-run on a Ray upgrade.
- The chart wires the discovery URL by precedence: an external `ray.dashboardUrl` wins, else the in-cluster head
  service (`chart/templates/explorer.yaml:252,255`); the setting is `serve_discovery_url` in
  `packages/service-kit/src/service_kit/media/config.py:226`.
- When the design was written the live-cluster half was unexercised: no real labelling model had been deployed and
  discovered end to end. Whether one has since was not checked here.
