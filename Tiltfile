# -*- mode: Python -*-
# lance-ns dev loop on kind. One `tilt up` builds the app images, deploys the umbrella Helm chart
# (every component — catalog, lineage, web, Dapr, NATS, Apache-AGE Postgres, OpenFGA, Dex, RustFS,
# OpenBao), and HOT-RELOADS the FastAPI services on source change (Tilt syncs the file, uvicorn
# --reload restarts the worker in ~1s instead of a full rebuild).
#
# Prereqs: a kind cluster (`kind create cluster --config deploy/kind/kind-config.yaml`) + helm.
# Then: tilt up   (inspect with k9s, or the Tilt UI at http://localhost:10350)

load('ext://helm_resource', 'helm_resource', 'helm_repo')

# Subchart repos (helm_resource resolves dapr/nats/openfga from chart/charts/, vendored via
# `helm dependency build ./chart` — these keep them refreshable).
helm_repo('dapr-repo', 'https://dapr.github.io/helm-charts/', labels=['infra'])
helm_repo('nats-repo', 'https://nats-io.github.io/k8s/helm/charts/', labels=['infra'])
helm_repo('openfga-repo', 'https://openfga.github.io/helm-charts', labels=['infra'])

# Build the catalog/lineage image (shared) + the web image. `only=` keeps the build context tight so
# unrelated edits don't trigger rebuilds; live_update syncs source for uvicorn --reload.
docker_build(
    'lance-rest-catalog', '.',
    dockerfile='.docker/rest-catalog.dockerfile',
    only=['.docker', 'pyproject.toml', 'uv.lock', 'services'],
    live_update=[
        # All services + common live under services/; the image copies them to /srv/services. Sync the
        # whole tree so an edit to any service hot-reloads (uvicorn --reload) without a full rebuild.
        sync('services', '/srv/services'),
    ],
)
# Deploy the umbrella chart via real helm (post-install hooks + subchart CRDs honored — unlike Tilt's
# bare helm() which only templates). Tilt injects the freshly built images into the chart's per-image
# repository/tag values and side-loads them into kind.
helm_resource(
    'lance-ns',
    'chart',
    # The P5 micro-frontend zones (frontend.enabled, default on) build from the parametrized
    # frontend.dockerfile — NOT wired into Tilt's dev loop yet, so disable them here or `tilt ci` would wait
    # on 5 never-built zone images. Tilt runs the BACKEND (catalog/lineage/…) for the dev loop; drive the
    # zones with `make frontend-images && make frontend-load` + a `helm upgrade --set frontend.enabled=true`
    # (see docs/DEPLOY.md). Zone-in-Tilt live_update is a follow-up.
    flags=['--timeout=300s', '--set', 'frontend.enabled=false'],
    image_deps=['lance-rest-catalog'],
    image_keys=[
        ('image.catalog.repository', 'image.catalog.tag'),
    ],
    resource_deps=['dapr-repo', 'nats-repo', 'openfga-repo'],
    labels=['lance-ns'],
)

# kind has no host ports — port-forward manually once up (see chart NOTES / DEPLOY.md):
#   kubectl port-forward svc/lance-ns-web 5173:3000
#   kubectl port-forward svc/lance-ns-lineage 8000:8000
