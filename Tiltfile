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

# ---- WHO BUILDS THE IMAGES ---------------------------------------------------------------------
# Dagger is this repo's build system for the CI GATES (`dagger call test|lint|charts|openapi|frontend`,
# .dagger/*.go) but until 2026-07-29 it built no image at all — every artefact came from
# `docker buildx`, in the Makefile, in ci.yml and here. `.dagger/images.go` closes that: it hands the
# SAME `.docker/*.dockerfile` to BuildKit through Dagger, so the dockerfile stays the single source of
# truth and this is a change of driver, not a second build definition.
#
# Set RASK_TILT_BUILDER=docker to fall back to Tilt's native docker_build — worth knowing about,
# because the two do not share a build cache, so switching costs one cold rebuild each way.
BUILDER = os.getenv('RASK_TILT_BUILDER', 'dagger')
DAGGER_ENGINE = os.getenv('DAGGER_ENGINE_NAME', 'dagger-engine-rask')

# THE REGISTRY IS ADDRESSED TWICE, ON PURPOSE. Tilt and k3s pull via `localhost:5000`; Dagger pushes to
# `172.17.0.1:5000`. Same registry container — but Dagger's engine IS a container, so `localhost` inside
# it means the engine, and a push there fails looking exactly like a broken registry. 172.17.0.1 is the
# docker bridge gateway, i.e. the host as seen from a container.
DAGGER_REGISTRY = os.getenv('DAGGER_REGISTRY_HOST', '172.17.0.1:5000')

# Dagger speaks HTTPS to every registry and `publish` has no --insecure flag, so the plain-HTTP dev
# registry needs an engine that has been told it is http. `make dagger-engine` provisions exactly that;
# without it the stock auto-provisioned engine fails with "server gave HTTP response to HTTPS client".
def dagger_publish(fn, flags):
    """A custom_build command: build via Dagger, push to $EXPECTED_REF's registry."""
    return ' '.join([
        'ADDR=$(echo "$EXPECTED_REF" | sed "s|^localhost:5000|' + DAGGER_REGISTRY + '|");',
        '_EXPERIMENTAL_DAGGER_RUNNER_HOST=docker-container://' + DAGGER_ENGINE,
        'dagger call', fn, flags,
        'publish --address="$ADDR"',
    ])

def build_image(ref, dockerfile, context_deps, live_update, dagger_fn, dagger_flags, build_args = {}):
    """docker_build or custom_build(dagger), same live_update either way."""
    if BUILDER == 'dagger':
        custom_build(
            ref,
            dagger_publish(dagger_fn, dagger_flags),
            deps=context_deps,
            # Dagger pushed straight to the registry: the image is NOT in the local docker daemon, and
            # Tilt must not push it a second time.
            skips_local_docker=True,
            disable_push=True,
            live_update=live_update,
        )
    else:
        docker_build(
            ref, '.',
            dockerfile=dockerfile,
            only=context_deps,
            build_args=build_args,
            live_update=live_update,
        )

# NOTE: the dapr/nats/openfga `helm_repo` resources were removed with `helm_resource` (2026-07-29).
# They existed so that extension could resolve the SUBCHARTS. Tilt no longer deploys subcharts at all
# — `make k3s-up` does — and rendering only `templates/*.yaml` reads the already-vendored
# `chart/charts/`, so re-adding the repos here would just be three resources that do nothing.

# Build the catalog/lineage image (shared) + the web image. `only=` keeps the build context tight so
# unrelated edits don't trigger rebuilds; live_update syncs source for uvicorn --reload.
# Where the wheels land in the final image (see .docker/rest-catalog.dockerfile).
SITE = '/opt/venv/lib/python3.13/site-packages'

# THE reason live_update never landed a single file, found 2026-07-29 by reading Tilt's own build
# history rather than its config: the sync fired, and the container refused the write.
#
#   spanID "liveupdate:rask-catalog:lance-rest-catalog"
#   error  "Updating pod …: command terminated with exit code 2
#           This usually means the container filesystem denied access."
#
# The prod dockerfiles copy the venv as root and then `USER 10001`, so site-packages is root:root 0755
# and the account running the app cannot write into the very directory every sync targets. Tilt then
# does what it is designed to do — falls back to a full image build — which is why an edit produced a
# ~90 s rebuild and a new ReplicaSet instead of a reload. `dev.reload` had already cleared
# readOnlyRootFilesystem; ownership is a SECOND, independent gate, and nothing reports it as one.
#
# Passed only here, so shipped images keep an immutable venv. Every Python image below takes it.
VENV_OWNER = {'VENV_OWNER': '10001:10001'}

build_image(
    'lance-rest-catalog',
    '.docker/rest-catalog.dockerfile',
    ['.docker', 'pyproject.toml', 'uv.lock', 'packages', 'services'],
    dagger_fn='image',
    dagger_flags='--name=rest-catalog',
    build_args=VENV_OWNER,
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
# compute joins these two: it ships its own image and the gateway invokes it for /api/ray + /api/serve.
# It was absent because the chart never RENDERED it (fleet.yaml gated on a literal "gateway"), so Tilt
# had no k8s object to attach an image to and silently built nothing.
for svc in ['gateway', 'controlplane', 'compute']:
    build_image(
        svc + ':dev',
        '.docker/' + svc + '.dockerfile',
        ['.docker', 'pyproject.toml', 'uv.lock', 'packages', 'services'],
        dagger_fn='image',
        dagger_flags='--name=' + svc,
        build_args=VENV_OWNER,
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

# DERIVED from the chart, not hand-kept in step with it. The comment here used to say "kept in step
# with `Makefile:ZONES` and `frontend.apps`" and then listed seven zones by hand — and it had already
# drifted: `workbench` landed (b021499) as an eighth, the chart deployed it, Tilt did not build it, and
# `rask-web-workbench` sat in ImagePullBackOff running whatever `k3s-build` last pushed. That is the
# exact failure the old comment warned about, written directly above the list that caused it.
#
# The chart is what DEPLOYS the zones, so reading its list is the only version that cannot disagree with
# what is running. `read_yaml` is a Tilt builtin; a new zone joins the loop by existing.
ZONES = [app['name'] for app in read_yaml('chart/values.yaml')['frontend']['apps']]

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
    # The fleet CONFIGMAP, for the same reason the ingress and dex are here: this list renders the
    # services, and their configuration is not optional to them. Without it RAY_DASHBOARD_URL,
    # RASK_*_URL and the gateway's route targets come from whatever `make k3s-up` last applied, so
    # Tilt can deploy compute pointing at a Ray that moved. Whoever owns the pods owns their config.
    'templates/configmap.yaml',
    'templates/controlplane.yaml',
    'templates/frontends.yaml',   # the seven zones
    # The INGRESS, and it is not optional once the zones are here. It was left to `k3s-up` at first,
    # and that release had frontend.enabled=false — so the live ingress carried only `/api` and `/`,
    # both to the gateway, and NO zone rules. Every zone pod was healthy and a browser still got 404
    # on /lakehouse/, because nothing routed there. Whoever owns the zones must own the routes to
    # them, or the two halves disagree and the symptom looks like a broken app.
    'templates/ingress.yaml',
    # DEX, for exactly the same reason the ingress is here, one rung further in. This Tiltfile sets
    # auth.enabled=true and dex.issuer below — but `--set` only reaches templates that are RENDERED,
    # and without this line dex.yaml is not among them. `make k3s-up` then owns the Dex ConfigMap at
    # chart defaults, so the issuer stayed `http://rask-dex:5556/dex`: Dex advertised in-cluster URLs
    # in its discovery document, the BFF forwarded the browser to `http://rask-dex:5556/dex/auth`,
    # and login died on an unresolvable host — while every value here looked correct, because the
    # setting was applied to a template nobody rendered.
    #
    # Whoever turns auth ON must own the IdP that auth depends on.
    'templates/dex.yaml',
    # The WHOLE Dapr plane. Tilt sets auth.enabled, media.enabled and storage.stores, and every one
    # of those is expressed in a Dapr CR — components (pubsub/state/secrets), their SCOPES, the
    # resiliency policy and the sidecar-injection sweep. Rendering the pods without them left Helm
    # owning the config at ITS values, and the two disagreed silently three separate times in one
    # day: dex.issuer stayed in-cluster so login died on an unresolvable host; the resiliency CR
    # still believed media was off, so four invoked app-ids had no timeout/retry/breaker; and
    # lance-secrets was unscoped for viewer/search, so the object browser 500'd on the first
    # external store. Whoever renders the pods renders what configures them.
    'templates/dapr-component.yaml',
    'templates/dapr-statestore.yaml',
    'templates/dapr-resiliency.yaml',
    'templates/dapr-app-token.yaml',
]

# The origin the BROWSER uses to reach this cluster. Default assumes the documented SSH tunnel
# (`ssh -L 8080:127.0.0.1:80 …`); set RASK_PUBLIC_ORIGIN when reaching the host any other way.
# Raw is external (R23): the governed tiers are on the in-cluster warehouse, raw and its derived ALTO
# output are not. Override the host for another environment with RASK_HCP_S3.
HCP_S3 = os.getenv('RASK_HCP_S3', 'https://dev-ai.hcp.ra-dev.int')
# Starlark has NO implicit string concatenation (adjacent literals are a syntax error, unlike Python),
# so this is a real list encoded once rather than a hand-glued JSON string.
STORES = encode_json([
    {"name": "images-batch", "bucket": "images-batch", "role": "raw", "endpoint": HCP_S3,
     "insecure": True, "secret": "hcp-s3", "description": "Source page images, as harvested."},
    {"name": "images-batch-alto", "bucket": "images-batch-alto", "role": "derived", "endpoint": HCP_S3,
     "insecure": True, "secret": "hcp-s3", "description": "ALTO XML exported from the cascade."},
    {"name": "lance-catalog", "bucket": "lance-catalog", "role": "bronze",
     "description": "The lakehouse warehouse — the governed medallion datasets."},
    {"name": "rask-observability", "bucket": "rask-observability", "role": "observability",
     "description": "Telemetry retained by GreptimeDB."},
])

PUBLIC_ORIGIN = os.getenv('RASK_PUBLIC_ORIGIN', 'http://localhost:8080')
# 32+ chars, fixed so a tilt restart does not sign everyone out. Dev-only; see the --set below.
SESSION_SECRET = os.getenv('RASK_SESSION_SECRET', 'rask-tilt-dev-session-secret-0123456789')

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
        # The storage registry. Raw and derived live on the EXTERNAL store (a different host with
        # different credentials); only the governed tiers are on the warehouse this chart deploys.
        # Without this every store resolves to the warehouse, and a bucket holding millions of objects
        # lists as empty — no error, no 404. `secret` names the OpenBao key holding its access/secret
        # pair, because one process env cannot hold two backends' credentials.
        '--set-json', 'storage.stores=' + STORES,
        # The zones ARE in the loop now (see the docker_build_with_restart block above), so this is
        # on. dev.reload also relaxes their read-only rootfs — without that every zone sync fails
        # with "Read-only file system" and silently changes nothing.
        '--set', 'frontend.enabled=true',
        # Governance ON. The chart defaults auth.enabled=false ("open dev mode"), and nothing here
        # turned it on — so every zone rendered signed-out with no way IN: no sign-in control at all,
        # `/media/capi/v1/me` answering 401, and Dex running the whole time with nothing pointing at
        # it. Auth is the single most repo-specific thing the in-cluster loop exists to exercise
        # (FGA, the BFF, cross-zone sessions), and it is exactly what `make dev-frontends` cannot do,
        # so leaving it off made the slower loop pointless.
        #
        # auth.enabled ALSO renders the `/dex` ingress path (templates/ingress.yaml:92). Without it
        # Dex is ClusterIP-only and the browser cannot reach the issuer, so the OIDC redirect dies at
        # the first hop — flipping frontend.oidc.enabled alone would have produced a sign-in button
        # that goes nowhere.
        '--set', 'auth.enabled=true',
        '--set', 'frontend.oidc.enabled=true',
        # Both must be what the BROWSER sees, not what the cluster sees — they form the issuer and the
        # redirect URI, and OIDC matches redirect URIs EXACTLY. Over an SSH tunnel the browser's origin
        # is the LOCAL forwarded port (`-L 8080:127.0.0.1:80` → http://localhost:8080), which is not
        # the cluster's own origin; that mismatch is a redirect_uri_mismatch at the callback, after a
        # successful login, which reads like a broken app rather than a config value. Override for any
        # other access path:  RASK_PUBLIC_ORIGIN=http://10.16.51.53 tilt up
        '--set', 'frontend.oidc.publicOrigin=' + PUBLIC_ORIGIN,
        '--set', 'frontend.oidc.publicIssuer=' + PUBLIC_ORIGIN + '/dex',
        # Dex's OWN issuer must be the public one too, and this is the step that is easy to miss:
        # the chart defaults it to `http://rask-dex:5556/dex` (in-cluster). Dex renders its discovery
        # document FROM that issuer, so with the default it advertises
        # `authorization_endpoint: http://rask-dex:5556/dex/auth` — and the BFF forwards the browser
        # to exactly what discovery returned, by design (oidc.ts keeps the discovered endpoints public
        # and rewrites them inward only for server-side calls). The browser then tries to resolve an
        # in-cluster Service name and the login dies at the first hop, which presents as the network
        # blocking Dex rather than as a misconfigured issuer.
        #
        # Setting it public is what makes the split-horizon work as intended: Dex advertises public
        # URLs, browsers use them, and `internalEndpoint()` swaps the prefix back to
        # OIDC_INTERNAL_ISSUER for discovery and the token POST, which pods can reach.
        '--set', 'dex.issuer=' + PUBLIC_ORIGIN + '/dex',
        # Seals the session cookie (AES-256-GCM); the chart requires >=32 chars when oidc is enabled.
        # Fixed rather than random ON PURPOSE: a fresh secret per `tilt up` invalidates every existing
        # session cookie, so each restart silently signs you out mid-task. Dev-only by construction —
        # it lives in a Tiltfile that refuses to run outside a local k8s context (allow_k8s_contexts
        # above), and production takes this from the OpenBao-backed secret store, never a literal.
        '--set', 'frontend.oidc.sessionSecret=' + SESSION_SECRET,
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
