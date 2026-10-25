# Deployment

rask uses a **Helm chart at `chart/`** as the single deploy artifact for both
local k3s and production Kubernetes. The chart is the product (ADR 0153): it
renders rask's own services and the objects they need, and installs no
operator or control plane. Those are prerequisites the platform runs, each in the
sections below; development installs them from `deploy/fixtures`, each its own
release, before rask's (`make k3s-up`).

## Prerequisites: the External Secrets Operator and a secret store

The chart takes **no credential as a value** ([[XC-004]]). Every secret lives in a store (the platform's
Vault or OpenBao in production, the dev OpenBao fixture in development; the next section) and reaches a pod
from it: through the Dapr secret store for a pod with a sidecar, and through an ExternalSecret for one
without. So the **External Secrets Operator is a prerequisite**, platform-run like the store, and rask never
installs it.
Install it on its own release first, at the version `deploy/fixtures/external-secrets/fixture.env` names (the one
rask is tested against; that fixture is also the development install `make k3s-up` runs).

Without it, `helm upgrade` and `make k3s-up` fail at render time with a message naming the operator. A
cluster-less `helm template` states the API it targets: `--api-versions external-secrets.io/v1/ExternalSecret`
(plus `--api-versions generators.external-secrets.io/v1alpha1/Password` for the dev OpenBao, whose
credentials ESO's Password generator creates in-cluster). A credential passed as a value is refused.

## Prerequisite in production: the secret store

The chart renders no secret store ([[XC-149]], ADR 0153). `deploy/values-prod.yaml` sets `openbao.enabled: false`,
and the deployer names the platform's Vault or OpenBao as `openbao.externalAddr`, over https: every sidecar sends the
store's token to it, so the render refuses an http address. Its certificate must chain to a CA the sidecars and ESO
trust by default; the chart names no CA bundle. Under the production NetworkPolicy, `openbao.egress` states the
store's addresses, and the rule admits only the pods whose sidecar reads it (the apps `lance.secretScopes` lists).
The render refuses either one empty.

Two kinds of identity read it, each through the store's Kubernetes auth method at `externalSecrets.kubernetesAuthPath`
([[XC-165]], ADR 0169). Each app's sidecar logs in as its own ServiceAccount: the one Dapr secret store `lance-secrets`
(`secretstores.hashicorp.vault`, KV v2 under `externalSecrets.kvMount`) names the role `<release>-{appID}`, which Dapr
fills with the sidecar's own app id, and presents a token the kubelet projects for `openbao.identity.audience`
(`rask-openbao`) into daprd alone, so no store token exists to deliver or rotate. This needs Dapr 1.19 or later, the first
whose Vault store logs in with Kubernetes auth and renews its session (components-contrib#4448). ESO's SecretStore
`<release>-vault` logs in as the role `externalSecrets.role`, bound to ESO's ServiceAccount.

THE ROLES AND POLICIES THE PLATFORM'S STORE MUST HOLD. The chart renders them in every mode, as the documents the dev
store's seed applies (`<release>-secret-store-identities`, keys `role-<name>.json` and `policy-<name>.json`):
one role per app that reads the store, `<release>-<app id>`, bound to that app's ServiceAccount in the release's namespace,
taking only the projected audience, and holding one policy that reads exactly the secrets the app and its components read
(`lance.storeReads`), plus ESO's role and its policy, which reads exactly the secrets the chart's ExternalSecrets name. A
role a sidecar cannot log in to fails that sidecar's start closed. Each app's Dapr Configuration denies its code every
secret but its own (`allowedSecrets`), so a component's secret (a bus user, the state store's DSN) is read by the sidecar's
login and never served to the app.

THE SECRETS THE STORE MUST HOLD. Nothing seeds the platform's store and no credential is a chart value, so an
unprovisioned secret is a fail-closed outage at first use, not a fallback. Each secret has exactly the readers it is for:
- `lineage-s3`, `maintenance-s3` (fields `access_key`, `secret_key`): the scoped key pair of the identity values.yaml
  `objectStore` names for that plane, read by that app alone. On the dev store the secret half is
  sha256("<identity>-s3-<root secret>")[:40] (`lance.scopedSecretFn`, which `object-store-scoped-users` derives too).
  Both halves are read from the store and read again every few minutes (ADR 0166), so a rotation writes them together.
  The catalog has no such secret (ADR 0170): the object store trusts the cluster's ServiceAccount issuer for
  `objectStore.identity.audience` and grants the catalog's subject, `system:serviceaccount:<namespace>:<release>-sa-catalog`,
  the catalog's policy, and the catalog exchanges its projected token for sessions through STS
  `AssumeRoleWithWebIdentity`. A platform store's owner provisions that trust; on AWS it is an IAM OIDC provider and
  the role `vending.assumeRoleArn` names, whose trust policy names the subject.
- `backup-s3` (ESO, for the backups, when one is on), fields `access_key`, `secret_key`.
- `dapr-app-token` (fields `token` and, during a rotation, `previous`; docs/OPERATORS.md § 8): the app-API token, read by
  the apps with a Dapr door (catalog, lineage, maintenance, notifications) and by ESO, which writes it into the
  `<release>-dapr-app-token` Secret daprd stamps the header from.
- `dapr-state` (field `connection-string`): the Dapr state store's DSN, read by the logins of the apps the state store
  is scoped to.
- `postgres` (field `password`), read by ESO for the AGE server, OpenFGA and the Collector; `ray-auth-token` (field
  `token`), read by ESO when `ray.auth` is on.
- For every identity in `lance.signingIdentities`, `signing-public-<identity>` (field `keys`, read by that signer and
  every verifier) and then `signing-key-<identity>` (field `seed`, read by that signer alone), described in values.yaml
  `signing:`; the render refuses the platform's store until its operator created them and `signing.provisioned` says so.
- `nats-user-<user>` per values.yaml `nats.auth` user (read by that app's login, or by ESO for `admin` and `monitor`),
  and `nats-route` (and `nats-server`, in operator mode), attested by `nats.auth.provisioned`.
- With `secrets.hfTokenInStore`, `hf-token` (field `token`); with the in-chart Collector, `otel-collector-export`
  (fields username, password), the credential the audit store admits. A multibase data base holds none: it is its
  tables' storage on the estate's store, reached under the catalog's own identity ([[LH-286]]).

No secret holds a store root: rask presents none in production, and on the dev store the root sits at
`object-store-root`, which only ESO reads, for the store and its provisioning hook (ADR 0168). The dev seed
(`chart/templates/openbao-seed.yaml`) writes the same list into the dev store; keep the two in step.

The store holds no IdP client secret. The lakehouse's audience (`auth.oidc.audience`) is a client nobody signs in as,
so its secret is given to no service. The object store holds none either: verifying a web-identity token needs only the
client id, and RustFS reads an OIDC client secret from env alone (no `_FILE` variant,
`.claude/skills/rask-rustfs/references/security.md` § Web identity), so a store given one would carry a credential in
its environment.

## Prerequisite: the Dapr control plane

rask's services talk to each other, publish and subscribe, hold actors and read secrets through Dapr sidecars, so a
**Dapr control plane is a prerequisite** (ADR 0153), installed once per cluster by the platform, as Dapr documents,
in `dapr-system`. The chart renders only rask's own Dapr objects (Components, Configurations, Resiliency,
Subscriptions) and the sidecar annotations; with `dapr.enabled` on it refuses to render for a cluster that does not
serve `dapr.io/v1alpha1` (a cluster-less `helm template` names it: `--api-versions dapr.io/v1alpha1`). Name the
control plane's namespace in `dapr.controlPlaneNamespace` (default `dapr-system`): the Collector scrapes it there,
and under NetworkPolicy every rask pod may reach it.

What the control plane must provide, and the version rask is tested against, are in `deploy/fixtures/dapr` (the
development install `make k3s-up` runs): Dapr 1.19 (1.19.0-rc.4 until its GA release; 1.19 is the floor, since each
sidecar's store login needs its Kubernetes auth); an injector that fails closed (`webhookFailurePolicy: Fail`)
scoped to pods labelled `dapr.io/enabled: "true"`, which rask stamps; no `dapr_rbac.secretReader`; Sentry mTLS on.
In production also HA (`global.ha.enabled`) and an injector that drops every sidecar capability
(`dapr_sidecar_injector.sidecarDropALLCapabilities`), the pair to rask's `dapr.sidecarRestricted`.

## Prerequisite: NATS JetStream

Every event rask moves (lineage, the catalog's control broadcast, the maintenance queues) rides Dapr pub/sub over **NATS JetStream, a prerequisite** (ADR 0153) the platform runs. The chart
renders no server; it renders what is rask's: the address its clients dial, the stream definitions (the stream Job,
which creates rask's streams on the bus as the `admin` user), and the bus users (`nats.auth.users`, one per app). Name
the bus in `nats.url` (the render refuses without it). Each user's credential `nats-user-<user>` lives in the store; a store the chart does not seed needs them issued by
the bus's operator and attested with `nats.auth.provisioned` (docs/OPERATORS.md § 7).

What the bus must provide, and the version rask is tested against, are in `deploy/fixtures/nats` (the development
install `make k3s-up` runs, release `nats`, Service `rask-nats` in rask's namespace): NATS 2.14.2; JetStream on a file
store; a cluster of at least `nats.streamReplicas` nodes (three); operator mode with one APP account holding JetStream
and every rask user, so Dapr's pubsub.jetstream can authenticate with a user JWT and NKEY seed.

## Prerequisite: the CloudNativePG operator, for the AGE-on-CNPG path

The default Postgres (OpenFGA's datastore and the Dapr state store) is the `rask-age` StatefulSet, which needs no operator. The AGE-on-CNPG path
(`age.cnpgCluster.enabled`, docs/CNPG-AGE.md) renders a CloudNativePG `Cluster`, so for it the **CloudNativePG operator
is a prerequisite** (ADR 0153): the chart installs no operator, and with `age.cnpgCluster.enabled` on it refuses to
render for a cluster that does not serve `postgresql.cnpg.io/v1` (a cluster-less `helm template` names it:
`--api-versions postgresql.cnpg.io/v1/Cluster`). The operator's CRDs belong to no Helm release: a release that owned
them would delete every CNPG CRD, and every `Cluster` with them, on uninstall.

The version rask is tested against is in `deploy/fixtures/cloudnative-pg` (the development install `make k3s-up` runs,
release `cloudnative-pg` in `cnpg-system`): chart 0.28.3, operator 1.29.1, CRDs applied server-side from
`chart/crds-bootstrap/` by `make k3s-crds` before it. The path itself needs CNPG >= 1.27 and Kubernetes 1.33+.

## Prerequisites in production: OpenFGA and its Postgres

The chart can run OpenFGA (the `openfga` subchart) and the Postgres behind OpenFGA and the Dapr state store (the
`rask-age` StatefulSet). Development runs both. **Production names the platform's** ([[XC-132]], ADR 0153),
and `deploy/values-prod.yaml` turns both off (`openfga.enabled: false`, `age.enabled: false`):

- `auth.fgaApiUrl` is the platform's OpenFGA HTTP API. Every FGA client, the `openfga-model` hook and the
  bootstrap-admin Job dial it. Each one presents its projected ServiceAccount token: audience `rask-openfga`, issuer
  `auth.serviceAccountIssuer`, subject `system:serviceaccount:<namespace>:<release>-sa-<component>`. The platform's
  OpenFGA takes OIDC against that issuer, fetching its keys from the cluster's ServiceAccount JWKS. rask's issuer
  mirror and subject ConfigMap serve only the in-chart OpenFGA. The subjects to admit are the ones
  `lance.openfgaSubjects` (chart/templates/_helpers.tpl) computes for the overlay. A render with `openfga.enabled=true`
  prints them in the `<release>-openfga-authn` ConfigMap.
- `age.externalHost` is the platform's Postgres, which needs the `openfga` and `daprstate` databases owned by
  `age.user`; lineage keeps no database (ADR 0158). Its password is the secret store's
  `postgres-password`, which ESO delivers, and the Dapr state store reads its whole DSN from the store's
  `dapr-state-connection-string` (`postgres://<age.user>:<password>@<age.externalHost>:<age.port>/<age.stateDb>`),
  which the platform writes beside it. No chart value carries either.

The render refuses either one when it is empty and its part is off. Under the production NetworkPolicy (default-deny egress), each endpoint needs a `networkPolicy.extraEgress` entry, as an external NATS does; the OTLP backend takes its in `observability.export.egress`, the API server its endpoints in `networkPolicy.apiServerEgress`, an external OpenFGA its addresses in `auth.fgaEgress`, and the platform's secret store its addresses in `openbao.egress`, each admitting only the pods that dial it. Every such rule names its `to` peers: the render refuses one that admits every destination. OpenFGA's own datastore, availability and
backups belong to the platform, so production runs no `pg_dump` CronJob.

## Development stand-ins

A real deployment brings its own identity provider, object store and secret store, and the chart takes them by
reference (`auth.oidc.issuer`, https, and the lakehouse's own `auth.oidc.audience`, which the IdP
issues to the clients it trusts; `objectStore.externalEndpoint` and the scoped
credentials; `openbao.externalAddr`).
Development runs stand-ins from `deploy/fixtures` (ADR 0153), and the chart renders none of them:

- **Dex** (`deploy/fixtures/dex`, release `dex`, Service `rask-dex`): the OIDC issuer with the demo users and rask's
  static client. `dex.enabled` says the estate uses it: the cluster route serves it at `/dex`, and a real
  deployment's render refuses it.
- **RustFS** (`deploy/fixtures/rustfs`, release `rustfs`, StatefulSet and Service `rask-rustfs`, ADR 0168): the S3
  store, one replica on the claim `data-rask-rustfs-0`, S3 on 9000 and the dev NodePort 30900, the upstream
  `rustfs/rustfs` image pinned by digest. `objectStore.enabled` says the estate uses it: rask's dev store issues its
  root pair (`object-store-access-key`/`object-store-secret-key` in `rask-object-store-root`, through ESO from
  `secret/object-store-root`, which no Dapr app may read), which the server reads as files, never env; rask's
  scoped-users hook (`object-store-scoped-users`, on rask's own image) provisions its buckets, policies and scoped
  users through RustFS's admin API. Its server starts once
  rask is installed, so the fixture install does not wait for it. The root secret signs every STS session token, so
  the chart hands it only to the store pod and the scoped-users hook (`rask-rustfs` § 3), and in the dev store only ESO's
  login reads it (ADR 0168, ADR 0169).
- **OpenBao** (`deploy/fixtures/openbao`, release `openbao`, Deployment and Service `rask-openbao`): the in-memory secret
  store. `openbao.enabled` says the estate uses it, and a real deployment's render refuses it. Its server starts
  uninitialized, not in dev mode (which prints its root token and unseal key to its log), and the seed beside it
  initializes it: the init's root token mints the store's administrator under an id the fixture generates in-cluster
  (`rask-openbao-root`, mounted into the seed alone), and is revoked, and the unseal key stays on the seed's memory
  volume. What goes into it stays rask's: the chart renders the seed scripts into `<release>-openbao-seed` (the dev
  credentials, every signing identity's Ed25519 pair, the bus trust root and users) and the store's roles and policies
  into `<release>-secret-store-identities` (a role and a policy for each identity that reads the store), and the
  fixture's pod runs the scripts beside the server and applies the documents.
- **Marquez** (`deploy/fixtures/marquez`, release `marquez`, Service `rask-marquez:5000`): the graph backend, with its
  own Postgres. The lineage graph is bring-your-own (ADR 0158), and `deploy/values-local.yaml` points lineage's
  forwarder (`services.lineage.forward.url`) at it, as a platform would point it at its own Marquez, DataHub or
  OpenMetadata.

## Local deploy (k3s)

```bash
make k3s-install      # one-time: k3s + helm (sudo)
make k3s-build        # build the fleet, the zones, the lakehouse image and the dev images as :dev, through Dagger
make k3s-import       # side-load the :dev images into k3s containerd
make k3s-up           # the fixtures (FIXTURES), each its own release, then helm upgrade --install rask ./chart --wait
# UI: http://rask.local/   API: http://rask.local/api/health
# (add "127.0.0.1 rask.local" to /etc/hosts)
make e2e              # verify MFE hydration + API round-trip end-to-end (run after k3s-up)
make k3s-down         # uninstall   |   make k3s-purge  # + delete PVCs
```

`k3s-build` builds every image through `scripts/dagger-image.sh --out`, as an OCI tarball in `build/k3s-images/`:
`compute`, `notifications` and `flows`, the seven zone images (`web-<zone>`), `annotator` (the client
the annotator fixture runs), `media` (the viewer and search, which the explorer runs), `lance-rest-catalog` (the
lakehouse image that serves catalog, lineage and maintenance) and the `ray-cluster` image the
development engine fixture runs. `k3s-import` loads each tarball into k3s containerd as `<image>:dev` with
`sudo k3s ctr images import`; neither step runs docker. The release installs into `RELEASE_NAMESPACE` (default
`default`, the fixtures' namespace), and `values-live-pins.yaml` files are per-host state from `make k3s-pins`, never
committed ([[XC-152]]). The dev-registry path (`make dev-registry`, then `dagger call image --name=<stem> publish
--address=172.17.0.1:5000/<stem>:<tag>`) remains for the in-cluster loop `CLAUDE.md` describes.

## Container images (`.docker/`)

Every image is built by Dagger (`.dagger/images.go`, reached through `scripts/dagger-image.sh`) from a dockerfile in
`.docker/`, with the repo root as context; the dockerfile is the single source of truth, so a build behaves the same
locally and in CI.

| Image | Dockerfile | Notes |
|---|---|---|
| `lance-rest-catalog` | `rest-catalog.dockerfile` | The lakehouse image, one image for several deployments: `catalog`, `lineage` and `maintenance`, plus the chart's Jobs and hooks. It installs no client, so deleting a client changes nothing it builds. |
| `media` | `media.dockerfile` | The viewer and search, clients of the lakehouse, one image for both; the product chart deploys neither. |
| `flows`, `notifications`, `compute`, `annotator` | `<name>.dockerfile` | One per service, `uv sync --frozen --package <name>` against the root `uv.lock`. `compute` and `annotator` each ship in their own chart (`deploy/fixtures/compute`, `deploy/fixtures/annotator`). |
| `web-<zone>` (7 images) | `frontend.dockerfile` | One parametrized dockerfile, `--build-arg APP=<zone>`. SSR via `svelte-adapter-bun`; `home` is the catch-all (owns `/`), the six domain zones (lakehouse, explorer, annotator, compute, models, studio) each pin a bare base `/<zone>`. The zones are a client of the lakehouse, and the product chart deploys none of them (XC-181). tini as PID 1, non-root UID 10001. |
| `ray-cluster`, `ray-<runner>` | `ray-cluster.dockerfile`, `ray-runner.dockerfile` | The development engine's head image and one image per sealed runner (`--runner <runner>`); neither is part of the product chart. |

The one frontend dockerfile encodes a non-obvious build contract (documented in its header): `svelte-adapter-bun`
externalizes `@sveltejs/kit`, so the final image must ship `node_modules` (`build/` is not standalone). And bun 1.3's
**isolated linker** keeps real packages in `node_modules/.bun/` with per-member symlinks, so the final stage copies
both the root store **and** the app dir (with its symlinks) and runs from the app dir.

The frontend topology those images serve (the Turborepo vertical-microfrontend proxy, the shared `@rask/ui` shell,
and per-app `kit.paths.base`) is documented in the `rask-frontend` project skill (`.claude/skills/rask-frontend`).

## The engine is the user's

The lakehouse submits no job and runs no stage, training or engine
([ADR 0155](../adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)): a pipeline is the user's, on the engine
they run, and it writes through the catalog's namespace doors. The chart installs no Ray (ADR 0153): no KubeRay
operator, no RayCluster or RayService, no GPU reservation and no Serve application.

The **`compute` service** (`:8804`, over `ray-kit`) is the companion of a bring-your-own Ray engine, not part of the
lakehouse. It reads that engine's Ray dashboard REST API at `RAY_DASHBOARD_URL` for the jobs board and job-history
prune, and proxies `/api/serve/*`. It ships in its own chart, `deploy/fixtures/compute` (release `compute` in rask's
namespace, installed by `make k3s-clients` after `make k3s-up`), which reads the rask release's Dapr app token, `lance-tracing`
Configuration, Dex, catalog, OTel Collector and Ray auth token by reference; it holds no OpenFGA credential and asks the
catalog's decision door, which answers its service account only when the product chart's `auth.externalDeciders` names
it ([[LH-442]]). The fixture chart publishes `/api/ray` and `/api/serve`
with its own HTTPRoute on the same Gateway (`deploy/fixtures/compute/chart/templates/route.yaml`), and a NetworkPolicy
admitting its app port. The address of the engine's dashboard is `ray.dashboardUrl`
(`rask.rayDashboardUrl`); the annotator fixture names the same head for its Serve discovery
(its `serve.discoveryUrl`).

In development `make k3s-clients` installs the `kuberay-operator`, `ray-cluster` and `compute` fixtures, after `make k3s-up`
(`deploy/fixtures`, not for production); the head Service is `rask-ray-head-svc`, and `deploy/values-local.yaml`
names it. GPUs, a device plugin, the `nvidia` RuntimeClass and each Serve replica's GPU fraction belong to the
engine: the `ray-cluster` fixture's `gpuCount` sizes its head, and the `nvidia-device-plugin` fixture advertises
`nvidia.com/gpu` on a node where nothing does yet (`deploy/fixtures/README.md`). A runner reaches a remote cluster
with `--address ray://…:10001`.

## Helm chart (`chart/`)

`make k3s-up` runs `helm upgrade --install rask ./chart --wait`. The chart deploys:

- **notifications** — the per-subject inbox.
- **the lakehouse** — catalog, lineage and maintenance. The clients (the zones, flows, the viewer and search, and the
  annotator, which has its own release, `deploy/fixtures/annotator`, ADR 0171) are not rendered here.
- **The cluster route** (`templates/route.yaml`) — Gateway API HTTPRoutes on the platform's Gateway
  (`route.parentRefs`): `<release>-api` sends each public `/api/*` prefix to its service, with a `URLRewrite`
  `ReplacePrefixMatch` where the service mounts another path (the table is `rask.apiRoutes` in `_helpers.tpl`), and
  `<release>-dex`, rendered only with `auth.enabled` and `dex.enabled`, sends `/dex` to the development Dex. A real deployment names the hosts the
  routes answer (`route.hostnames`), and the render refuses one that names none, since a host-less route claims every
  host its listener accepts ([[XC-146]]). No rask process sits at the edge: an
  unmatched `/api/*` is the Gateway's own 404, and every service authenticates each request itself. Only Core matches
  and `URLRewrite` are used, so any conformant implementation serves them (kgateway, the intended production edge;
  Envoy Gateway, Istio, Cilium); in development it is k3s's Traefik with its Gateway provider switched on by
  `deploy/k3s/traefik-gateway.yaml`, which `scripts/k3s-install.sh` installs (GatewayClass `traefik`, Gateway
  `traefik-gateway` in kube-system, listener `web` on the node's port 80). The Gateway must allow 64 MB request bodies;
  that is Gateway policy, not route (ADR 0165).
- **Optional, in-chart for development and the platform's in production** ([[XC-132]]): `openfga.enabled` (the
  OpenFGA subchart) and `age.enabled` (the apache/age Postgres StatefulSet behind OpenFGA and the Dapr state store); see *Prerequisites in production*.
- **Its own configuration of what it does not run**: the Dapr Components, Configurations and Resiliency, the NATS stream
  Job, the ExternalSecrets and SecretStore, the object store's bucket and user hooks, and the dev OpenBao's seed scripts.
- **The OTel Collector**, with `observability.enabled` (see below).

Everything else is a separate release ([[XC-085]], ADR 0153). `make k3s-up` installs the development set from
`deploy/fixtures` first (`make k3s-fixtures`): the External Secrets Operator, Dapr, NATS, the CloudNativePG operator,
Dex, RustFS, OpenBao, the telemetry chain, the lineage graph backend and the SMTP relay. It installs no client:
`make k3s-clients` adds them after it (the KubeRay operator, a Ray cluster, the `compute` chart, the table-maintenance
job and the annotator), each its own release reaching the lakehouse by reference. A platform brings its own.

### The first estate admin

`auth.bootstrapAdmin` names the first estate admin by principal, `<idp-id>~<user id>` (the idp id `auth.oidc.connectors`
gives their Dex connector; `dex~<userID>` for a development Dex user, LH-063), and the render refuses a bare name: a
post-install/upgrade hook Job grants that principal `owner` on the default warehouse and `admin` on the estate root,
the grants the estate-wide surfaces gate on. Empty seeds no human grant.

### StorageClasses: omit, don't guess

No template names a StorageClass: a claim that omits `storageClassName` lets the **cluster's
default** class provision — the only portable answer (`local-path` is k3s's provisioner name and does not
exist on kind, whose default is `standard`). Note `storageClassName: ""` is *not* the same as omitting it:
the empty string disables dynamic provisioning, which is why the templates use `with`. No test pins it.

### Observability (`observability.enabled`)

rask's telemetry seam is one **OTel Collector** (`chart/templates/otel-collector.yaml`, `rask-otel-collector`),
the **single log shipper** (Vector retired, owner ruling 2026-07-27). It receives the fleet's OTLP, tails
infra-pod logs (filelog), scrapes the Dapr sidecars, the Dapr control plane and OpenFGA, and exports
everything over OTLP/HTTP to **`observability.export.endpoint`**, with the backend's ingestion headers from
`observability.export.{headers,traceHeaders,auditHeaders}` and HTTP basic auth from the store's
`otel-collector-export` secret (via ESO, as mounted files).

**The store, dashboards and alerting are a prerequisite, not part of the chart** (ADR 0153). A platform names
its OTLP backend in `observability.export.endpoint` (values-prod leaves it empty and the render refuses
that), and loads rask's alert rules, **`deploy/alerting/rules.yml`** (Prometheus rule-file format; proven by
`make alert-rules-check` and replayed against GreptimeDB by `make alert-rules-drill`), into its own
evaluator. The store must meet the audit store's contract in `chart/values.yaml` `observability`.

**Development** gets the chain from the `observability` fixture (`deploy/fixtures/observability`, release
`observability` in namespace `observability`, installed by `make k3s-up`), which `deploy/values-local.yaml` names:

| Component | Version | Service | Role |
|---|---|---|---|
| **GreptimeDB** | `greptimedb-standalone` 0.4.5 (app 1.1.1) | `greptimedb.observability:4000` | Metrics/logs/traces store on a local volume: OTLP at `/v1/otlp`, PromQL at `/v1/prometheus`, SQL at `/v1/sql`; a retention Job sets the database and audit-table TTLs |
| **Perses** | 0.22.0 | `perses.observability:8080` | rask's dashboards-as-code over GreptimeDB's PromQL |
| **vmalert** | v1.106.1 | `observability-vmalert:8880` | Evaluates `deploy/alerting/rules.yml` (passed with `--set-file`) |
| **Alertmanager** | v0.28.0 | `observability-alertmanager:9093` | Groups and holds firing alerts (no receiver: pages nowhere) |

**App instrumentation:** `service_kit.setup_otel` instruments the FastAPI fleet —
the services built on `make_service_app` call it automatically, so each service roots or
continues the trace of every request the cluster route hands it. It wires both **traces** and **RED
metrics**: the FastAPI/HTTPX instrumentation emits `http.server.*`/`http.client.*` request count,
duration, and active-request metrics automatically. Every plane exports plain OTLP/HTTP to the
Collector (`OTEL_EXPORTER_OTLP_ENDPOINT`, `RASK_OTEL_ENABLED` when `observability.enabled=true`) and names
no store; the Collector adds the backend's headers. When the toggle is off the env vars are absent and
instrumentation is a no-op. Standard OTLP throughout; OTel-Arrow is not used (the Python SDK lacks OTAP support).

Greenfield local cutover (drops old PVCs): `make k3s-purge && make k3s-up`.

Sensitive config comes from an operator-created Secret (`existingSecret`, default
`rask-app`); non-sensitive config from `values.yaml` → ConfigMap. See
`chart/README.md`.

## CI

- **`.github/workflows/ci.yml`** calls the Dagger module's functions (`lint`, `typecheck`, `openapi`, `test`,
  `test-pg`, `charts`, `frontend`, `scan-*`, `zone-smoke`) plus the kind-backed live jobs
  (`make e2e-ci`) and the per-zone Playwright suites; `dagger call <fn>` locally is the CI step.
- **`.github/workflows/docs.yml`** builds this Zensical site (`zensical build --clean`) and Storybook, and deploys
  both to GitHub Pages.

## State stores

- **The object store** (RustFS in development, the platform's S3 in production): every Lance table the catalog
  governs (lineage's commit feed, edge list, outbox refusals and forwarder cursors among them, in the catalog's
  provenance namespace, ADR 0158), the outbox, the trash and the registries. Lance puts its commit CAS in the store,
  so neither the catalog nor lineage needs a relational database.
- **Postgres**: OpenFGA's datastore and the Dapr state store, served by the `rask-age` StatefulSet in development and
  by the platform in production. Lineage keeps no database (ADR 0158, LH-468 #592).
