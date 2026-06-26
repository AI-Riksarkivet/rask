# -*- mode: Python -*-
# rask dev loop on k3s — in-cluster HOT-RELOAD for the Python fleet.
#
# What this gives you: edit any .py under packages/ or components/ and the change
# is live in the running cluster in ~1s (uvicorn --reload picks up the file Tilt
# syncs), instead of the ~1-min rebuild+import+restart loop.
#
# Scope: the FastAPI fleet only. Frontends and Ray are NOT managed here on purpose —
#   * frontends iterate faster via the local dev server (Path A): port-forward the
#     gateway and run `make dev-frontends` (true Vite HMR). See chart/README / docs.
#   * the ray image is 8.5 GB — you don't want it in a tight rebuild loop.
# They keep running whatever images `make k3s-up` deployed.
#
# Prerequisites (one-time):
#   1. make k3s-up         # full real-helm deploy (infra, CRDs, hooks, frontends, ray)
#   2. make tilt-registry  # local registry + point k3s at it (sudo, restarts k3s)
#   3. tilt up   (or: make tilt-up)
#
# How it works: docker_build pushes editable dev images (.docker/fleet.dev.dockerfile)
# to the local registry; helm_resource re-deploys the chart with image.repository
# pointed at that registry and dev.reload=true (adds `uvicorn --reload`); live_update
# syncs source into the running pods. helm_resource runs REAL `helm upgrade --install`
# so the post-install hooks (GreptimeDB TTL, Kueue queues) and subchart CRDs are honored
# — unlike Tilt's bare helm() which only templates.

load('ext://helm_resource', 'helm_resource')

REGISTRY = 'localhost:5000'
FLEET = ['gateway', 'core-api', 'search-api', 'volumes-api', 'ray-api', 'orchestrator']

# Build an editable dev image per fleet service and hot-reload it on source change.
# `only=` keeps the build context tight so unrelated edits don't trigger rebuilds.
for svc in FLEET:
    docker_build(
        '%s/%s' % (REGISTRY, svc),
        '.',
        dockerfile='.docker/fleet.dev.dockerfile',
        build_args={'PACKAGE': svc},
        only=['.docker', 'pyproject.toml', 'uv.lock', 'packages', 'components'],
        live_update=[
            sync('packages', '/app/packages'),
            sync('components', '/app/components'),
            # No restart_container() needed: uvicorn --reload restarts the worker
            # itself when a watched file changes.
        ],
    )

# Deploy the umbrella chart via real helm (hooks + CRDs honored). image.repository
# redirects the fleet (and the migrate job, which shares it) to the registry; the
# frontends/ray keep their own side-loaded :dev refs from `make k3s-up`.
helm_resource(
    'rask',
    'chart',
    flags=[
        '--set', 'image.repository=%s' % REGISTRY,
        '--set', 'image.tag=dev',
        '--set', 'dev.reload=true',
        '--set', 'ray.enabled=true',
        '--set', 'dapr.sidecars=false',
        '--set-string', 'secrets.postgresPassword=raskpgpass1234',
        '--set-string', 'secrets.minioSecretKey=rasks3secretkey1234',
    ],
    image_deps=['%s/%s' % (REGISTRY, svc) for svc in FLEET],
)
