# -*- mode: Python -*-
# rask dev loop on k3s. One `tilt up` builds the app images, deploys the umbrella Helm chart
# (every component — catalog, lineage, web, Dapr, NATS, Apache-AGE Postgres, OpenFGA, Dex, RustFS,
# OpenBao), and HOT-RELOADS the FastAPI services on source change (Tilt syncs the file, uvicorn
# --reload restarts the worker in ~1s instead of a full rebuild).
#
# Prereqs: `make k3s-up` (the cluster + release) and `make tilt-registry` (once).
# Then: `make tilt-up`   (inspect with `make k9s`, or the Tilt UI at http://localhost:10350)

load('ext://helm_resource', 'helm_resource', 'helm_repo')

# Tilt refuses to deploy to a context it does not recognise as local ("might be production").
# It knows kind-*, k3d-*, minikube, docker-desktop and rancher-desktop by name, but a plain k3s
# install names its context 'default', so it gets blocked. This repo is not k3s-specific — the
# same chart runs on kind (make kind-deploy) and k3s (make k3s-up) — so allow the LOCAL context
# names generically, and let an operator name their own via TILT_ALLOW_CONTEXT rather than
# editing this file. Still an allow-LIST: allow_k8s_contexts(k8s_context()) would wave through
# a genuine production cluster.
LOCAL_CONTEXTS = ['default', 'kind-rask', 'kind-rask-dl', 'k3d-rask', 'minikube',
                  'docker-desktop', 'rancher-desktop']
allow_k8s_contexts(LOCAL_CONTEXTS + [c for c in [os.getenv('TILT_ALLOW_CONTEXT')] if c])

# k3s serves containerd, NOT the host docker daemon, so a locally-built image is invisible to it.
# `make tilt-registry` runs a registry on :5000 and points k3s' containerd at it over plaintext;
# this pushes there so the cluster can actually pull what Tilt builds.
default_registry('localhost:5000')

# Subchart repos (helm_resource resolves dapr/nats/openfga from chart/charts/, vendored via
# `helm dependency build ./chart` — these keep them refreshable).
helm_repo('dapr-repo', 'https://dapr.github.io/helm-charts/', labels=['infra'])
helm_repo('nats-repo', 'https://nats-io.github.io/k8s/helm/charts/', labels=['infra'])
helm_repo('openfga-repo', 'https://openfga.github.io/helm-charts', labels=['infra'])

# Build the catalog/lineage image (shared) + the web image. `only=` keeps the build context tight so
# unrelated edits don't trigger rebuilds; live_update syncs source for uvicorn --reload.
# Where the wheels land in the final image (see .docker/rest-catalog.dockerfile).
SITE = '/opt/venv/lib/python3.13/site-packages'

docker_build(
    'lance-rest-catalog', '.',
    dockerfile='.docker/rest-catalog.dockerfile',
    only=['.docker', 'pyproject.toml', 'uv.lock', 'packages', 'services'],
    live_update=[
        # The src-layout rewrite (2026-07-28) made this image install its members as WHEELS into
        # /opt/venv — there is no /srv/services any more, so the old `sync('services', '/srv/services')`
        # silently synced into a path that does not exist and hot reload never worked. Sync each
        # member's package root into site-packages, which is where uvicorn --reload actually watches.
        sync('services/catalog/src/catalog', SITE + '/catalog'),
        sync('services/lineage/src/lineage', SITE + '/lineage'),
        sync('services/medallion/src/medallion', SITE + '/medallion'),
        sync('services/compaction/src/compaction', SITE + '/compaction'),
        sync('services/viewer/src/viewer', SITE + '/viewer'),
        sync('services/search/src/search', SITE + '/search'),
        sync('services/annotator/src/annotator', SITE + '/annotator'),
        sync('packages/service-kit/src/service_kit', SITE + '/service_kit'),
        sync('packages/lineage-kit/src/lineage_kit', SITE + '/lineage_kit'),
    ],
)
# Deploy the umbrella chart via real helm (post-install hooks + subchart CRDs honored — unlike Tilt's
# bare helm() which only templates). Tilt injects the freshly built images into the chart's per-image
# repository/tag values and side-loads them into kind.
helm_resource(
    'rask',
    'chart',
    # The P5 micro-frontend zones (frontend.enabled, default on) build from the parametrized
    # frontend.dockerfile — NOT wired into Tilt's dev loop yet, so disable them here or `tilt ci` would wait
    # on 5 never-built zone images. Tilt runs the BACKEND (catalog/lineage/…) for the dev loop; drive the
    # zones with `make frontend-images && make frontend-load` + a `helm upgrade --set frontend.enabled=true`
    # (see docs/DEPLOY.md). Zone-in-Tilt live_update is a follow-up.
    # 300s was not enough for this chart and it failed in a way that COMPOUNDS: helm dies
    # with "context deadline exceeded" mid-upgrade, the release is left in pending-upgrade,
    # and every later `helm upgrade` — Tilt's or a human's — is refused until someone
    # rolls back by hand. Observed twice (rev 3 failed, rev 5 stuck 22min). Matched to
    # kind-deploy's 900s; k3s-up allows 20m.
    flags=['--timeout=900s', '--set', 'frontend.enabled=false',
           # Without this the synced files land in the pod and uvicorn never re-reads
           # them — live_update looks like it works and changes nothing.
           '--set', 'dev.reload=true'],
    image_deps=['lance-rest-catalog'],
    image_keys=[
        ('image.catalog.repository', 'image.catalog.tag'),
    ],
    resource_deps=['dapr-repo', 'nats-repo', 'openfga-repo'],
    labels=['rask'],
)

# kind has no host ports — port-forward manually once up (see chart NOTES / DEPLOY.md):
#   kubectl port-forward svc/lance-ns-web 5173:3000
#   kubectl port-forward svc/lance-ns-lineage 8000:8000
