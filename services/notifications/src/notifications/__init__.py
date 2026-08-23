"""notifications — the estate's targeted inbox.

A thirteenth fleet member on `:8850`, app-id `notifications`, because an inbox belongs to none of its
producers: it aggregates runs from lineage and governance from the catalog, and both would-be hosts
pin authorization and state cohesion as per-domain. The badge it feeds counts YOUR work — one
`InboxActor` per subject holding claim-check pointers with durable read state — rather than the
estate-wide activity projection the bell renders today.

Fleet layout (`make_service_app` + flat modules + an injectable lifespan), like `compute` and `flows`.
The public namespace is `/api/notifications`; the gateway forwards that row unrewritten because this
app mounts its routers under `RASK_API_PREFIX` itself.
"""

import os

from fastapi import APIRouter

from notifications import health

# The HTTP door and the bus ingress, which `notifications.api` owns. Two names, and they are the whole
# contract: `routers`, mounted under the api prefix, and `register_subscriptions(app)`, called after
# `app` exists — a `DaprApp` subscription cannot be declared before there is an app to hang it on.
from notifications.api import register_subscriptions, routers
from notifications.api.reconcile_cron import router as reconcile_router
from notifications.lifespan import actor_plane_ready, build_actor_host, make_lifespan
from service_kit import make_service_app
from service_kit.probes import make_probes_router


# The operational probes are ROOT-mounted: a kubelet does not know this service's api prefix.
# `proxy_router` is `make_service_app`'s one root-mount slot (compute already uses it for its cron
# binding, which is delivered to POST /<name> at the root for the same reason).
_root = APIRouter()
_root.include_router(make_probes_router(actor_plane_ready))
# The reconciler's cron binding joins them: Dapr delivers an input binding to POST /<component name>
# at the root, never under the api prefix, so it belongs in this slot rather than in `routers`.
_root.include_router(reconcile_router)

# Keep the probes out of the trace stream: a kubelet polling twice a second is otherwise the loudest
# span in the service and carries no information. `setup_otel` (called by `make_service_app` below)
# takes no `excluded_urls` argument, so the lever is the instrumentation's own env var — read when
# `opentelemetry.instrumentation.fastapi` is first imported, which `setup_otel` does lazily and only
# when telemetry is on, i.e. after this line. `setdefault`, so a deployment can still widen or clear it —
# and in-cluster it always does. `rask.otelEnv` sets "/livez,/readyz,/metrics" on every fleet pod, so this
# line only ever takes effect in a bare local run, and the two values DIFFER in both form and content.
# Neither difference is a bug, and both were checked: the instrumentation matches each entry as a regex
# searched against the url, so "livez" and "/livez" exclude the same path; the chart adds `/metrics`
# (harmless here, nothing local scrapes it) and omits `/health`, which this service really does serve.
# Left as-is rather than unified: `test_probe_wiring.py` pins this set as a MEMBERSHIP so a deployment
# may widen it, and rewriting the value to look like the chart's would buy nothing but churn.
os.environ.setdefault("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "livez,readyz,health")

app = make_service_app(title="notifications", routers=[health.router, *routers], proxy_router=_root, lifespan=make_lifespan)

#: The actor plane's own health, defined HERE and not only in the lifespan so it is never merely
#: absent: a mount that fails below never reaches registration, and a flag that exists only on the
#: happy path cannot be the thing a route gates on. False until the lifespan proves otherwise.
app.state.actors_registered = False
app.state.actor_ext = build_actor_host(app)

register_subscriptions(app)
