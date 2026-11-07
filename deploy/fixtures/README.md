# Fixtures: NOT FOR PRODUCTION

Throwaway installs of what the rask chart takes as a prerequisite, so a development cluster or a CI lane has
them (ADR 0153). A production cluster gets each from its platform team, at the version and with the settings that
team supports; nothing here is a supported install of anything.

Each directory is one fixture, installed as **its own Helm release** before the rask chart:

| File | What it holds |
| --- | --- |
| `fixture.env` | `RELEASE`, `NAMESPACE` and `CHART`; for an upstream chart also `REPO` and `VERSION`, otherwise `CHART` is a directory beside it |
| `values.yaml` | optional: values for an upstream chart (a local chart carries its own) |

`fixture.env` may name `WAIT=false` for a stand-in whose pod needs something rask's own install creates: the install does
not wait for it to turn Ready. It may also name `SET_FILES`: space-separated `<value key>=<path from the repo root>` pairs, each passed as
`--set-file`, for an artefact that lives outside the fixture's chart (the observability fixture's alert rules). A local
chart that declares upstream `dependencies:` has them vendored from its committed `Chart.lock` before the install.

`scripts/fixtures.sh <name>... [-- <helm args>]` installs them in the order named. `make k3s-up` runs it with `FIXTURES`
(default `external-secrets dapr nats cloudnative-pg dex rustfs openbao observability marquez`) before the rask release, whose product chart renders the lakehouse with its console, and installs no client. A client lives in a repository of its own and passes what the lakehouse must admit for it to the lakehouse release with `-f`.

Every fixture's release carries the mark in its description (`helm history <release> -n <ns>`), and a fixture's own
namespace is labelled `rask.dev/fixture=not-for-production`. A fixture that shares rask's namespace
(`SHARED_NAMESPACE=true` in its `fixture.env`, as Dex does) leaves that namespace unlabelled, since the label
says everything in it is throwaway.

## The fixtures

| Fixture | What it stands in for |
| --- | --- |
| `external-secrets` | the platform's External Secrets Operator, a prerequisite the rask chart refuses to render without |
| `dapr` | the platform's Dapr control plane (upstream chart, namespace `dapr-system`, rask's `dapr.controlPlaneNamespace`): fail-closed injector scoped to `dapr.io/enabled`, no `secretReader`; a prerequisite the chart refuses to render `dapr.enabled` without |
| `nats` | the platform's NATS JetStream bus (local chart over upstream nats 2.14.2, release `nats` in rask's namespace): three nodes at `rask-nats:4222`, the address `deploy/values-local.yaml` names in rask's `nats.url`. With `nats.auth.server` it runs in operator mode, its config written by its own ExternalSecrets from rask's dev OpenBao, so turn it on once rask is installed (`scripts/fixtures.sh nats -- --set nats.auth.server=true`; docs/OPERATORS.md § 7) |
| `cloudnative-pg` | the platform's CloudNativePG operator (upstream chart 0.28.3, operator 1.29.1, namespace `cnpg-system`): what reconciles the `Cluster` rask renders on the AGE-on-CNPG path, which the chart refuses to render for a cluster without `postgresql.cnpg.io/v1`. `crds.create: false`: its CRDs come from `chart/crds-bootstrap/` (`make k3s-crds`, which `make k3s-up` runs first), so no release owns them |
| `dex` | the development OIDC issuer (local chart over Dex v2.46.1, release `dex` in rask's namespace, Service `rask-dex`, the host rask's `lance.dexHost` dials): in-memory storage, the demo users, `publisher@rask.internal` (a user bound to no team, the live lanes' job identity and outsider), rask's confidential sign-in client `rask-web` (the live lanes' password grant), the console's public PKCE client `rask-console` (its `console` value: redirect URIs on :8080 and :5173, and the Vite dev server's origin answered CORS for), and the audience client, which trusts both, and the machine client `rask-pipeline`, which signs in for itself with the client-credentials grant (ADR 0181); every authorization code is bound to an S256 PKCE challenge (`oauth2.pkce.enforce`), so the device grant, whose browser leg carries no challenge, is not served; `allowedOrigins` (empty by default) joins the console's origin in `web.allowedOrigins` for a browser app served from another origin. Its defaults are rask's chart defaults (issuer `http://rask-dex:5556/dex`); `make k3s-fixtures` passes the DEV_ISSUER it passes rask, and a client's own repository passes its sign-in client in `clients`. rask's `dex.enabled` says the estate uses it, which a real deployment refuses |
| `rustfs` | the development object store (local chart, release `rustfs` in rask's namespace): one RustFS server, the upstream `rustfs/rustfs` image pinned by digest at the GA tag the Dagger lanes run, as StatefulSet `rask-rustfs` on the one claim `data-rask-rustfs-0`, Service `rask-rustfs:9000` (S3 only, NodePort 30900 for the endpoint the catalog vends). Its root pair is rask's `rask-object-store-root` (`object-store-access-key`, `object-store-secret-key`), which ESO writes from rask's dev store (`secret/object-store-root`, no Dapr app may read it) and the pod mounts as files, so `WAIT=false`: on an empty cluster the server starts once rask is installed. rask's `objectStore.enabled` says the estate uses it; rask provisions its buckets and scoped users. Rationale: `deploy/fixtures/rustfs/README.md` |
| `openbao` | the development secret store (local chart, release `openbao` in rask's namespace): an in-memory OpenBao server that starts uninitialized (not `server -dev`, which prints its root token), Deployment and Service `rask-openbao`, the address rask's Dapr secret stores and ESO read. Its seed initializes and unseals it and mints its administrator under an id ESO generates in-cluster (`rask-openbao-root`, mounted into the seed alone), so no token is published. What to seed is rask's: its init container and seed sidecar run the scripts rask renders into `rask-openbao-seed` (`mint.sh`, `open-and-seed.sh`) from the credentials ESO generates into `rask-dev-credential-<key>` (one Secret each, mounted as one directory), and apply the roles and policies it renders into `rask-secret-store-identities`, so `WAIT=false`. The seed writes only to the server beside it, and rolls with a surge so a replacement carries the signing keys and the bus trust root; when rask changes the scripts the seed mints for any new identity and runs again in place. Its pod selector is the one the store first ran under (`instance: rask`), so the handover from rask's release was a surge rollout. rask's `openbao.enabled` says the estate uses it, and a real deployment names the platform's store instead (rask's chart renders none, XC-149) |
| `marquez` | the platform's OpenLineage graph backend (local chart over Marquez 0.51.1, release `marquez` in rask's namespace): the Marquez API at `rask-marquez:5000`, built by rask onto a multi-arch JRE (`.docker/marquez.dockerfile`, pushed to the dev registry before the install, because upstream publishes linux/amd64 only), over its own Postgres (`rask-marquez-db`, trust authentication inside the namespace, a 1 Gi claim). `deploy/values-local.yaml` points rask's `services.lineage.forward.url` here, so the relay tick forwards every event lineage records to it (ADR 0158); `GET /api/v1/events/lineage` lists what it took |
| `observability` | the platform's telemetry chain (namespace `observability`): GreptimeDB on a local volume at `greptimedb:4000`, the endpoint `deploy/values-local.yaml` names in rask's `observability.export` and `query`; Perses with rask's dashboards; vmalert evaluating `deploy/alerting/rules.yml`; Alertmanager. `docs/` explains its templates |

## Handing objects to a fixture

When something leaves the rask chart and a fixture takes it over (a RayCluster, an operator's ClusterRoles, a CRD),
Helm would delete it on the rask upgrade that stops rendering it, and a CRD takes every object of its kind with it.
`scripts/fixtures.sh` hands such objects over instead: it renders the fixture, and every object that already exists
under another release (or under none) is annotated `helm.sh/resource-policy=keep`, so no upgrade or uninstall of the
old release deletes it, and the fixture installs with `--take-ownership`, so its release becomes the owner. Run the
fixture before the rask upgrade that stops rendering the object.

A workload whose selector names its release (most charts' do) cannot change owner in place, because a selector is
immutable. A Deployment or DaemonSet is deleted and the install creates it again. A StatefulSet keeps its pods and its
data: it is deleted with `--cascade=orphan`, its pods are relabelled to the new selector, and the new StatefulSet adopts
them and replaces them one at a time onto the same claims (the NATS bus kept its quorum and its streams this way). Each
re-created workload is annotated keep after the install. That handover is for an object another release owns. Within one
release a selector change needs no script: a chart whose workloads are named by the release their selector carries
renames a workload whenever its selector changes, so Helm, or any reconciler that prunes, replaces it in one upgrade.

Each install takes `--reset-values`: a fixture's values are its chart's defaults, the files in its directory, the
`SET_FILES` it names and the caller's arguments, which every caller (the Makefile, `scripts/e2e_stack.sh`,
`scripts/install_from_empty.sh`) passes on each run. Nothing an earlier install was given outlives it, so a client's
Dex values no longer passed take its Dex clients and origins with them on the next `make k3s-fixtures`.

`scripts/fixtures.sh --vendor [<name>...]` only vendors the local fixture charts' dependencies, with no cluster;
`make k3s-deps` runs it so a test can render a fixture.
