# -*- mode: Python -*-
# rask dev loop on k3s. `tilt up` builds the shared fleet image and HOT-RELOADS the FastAPI services
# on source change: Tilt syncs the changed file into the running pod and uvicorn --reload restarts
# the worker in ~1s, instead of a full image rebuild + re-import + rollout.
#
# It deploys the APP DEPLOYMENTS ONLY. The platform — Dapr, NATS, CloudNativePG/AGE, OpenFGA, Dex,
# RustFS, OpenBao, KubeRay, every CRD and every helm hook — belongs to `make k3s-up`, which must
# already have run. That split is deliberate and load-bearing; see the deploy block below for why
# taking it over cost this Tiltfile its live_update for months.
#
# Prereqs: `make k3s-up` (cluster + platform) and `make tilt-registry` (once).
# Then: `make tilt-up`      (inspect with `make k9s`, or the Tilt UI at http://localhost:10350)
# PROVE it works: `make tilt-verify` — not optional, see the comment in that script.

# Re-run the release when the CHART changes, not just the Tiltfile. helm_resource does not
# watch its own chart directory, so editing a template or values.yaml left Tilt happily serving
# the previous release with no indication anything was stale — chart edits appeared to do
# nothing. `tilt alpha tiltfile-result` lists the watched ConfigFiles if this needs checking.
watch_file('chart')

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

# NOTE: the dapr/nats/openfga `helm_repo` resources were removed with `helm_resource` (2026-07-29).
# They existed so that extension could resolve the SUBCHARTS. Tilt no longer deploys subcharts at all
# — `make k3s-up` does — and rendering only `templates/*.yaml` reads the already-vendored
# `chart/charts/`, so re-adding the repos here would just be three resources that do nothing.

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
# ---- The other two Python images -------------------------------------------------------------
# gateway and controlplane ship their OWN images (`gateway:dev`, `controlplane:dev`), so the shared
# lance-rest-catalog build above does not cover them. They were outside Tilt's loop entirely: it
# rebuilt neither, so they sat at whatever `make k3s-build && make k3s-import` last put on the node
# while every other service moved — the quiet way a cluster ends up mixing versions.
#
# Their prod dockerfiles `uv sync --no-editable` into /opt/venv, exactly like the catalog image, so
# syncing into site-packages works and no dev-only image is needed. (`.docker/fleet.dev.dockerfile`
# was written for this and is referenced by nothing — it also still `COPY components components`, a
# directory deleted in the src-layout rewrite, so it cannot build. Left alone here; deleting it is a
# separate call.)
for svc in ['gateway', 'controlplane']:
    docker_build(
        svc + ':dev', '.',
        dockerfile='.docker/' + svc + '.dockerfile',
        only=['.docker', 'pyproject.toml', 'uv.lock', 'packages', 'services'],
        live_update=[
            sync('services/' + svc + '/src/' + svc, SITE + '/' + svc),
            sync('packages/service-kit/src/service_kit', SITE + '/service_kit'),
        ],
    )

# ---- The seven micro-frontend zones ------------------------------------------------------------
# These were `frontend.enabled=false` and out of the loop, so `/` on the ingress 404'd under Tilt and
# no zone could be exercised against real in-cluster backends — which is the ONE thing
# `make dev-frontends` cannot do (auth, FGA, Dapr, the gateway's own routing).
#
# A zone's prod image runs `bun build/index.js`, so syncing `src/` alone changes nothing: the server
# serves the COMPILED build/. It does ship src/ and node_modules though, so the build can be re-run
# in-container — sync, rebuild, restart. That is seconds rather than Vite's sub-second HMR, and it is
# the honest trade for running against the real cluster. For pure UI work `make dev-frontends` is
# still the faster loop; this is for when the backend is the point.
load('ext://restart_process', 'docker_build_with_restart')

# Kept in step with `Makefile:ZONES` and `frontend.apps`. R15: a zone missing from one list and
# present in another is exactly how a zone silently stops being deployed.
ZONES = ['home', 'lakehouse', 'media', 'annotator', 'compute', 'studio', 'train']

for zone in ZONES:
    docker_build_with_restart(
        'web-' + zone + ':dev', '.',
        dockerfile='.docker/frontend.dockerfile',
        build_args={'APP': zone},
        # The whole frontend workspace: bun's `--frozen-lockfile` fails with "Workspace not found" if
        # any member is absent, so this cannot be narrowed to one zone's directory.
        only=['.docker', 'frontend'],
        entrypoint=['bun', 'build/index.js'],
        # NOT the default /tmp/.restart-proc. `docker_build_with_restart` bakes that file into the
        # image and its entr-based wrapper stats it at startup — but the chart mounts an EMPTY
        # emptyDir over /tmp (the writable scratch that makes readOnlyRootFilesystem feasible), which
        # masks the image's /tmp entirely and takes the file with it. entr then exits immediately
        # with "unable to stat '/tmp/.restart-proc'" and every zone CrashLoopBackOffs — which is
        # exactly what happened to all seven on 2026-07-29 the moment they were enabled.
        # /app/app — the symlinked app dir, which IS chowned to 10001. Not /tmp (the chart mounts an
        # empty emptyDir over it, masking the baked-in file, so entr exits with "unable to stat" and
        # every zone CrashLoopBackOffs — which is what happened to all seven on 2026-07-29), and not
        # /app either: that directory is created by WORKDIR as root, so the `touch` the extension
        # injects fails the BUILD with exit 1 as UID 10001.
        restart_file='/app/app/.restart-proc',
        live_update=[
            sync('frontend/microfrontends/' + zone + '/src', '/app/app/src'),
            # Shared packages: an edit to @rask/ui or @rask/api must reach every zone that renders it,
            # or the one place a change is most likely to be wrong is the one place it is invisible.
            sync('frontend/packages', '/app/packages'),
            run('bun run build', trigger=['frontend/microfrontends/' + zone + '/src', 'frontend/packages']),
        ],
    )

# ---- Deploy: ONLY the Python fleet, natively -------------------------------------------------
#
# This used `helm_resource` until 2026-07-29, and that was the reason live_update never worked once.
# `helm_resource` shells out to `helm upgrade` behind `k8s_custom_deploy`, so **Tilt never owns the
# Kubernetes objects** — which is exactly what `tilt get liveupdates` reported: the LiveUpdate object
# existed with correct sync paths and discovered containers, every `lastFileTimeSynced` was `null`,
# and `kubernetesapplys/rask` said `live-update: False`. Tilt knew what it would sync and had no
# owned container to sync into. Nine config blockers were found and fixed against that setup and none
# of them were the cause.
#
# The stated justification for `helm_resource` — "post-install hooks + subchart CRDs are honored,
# unlike bare helm() which only templates" — does not survive checking:
#
#   * Hooks: THREE templates use them (kueue-queues, greptimedb-ttl-job, bootstrap-admin), all behind
#     toggles that are off or irrelevant to the Python fleet loop.
#   * CRDs: already applied. `make tilt-up` requires `make k3s-up` first, and THAT is the step that
#     installs the umbrella chart, its subcharts, the operators and their CRDs.
#
# So Tilt was redundantly redeploying the entire platform — Dapr, CloudNativePG, KubeRay, RustFS,
# OpenFGA — to iterate on a handful of FastAPI services, and doing too much is what cost it the one
# feature it exists for. It also caused the documented two-owner landmine: Tilt and `k3s-up` both
# claiming the `rask` release, where a hand-run `helm upgrade` silently evicts Tilt's injected image.
#
# Now: `k3s-up` owns the PLATFORM, Tilt owns the APP DEPLOYMENTS. Tilt applies them itself, so it can
# associate the image it built with the container running it, which is the precondition for a sync.
#
# `helm()` (Tilt's builtin) cannot do `-s`, so this shells out to `helm template` and hands Tilt the
# rendered objects. Ten of the eleven rendered Deployments run `lance-rest-catalog:dev` — the image
# built above — so live_update covers catalog, lineage, the medallion movers, compaction and the
# media trio. `rask-gateway` runs `gateway:dev`, which Tilt does not build, so it deploys but does
# not hot-reload.
FLEET_TEMPLATES = [
    'templates/services.yaml',    # catalog, lineage
    'templates/medallion.yaml',   # the producer + the three movers
    'templates/media.yaml',       # viewer, search, annotator  (needs media.enabled)
    'templates/compaction.yaml',  # compaction
    'templates/fleet.yaml',       # gateway
    'templates/controlplane.yaml',
    'templates/frontends.yaml',   # the seven zones
    # The INGRESS, and it is not optional once the zones are here. It was left to `k3s-up` at first,
    # and that release had frontend.enabled=false — so the live ingress carried only `/api` and `/`,
    # both to the gateway, and NO zone rules. Every zone pod was healthy and a browser still got 404
    # on /lakehouse/, because nothing routed there. Whoever owns the zones must own the routes to
    # them, or the two halves disagree and the symptom looks like a broken app.
    'templates/ingress.yaml',
]

k8s_yaml(local(
    ['helm', 'template', 'rask', 'chart']
    + [arg for t in FLEET_TEMPLATES for arg in ('-s', t)]
    + [
        # Without this the synced files land in the pod and uvicorn never re-reads them —
        # live_update looks like it works and changes nothing.
        '--set', 'dev.reload=true',
        # The media trio defaults OFF in the chart, so the annotator/viewer/search had no pods at
        # all under Tilt. They are the services most worth iterating on, so turn them on here.
        '--set', 'media.enabled=true',
        # The zones ARE in the loop now (see the docker_build_with_restart block above), so this is
        # on. dev.reload also relaxes their read-only rootfs — without that every zone sync fails
        # with "Read-only file system" and silently changes nothing.
        '--set', 'frontend.enabled=true',
    ],
    quiet=True,
    # `helm template` needs chart/charts/ vendored. `make k3s-up` depends on `k3s-deps`, which runs
    # `helm dependency build`, and k3s-up is a prerequisite of tilt-up — so by the time this runs the
    # dependencies are present. If they are not, helm fails loudly here rather than half-deploying.
))

# Group the fleet under one label in the UI, and give the hot-reloadable ones their own group so it
# is obvious at a glance which resources a source edit actually moves.
for name in ['rask-catalog', 'rask-lineage', 'rask-compaction',
             'rask-viewer', 'rask-search', 'rask-annotator',
             'rask-bronze-to-silver', 'rask-silver-to-gold', 'rask-media-to-silver',
             'rask-lance-ray', 'rask-gateway', 'rask-controlplane']:
    k8s_resource(name, labels=['fleet'])

for zone in ZONES:
    k8s_resource('rask-web-' + zone, labels=['zones'])

# No host ports — port-forward manually once up (see chart NOTES / DEPLOY.md).
#
# These named `svc/lance-ns-web` and `svc/lance-ns-lineage` until 2026-07-29. NEITHER EXISTS: the
# release is `rask`, so every service renders as `rask-*`, and `lance-ns-web` was ONE service where
# there are now SEVEN zone services. Both commands failed with `services "lance-ns-web" not found`,
# which reads as a broken cluster rather than a stale comment.
#
#   kubectl port-forward svc/rask-gateway 8888:8888   # /api/* — the one you usually want
#   kubectl port-forward svc/rask-lineage 8000:8000
#   kubectl port-forward svc/rask-catalog 2333:2333
#
# The zones are NOT deployed here (`frontend.enabled=false` above), so there is nothing to forward
# for the UI and `/` on the ingress 404s under Tilt — use `make dev-frontends` (Vite HMR) instead.
# With `--set frontend.enabled=true` each zone is its own service on :3000:
#   kubectl port-forward svc/rask-web-home 5273:3000  # …-lakehouse | -media | -annotator |
#                                                     # -compute | -studio | -train
#
# The media trio is gated behind `media.enabled` (chart default FALSE, and Tilt does not override
# it), so rask-viewer/-search/-annotator have NO PODS unless you pass `--set media.enabled=true`:
#   kubectl port-forward svc/rask-viewer    8101:8101
#   kubectl port-forward svc/rask-search    8102:8102
#   kubectl port-forward svc/rask-annotator 8103:8103
