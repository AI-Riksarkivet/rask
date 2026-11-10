# Operators: what Lakekeeper's and Polaris's own, and what rask's should

> Research note. Lakekeeper and Polaris are references, never the stack: every proposal is stated on rask's own stack
> (Lance and lance-namespace, OpenFGA, Dex, Kubernetes ServiceAccounts, NATS JetStream, one Helm chart under Argo CD).
> Sources, each cloned and read 2026-11-10:
>
> - `github.com/lakekeeper/lakekeeper-operator` at `main` `27fa1e1` (2026-06-10), with the Lakekeeper server at `08b67d7`
>   and `lakekeeper-charts` at `040875a`;
> - `github.com/apache/polaris` at `main` `be8167fe0` (2026-11-10), `apache/polaris-tools` at `823dddd`,
>   `apache/terraform-provider-polaris` at `abb1950`;
> - secondary, third party: `github.com/canonical/polaris-k8s-operator` at `90c45e4` (2026-11-05);
> - the frameworks: `kubernetes-sigs/kubebuilder` `f9c0b30` (release v4.16.0), `kube-rs/kube` `f0b9c44` (5.0.0),
>   `nolar/kopf` `77c0ce3` (1.45.2), `metacontroller/metacontroller` `8083a00` (v4.17.6), and `nats-io/nack` `4f08e6d`
>   (v0.25.0);
> - rask at `0a589afc7`.
>
> Paths under a reference's heading are relative to that checkout; rask paths are relative to this repository. Commit
> counts, releases and stars are GitHub's API answers on 2026-11-10. Nothing was run or measured.

## 1. Summary

1. **Lakekeeper's operator owns one Lakekeeper instance's Kubernetes objects and nothing inside the catalog.** One CRD,
   `Lakekeeper`, renders a Deployment, two Services and a database-migration Job, validates the Secrets its spec names,
   and polls (optionally calls, unauthenticated) the server's bootstrap endpoint. It creates no project, warehouse,
   principal or grant; those are listed as planned CRDs. It is a work in progress: three commits, no release.
2. **Apache Polaris ships no Kubernetes operator.** Its first-party Kubernetes tooling is a Helm chart with a
   maintenance CronJob; a realm is bootstrapped by a hand-run `kubectl run` of the admin tool, schema migration is a
   database administrator's manual step, and the first-party Terraform provider is an empty repository. The only
   third-party operator with code is Canonical's Juju charm, which also manages the server alone.
3. **Neither reference converges catalog objects from Kubernetes today**, so neither has met the two-writer, deletion
   and drift problems rask would meet. Lakekeeper's server already carries the seam for it: a `server#operator` relation
   "designed to be used by machines that provision resources inside Lakekeeper, i.e. a `KubernetesOperator`".
4. **rask's chart under Argo CD already does what Lakekeeper's operator does.** An operator that rendered rask's
   Deployments would be a second writer of objects Argo CD owns. None of rask's provisioning Jobs gains enough from a
   controller to justify replacing it in a first scope; the one real drift gap among them, the NATS streams, is closed
   by the platform's own NATS controller (NACK), not by rask code.
5. **What a rask operator should own is catalog-level intent declared in git**: projects, warehouses and grants first,
   reconciled only through the catalog's audited doors as the operator's own ServiceAccount identity, with the catalog
   as the state of record and no deletion cascaded from a custom resource. Recommended framework: Go with kubebuilder
   (controller-runtime), namespace-scoped.

## 2. The references, from source

### 2.1 Lakekeeper's operator (`lakekeeper-operator` `27fa1e1`)

**Maturity.** The README opens "**Work in Progress** — This operator is under active development and is not yet
production-ready. APIs, CRDs, and behavior may change without notice" (`README.md:3-5`). `main` has three commits
(`5ef82c8` "Initial Commit" 2026-04-30, `7930d94` and `27fa1e1` 2026-06-10), no tag and no GitHub release; installation
is from source only (`README.md`, "From source (currently the only available method)"). Go, operator-sdk v1.42.2 on
Kubebuilder v4 (`README.md`, Technology Stack; `PROJECT`: `go.kubebuilder.io/v4`, one namespaced resource).

**The CRD.** One kind, `Lakekeeper` (`lakekeeper.k8s.lakekeeper.io/v1alpha1`). Its spec mirrors the server's
configuration: `image`, `replicas`, `resources`, `database` (Postgres host, port, user or `userSecretRef`,
`passwordSecretRef`, `encryptionKeySecretRef`, pool and TLS settings), `server`, `secretStore` (Postgres or Vault KV2),
`authentication` (OpenID, Kubernetes), `authorization` (`allowall`, `openfga` or `cedar`), `idempotency`, `taskQueues`,
`requestLimits`, `ssl`, `bootstrap` (`api/v1alpha1/lakekeeper_types.go:66-122`, enums at `:28-51`, CEL one-of rules
at `:126`, `:140`, `:379`). The planned CRDs, `LakekeeperProject`, `LakekeeperWarehouse`, `LakekeeperNamespace`,
`LakekeeperUser` and `LakekeeperRole`, are a table marked "Planned" in `docs/architecture.md` (§ CRD Design), with
"Lakekeeper API Integration: Use go-lakekeeper SDK for entity management" under Future Enhancements.

**The reconcile** (`internal/controller/lakekeeper_controller.go:118-201`), in order: fetch the CR; on a deletion
timestamp run `reconcileDelete`; add the finalizer; check every referenced Secret exists and holds its key
(`:349-380`); set a `ConfigWarning` for fields not implemented (`:1465`); create or wait for the migration Job, and
return until it succeeds (`:226-303`); `CreateOrUpdate` the Deployment (`:425-535`) and the main and metrics Services
(`:537-618`); update status from the Deployment (`:620-677`); requeue every `checkInterval` (15 s) until the server
reports bootstrapped (`:194-199`). It watches its CR and `Owns` Deployments, Services and Jobs (`:1508-1516`); it does
not watch the Secrets it references (`docs/architecture.md`, Future Enhancements).

**RBAC.** A ClusterRole bound by a ClusterRoleBinding: CRUD on Deployments, Services and Jobs, and `get;list;watch` on
**every Secret in the cluster** (`lakekeeper_controller.go:81-88`; `config/rbac/role.yaml:1-13`;
`charts/lakekeeper-operator/templates/rbac/manager-rolebinding.yaml:2`).

**Status.** `conditions` of type `Ready`, `Degraded`, `Migrated`, `Bootstrapped`, `ConfigWarning`
(`lakekeeper_controller.go:52-56`), plus `bootstrappedAt`, `serverID`, `migrationJob`, `observedGeneration` and replica
counts (`lakekeeper_types.go:622`).

**Finalizer and deletion.** It adds `lakekeeper.k8s.lakekeeper.io/finalizer` (`:58-59`, `:138-147`), and its delete path
removes it and does nothing else: "Kubernetes will automatically delete owned resources ... When we add Lakekeeper API
integration in future, we would clean up external resources here (warehouses, projects, etc.)" (`:325-345`). Owned
objects carry a controller reference (`:441`, `:552`, `:590`, `:1721`), so deleting the CR deletes the Deployment,
Services and Jobs; the database and everything in it are untouched.

**Secrets and credentials.** It reads Secrets only to check they exist (`:360-370`). Values reach the server as
environment variables through `secretKeyRef`: the database password and encryption key (`:1041-1058`), the Vault
password (`:1143-1151`), the OpenFGA client secret (`:1206`). TLS certificates are mounted from Secrets as read-only
volumes (`:1407-1460`). It talks to no secret store, database or OpenFGA itself.

**Upgrades and migrations.** A migration Job named by the first characters of a SHA-256 over the migration-relevant
fields (`image` and the database location and user) (`:1518-1591`); a new hash creates a new Job, which must succeed
before the Deployment is updated (`:226-303`). The Job runs the server image with `args: ["migrate"]`, backoff limit 3,
TTL 600 s (`:62-64`, `:1655-1730`). Failure is judged by `job.Status.Failed >= 3` (`:1641`) on a pod spec with
`RestartPolicy: OnFailure` (`:1703`), under which a failing container restarts in place; whether `status.failed` ever
reaches 3 there is not settled (§ 5). Authorization-model migration is the server's own: the operator passes
`authorizationModelVersion` through as an env var (`:1188-1191`). The chart does the same migration as an Argo CD
`Sync` hook (`lakekeeper-charts` `charts/lakekeeper/templates/db-migration.yaml:13-20`), so the operator replaces a
chart hook with a controller-created Job of the same command.

**How it calls the catalog's API.** Only `GET /management/v1/info` and, when `spec.bootstrap.enabled`, `POST
/management/v1/bootstrap` with `{"accept-terms-of-use": true, "is-operator": true}`, over plain HTTP to the in-cluster
Service and **with no Authorization header** (`:745-800`, `:803-830`). The server refuses an anonymous bootstrap under
OpenFGA: `can_bootstrap` returns 401 "Anonymous users cannot bootstrap the catalog" (`lakekeeper` `08b67d7`
`crates/authz-openfga/src/authorizer.rs:136-149`). So the operator's bootstrap works only with the `allowall`
authorizer, which is what every end-to-end test uses (`test/e2e/e2e_test.go:296`, `:482`, `:591`, `:687`).

**Tests.** envtest suites for the controller, migration, status, env and helpers (`internal/controller/*_test.go`,
about 4,900 lines) and a Kind end-to-end suite (`test/e2e/e2e_test.go`): deploy with Postgres, deletion, custom ports,
limits, the management API, bootstrap detection.

**What it does not manage.** Postgres, Vault, OpenFGA, TLS issuance ("validates references but does NOT create",
`docs/architecture.md` § External Dependencies); projects, warehouses, namespaces, users, roles and grants (planned).

**The server side of an operator.** Lakekeeper's model gives the machine that provisions inside the catalog the
strongest role there is: "Operators are more powerful than administrators - they are the most powerful role in
Lakekeeper. The operator role allows usage of all APIs inside the catalog. It is designed to be used by machines that
provision resources inside Lakekeeper, i.e. a `KubernetesOperator`" (`lakekeeper` `08b67d7`
`authz/openfga/v4.15/components/server.fga:15-18`). rask's model is narrower: `estate#operator` yields only
`can_create_project` (`packages/lakehouse/src/lakehouse/governed/auth/model.fga:138`, `:148`).

### 2.2 Apache Polaris: no first-party operator

**The evidence.**

- `apache/polaris` at `be8167fe0` holds no operator code: `git grep -i -E "kubernetes operator|k8s operator|operator
  sdk|CustomResourceDefinition|josdk|operator-framework"` matches nothing, and no commit message on any branch mentions
  a Kubernetes operator (`git log --all -i --grep=operator`: Ranger, SQL and STS commits only). Its Kubernetes
  deployment directory is `helm/polaris` alone.
- The Apache organisation has three Polaris repositories: `polaris`, `polaris-tools` and `terraform-provider-polaris`
  (GitHub API, `orgs/apache/repos`). `polaris-tools` at `823dddd` holds `apprunner`, `benchmarks`, `console`,
  `iceberg-catalog-migrator`, `mcp-server` and `polaris-synchronizer`, and no operator.
- `apache/polaris` issues and pull requests searched for "kubernetes operator", "operator" in the title and "CRD"
  return no operator proposal (the one hit is #1269, "Hosted helm chart in a Helm Repository").
- `apache/terraform-provider-polaris` has one commit (`abb1950`, "initial commit", 2026-08-25) and a README that says
  "This repository is being bootstrapped and does not yet contain a usable provider. There are no releases"
  (`README.md`).

**The first-party Kubernetes tooling: the Helm chart** (`helm/polaris`, chart version 1.9.0-SNAPSHOT). Templates: a
ConfigMap, the Deployment, Services, HPA, PDB, Ingress, Gateway API routes, a ServiceMonitor, a log PVC, and
`cronjob-maintenance.yaml`, which renders admin-tool CronJobs (the one defined, `nosql-maintenance`, runs `nosql
maintenance-run` daily and is off by default, `values.yaml:1288-1298`). What the chart does not do:

- **Realm bootstrap is by hand.** "When installing Polaris for the first time, it is necessary to bootstrap each realm
  using the Polaris admin tool", with a `kubectl run polaris-bootstrap ... bootstrap -r polaris-realm1 -c
  polaris-realm1,root,$ROOT_PASSWORD` that reads the database password out of a Secret into env on the command line
  (`site/content/in-dev/unreleased/helm-chart/production.md:161-199`).
- **Schema migration is a person's.** "Polaris has no automated schema migrations" (`site/content/downloads/1.7.0/index.md:57`);
  "A database administrator creates the schema. Polaris does not issue `CREATE SCHEMA`"
  (`site/content/in-dev/unreleased/metastores/relational-jdbc.md:165-171`).
- **Catalogs, principals, roles and grants** are created through the management API
  (`spec/polaris-management-service.yml`); no first-party tool declares them from Kubernetes or Terraform today.

### 2.3 Secondary, third party: Canonical's Polaris charm (`canonical/polaris-k8s-operator` `90c45e4`)

Read only as the most-used third-party operator with code. The other repositories named "polaris-operator" on GitHub
hold a README or a Helm chart and no controller (`da3mon-01/polaris-operator`, `mmgaggle/polaris-operator`, each 0
stars). Canonical's is a Juju charm ("Charmed K8s operator for Apache Polaris to be deployed and managed by Juju"),
Python on the `ops` framework, 14 commits, no GitHub release, published to Charmhub track `1/edge`
(`.github/workflows/release.yaml:9`, `:22-27`). It defines no CRD: its "resources" are Juju relations to Postgres, S3,
OAuth, TLS, ingress and observability (`metadata.yaml:49-98`) and one action, `status-detail` (`actions.yaml`).

- **Reconciles** the Polaris container's config file and environment, and restarts it on change
  (`src/managers/polaris.py:70-125`).
- **Bootstraps** the realm on every leader update with the admin tool, which it calls idempotent from 1.7.0
  (`src/managers/polaris.py:104-117`; `src/core/constants.py:46`).
- **Calls the management API** only to reset the root principal's credentials, authenticating as that root principal
  with client id `charmed-operator` and the current password (`src/managers/polaris.py:36-68`; `src/events/polaris.py:216-274`).
- **Credentials**: the root password from a Juju secret or generated and kept in the peer application databag
  (`src/events/polaris.py:252-260`; `src/core/models.py:436-444`); the database password, the root credential and the
  S3 secret key written into the server's properties file (`src/config/polaris.py:24-50`, `:110-118`), and the S3 key
  pair also as `AWS_*` environment variables (`:56-74`). With OAuth related, every token's roles map to
  `PRINCIPAL_ROLE:ALL` (`src/config/polaris.py:101-104`).
- **Does not manage** catalogs, principals, roles or grants (no call other than the credential reset in `src/`).

### 2.4 What each operator owns

| Operator | Resource kind | What it reconciles | State or intent | Credentials path | Deletion semantics |
| --- | --- | --- | --- | --- | --- |
| Lakekeeper | `Lakekeeper` (v1alpha1) | Deployment, main and metrics Services, a migration Job per migration-relevant spec hash; bootstrap observed, optionally performed | Intent: the server's config. State stays in Postgres; `serverID` and `bootstrappedAt` copied into status | Reads every Secret cluster-wide to validate; values to the pod as `secretKeyRef` env; TLS as mounted files; calls the API unauthenticated | Finalizer removes itself; owner references delete Deployment, Services and Jobs; database untouched |
| Lakekeeper (planned) | `LakekeeperProject`, `LakekeeperWarehouse`, `LakekeeperNamespace`, `LakekeeperUser`, `LakekeeperRole` | Not written | Not written | Not written ("go-lakekeeper SDK") | Placeholder comment only ("we would clean up external resources here") |
| Polaris (first party) | none: Helm chart | Deployment, Services, HPA, routes, maintenance CronJobs | Intent: the server's config; realm bootstrap and schema are manual | Chart values reference Secrets; bootstrap by hand with the password in `kubectl run` env | Helm uninstall; nothing in the database |
| Polaris (Canonical, third party) | Juju charm, no CRD | Container config and restart, realm bootstrap on each leader update, root credential rotation | Intent from Juju config and relations; state in Postgres | Juju secrets and peer databag; written into a properties file and `AWS_*` env; API called as the root principal | Juju removal; nothing in the catalog |

## 3. rask

### 3.1 The imperative steps an install from git runs, and what a controller would add

Under Argo CD every bootstrap Job is a hook that each sync runs again (ADR 0196 decision 1), and Argo CD leaves hooks
out of the state it compares (ADR 0196, Context, `pkg/sync/reconcile.go` `splitHooks`). So a Job's targets converge on
each sync and drift unseen between syncs; a controller would add continuous drift correction and a status. Whether that
is worth code depends on the target:

| Job or hook | Where | What it does | Drift between syncs possible? | Verdict |
| --- | --- | --- | --- | --- |
| `openfga-migrate` | `chart/templates/openfga-migrate.yaml:11-17` (Argo `Sync`), command at `:42-65` | Waits for the AGE Postgres, runs `openfga migrate` | No: a schema migration is per version | Keep the Job. It is the same pattern Lakekeeper's operator implements as a controller-created Job; a controller would run the same command. Its DSN arrives through `secretKeyRef` env (`:63`), the zero-trust gap XC-002 already names |
| NATS stream Job | `chart/templates/nats-stream-job.yaml:1-7` (Argo `Sync`), `converge_stream` at `:66-76` | Creates or edits each declared stream, consumer and bucket to the chart's config, as the bus `admin` user from a mounted creds file (`:20-40`) | Yes: a stream edited or removed by hand stays so until the next sync | Replace with the platform's controller, not rask's. NACK's `Stream` CRDs are enforced continuously ("configuration state will be enforced if mutated by an external client", `nack/README.md:37`), with `preventDelete` (`nack/deploy/crds.yml:313-316`, default false, so it must be set true). Keep the Job where the platform runs no NACK (docs/OPERATORS.md § 3) |
| `object-store-provisioning` and its prune | `chart/templates/object-store-provisioning.yaml:1-16` (Argo `Sync`, prune `PostSync`), args `:49-75` | Creates buckets and each workload's canned policy on the development store with the root key; the prune removes what git does not declare (`scripts/rustfs_admin.py:9`, `:213-241`) | Yes, on the dev store | Keep. It runs only with `objectStore.enabled`, the development fixture; a platform store's owner provisions production (docs/OPERATORS.md § 8). An operator doing it would hold the store's root key |
| `openfga-model` | `chart/templates/openfga-model.yaml:1-12`, command `:50` | Writes the image's model unless the store holds its canonical body (`packages/lakehouse/src/lakehouse/governed/auth/write_model.py:108-120`) | No: a model is immutable and found by content; the catalog also writes it at boot | Keep |
| `bootstrap-admin` | `chart/templates/bootstrap-admin.yaml:1-10` (Helm `post-install,post-upgrade`, which Argo CD maps to PostSync) | Writes the first admin's `admin` on `estate:rask` and the release's own service rungs straight to OpenFGA (`:166-188`), and revokes a machine role held by one of the release's own accounts that it no longer declares (`:190-222`) | Yes: a tuple removed at the estate tuple door stays removed until the next sync | Keep, as the trust root. It is the only writer that must act before any door can admit anyone, as Lakekeeper's bootstrap is. Re-asserting it continuously would need an estate-admin credential in a long-running pod |
| `openbao-seed` | `chart/templates/openbao-seed.yaml:1-12`, consumed by the openbao fixture (`deploy/fixtures/openbao/chart/values.yaml:19`) | Mints and seeds signing pairs, the NATS root and users, and generated credentials on the development store | Already continuous: it re-reads its inputs every 5 s (docs/OPERATORS.md § 8) | Keep; a development fixture, not production |
| Backup CronJobs | `chart/templates/backup-control-root.yaml:3`, `backup-pg.yaml:6`, `backup-snapshot.yaml:42` | Scheduled copies | Not provisioning | Keep |

**Answer.** For the first scope a rask operator replaces none of these Jobs. Each is idempotent and ordered by its own
wait, and each target is per version (migrate, model), a development fixture (store, seed), or a trust root (bootstrap).
A controller over them is more code without more convergence, and for the store and the trust root it would hold the
two most privileged credentials in the estate. The streams are the one target with real drift, and NACK, the
platform's own NATS controller, closes it. docs/OPERATORS.md § 2's first bullet ("a reconciler converges them
continuously") and ADR 0196's last consequence (production provisioning of the store's policies, the bus permission
table and the signing pairs "is the planned operator's") lose to the zero-trust fact on this reading: reconciling
those needs the platform store's admin, the bus's account signing key and write access to the secret store. Reopening
either is the owner's.

### 3.2 Should catalog-level objects be custom resources?

**What the references do.** Neither does it. Lakekeeper's operator lists the CRDs as planned and its delete path is a
placeholder (§ 2.1); Polaris has no operator and an empty Terraform provider (§ 2.2); Canonical's charm creates nothing
in the catalog (§ 2.3). Lakekeeper's server reserves the most powerful role for the provisioning machine (§ 2.1). The
nearest working precedent for an external object owned through a CR is NACK: a stream is "expected to _exclusively_ be
managed by NACK" (`nack/README.md:37`), and deleting the CR deletes the stream unless `preventDelete` is set
(`nack/deploy/crds.yml:313-316`).

**rask's doors already exist and are idempotent.** Project create (`services/catalog/src/catalog/api/v1/endpoints/projects.py:216`,
a re-POST converges, `:244-256`), warehouse create and lifecycle (`warehouses.py:164`, `:578`, `:601`, `:665`, `:1153`),
grants on project, warehouse, namespace, table and classification (`access.py:500-645`), quality gates
(`gates.py:59`, `:97`, `:115`) and maintenance policies (`policies.py:133-294`). Each door is audited and emits a
control event.

**Failure modes, and the rule that answers each.**

| Failure mode | What goes wrong | Rule |
| --- | --- | --- |
| Two writers | The operator and a person both change one warehouse or grant through the doors; each undoes the other, or the person's change survives until the next reconcile | A record the operator created carries `managed_by: operator` (`docs/architecture/lakekeeper-control-plane-findings.md` finding 2.2); its spec doors refuse every caller but the operator principal named in git, and the refusal is audited. Below the managed object (namespaces, tables, grants on them) the doors stay its grant-holders' |
| A CR deleted with data behind it | `kubectl delete`, an Argo CD prune or a removed file deletes a warehouse and its tables | A CR carries `deletionPolicy`, default `Retain`: removing the CR stops management and clears the marker, and the catalog record and data stay. `Delete` is explicit per object and calls the catalog's own delete door, which still refuses a protected or non-empty warehouse with 409 unless `force` (`warehouses.py:1178-1187`, `:1215`); the operator never sends `force`. Data goes through trash and its grace as any drop does |
| Drift between the CR and the catalog's record | The CR says one thing, the catalog another; or the operator caches ids that the catalog has since replaced | The catalog's record is the state of record. The operator reads it back through the door on every reconcile and reports the difference as a condition; status holds only what the door returned (ids, `observedGeneration`, conditions), never a copy of state |
| Secrets in a CR spec | A storage key or client secret in `spec` lands in etcd, in git, in Argo CD's cache and in every `kubectl get -o yaml` | No CR field holds a credential. rask needs none: a warehouse names a bucket and the catalog reaches it with its own STS session (ADR 0170, ADR 0197); a grant names a principal key |
| The operator as a superuser | One compromised pod writes any tuple or reaches data | The operator calls the doors as its own ServiceAccount identity, never the object store or OpenFGA directly, holds `estate#operator` (`can_create_project` only) and whatever its own creates give it, and holds no data relation (§ 3.3) |

**Per object.**

- **Project and warehouse: yes.** They are the tenant and binding levels above the namespace spec, configuration rather
  than data, and an install from git that cannot recreate them is not an install from git (ADR 0199, the open question
  "whether projects, warehouses and grants become resources declared in git"). Prerequisites: LH-076 (#128), because the
  project door checks `can_observe_events` (`projects.py:235`) where the model's `can_create_project` is `admin or
  operator` (`model.fga:148`); and the `managed_by` marker.
- **Grants: yes, for a principal that is not a person.** This is the direction ADR 0199 records for ADR 0192 ("grants
  declared in git and converged by the planned operator, the doors kept for people"). A grant CR names an object, a
  rung and a principal key and calls that object's grant door; on removal it revokes only a tuple it wrote, recorded in
  its status, so a person's grant of the same rung at the door is never taken by a CR's deletion. Revoking on removal
  is the default for grants, because a grant holds no data and a removed declaration that left access in place would
  break the zero-trust fact.
- **Quality gates and maintenance policies: later.** They are a project's and a table's governance settings with
  idempotent set and delete doors, so they fit the same pattern; they wait until projects and grants have proved it.
- **Namespaces and tables: never.** They are created by a user's pipeline through the lance-namespace doors
  (`CreateNamespace`, `CreateTable`, `DeclareTable`; `lance_docs/ns_catalog/spec.yaml`). A CR per table would make the
  operator a second, non-spec writer of the data plane.

### 3.3 Zero trust: the operator's minimum identity and RBAC

**Kubernetes RBAC.** A namespace-scoped manager (kubebuilder `--namespaced`, `WATCH_NAMESPACE`,
`docs/book/src/reference/manager-scope.md:44-53`) with one Role in the release's namespace:

- `get;list;watch;update;patch` on its own CRDs, `update;patch` on their `/status` and `/finalizers`;
- `create;patch` on `events`; `get;create;update` on `coordination.k8s.io/leases` for leader election;
- **nothing else**: no Secrets, no Deployments, Services, Jobs or ConfigMaps (the chart owns them), no ClusterRole.

**Catalog identity.** Its own ServiceAccount, with a projected token for the catalog's audience, verified by the
catalog as `kubernetes~system:serviceaccount:<ns>:<sa>` exactly as every service is (ADR 0083). It holds
`estate#operator` (written by the bootstrap hook from git, so the authority is itself declared), and on each project
it creates, the `admin` the create door seeds (`projects.py:257`, `fga_deps.seed_project_admin`), which carries grant
authority inside that project only. A grant the operator converges on an object it did not create needs that object's
grant authority given to the operator at the door by its holder.

**What it must never hold:** the object store's root key or any store session (it reads no data); an OpenFGA token
(`rask-openfga`) or estate `admin`/`owner` (the raw tuple door, `access_admin.py:370-389`, stays the estate admin's);
the bus's admin or account credentials; the secret store's write access; a Secret `get` anywhere; any data rung
(`reader`, `writer`, `maintainer`) on a warehouse, namespace or table.

### 3.4 Language and framework

**What each reference uses.** Lakekeeper's operator: Go, operator-sdk v1.42.2 on Kubebuilder v4 (`README.md`,
Technology Stack). Polaris: no operator; the server is Java on Quarkus. Canonical's Polaris charm: Python on Juju's
`ops` framework, which runs only under Juju.

**The maintained options, from their own docs.**

| Framework | What it is | What it implies for rask |
| --- | --- | --- |
| kubebuilder (Go, controller-runtime; release v4.16.0, 2026-09-10) | "a framework for building Kubernetes APIs using CRDs", built on controller-runtime and controller-tools; Operator SDK uses it as a library (`README.md:14-31`). Level-triggered `Reconcile`; CRD schemas, CEL rules and RBAC generated from Go markers; envtest runs a real API server and etcd in tests (`docs/book/src/reference/envtest.md:1-4`); namespace-scoped managers supported (`manager-scope.md:17-53`) | Go is already in the repository as the Dagger module (`.dagger/go.mod`, go 1.26.1), not as product code. A Go operator is a third product plane with its own gates (golangci-lint, `go test` with envtest) to add to `make check` and the Dagger lanes. The catalog's doors are described by `docs/catalog-openapi.json`, from which a client can be generated |
| kopf (Python; 1.45.2, 2026-10-23) | "production-ready and stable (semantic v1)"; "There is no active development of new major functionality ... This piece of art is finished"; PRs reviewed by one maintainer (`README.md:24-38`). Default triggering is edge-based on diffs, which "fully excludes the actual state" (`docs/reconciliation.rst:20-36`); level-based reconciling is built from timers or daemons (`:80-100`); the diff base is kept in an annotation on each object (`docs/continuity.rst:19`). Namespaced with `--namespace` (`docs/scopes.rst:8-13`); its sample RBAC lists CRDs and namespaces cluster-wide and patches admission webhooks (`docs/deployment-rbac.yaml:15-30`) | One language with the services, the uv workspace, ruff, ty and pytest. No CRD or RBAC generation: schemas are hand-written YAML. Drift correction against the catalog needs a timer per object. One maintainer is a supply-chain and bus-factor risk the project names itself |
| kube-rs (Rust; 5.0.0, 2026-11-01) | A Rust client "in the style of a more generic client-go, a runtime abstraction inspired by controller-runtime, and a derive macro for CRDs inspired by kubebuilder", a CNCF Sandbox project (`README.md:9`) | A language rask does not use anywhere |
| Metacontroller (Go add-on; v4.17.6, 2026-10-15) | Write a webhook "in any language that receives the current state as JSON and returns the desired state as JSON" (`README.md:15-20`) | A cluster add-on (another prerequisite), and its shipped ClusterRole is `*` on every group, resource and verb (`manifests/production/metacontroller-rbac.yaml:8-17`), which breaks the zero-trust fact. Its model computes children objects, not calls to an external API |

**Recommendation: kubebuilder, namespace-scoped.** The operator's whole job is level-triggered reconciling of an
external API with status conditions, which is controller-runtime's model and needs timers in kopf; its CRD schemas,
CEL validation and least-privilege Role come from the same markers; envtest gives the real-collaborator integration
tests rask's testing rules ask for; it is maintained by a Kubernetes SIG rather than one person; and it is what the
reference uses. The cost is Go in the product, which is an owner decision because it adds a plane to the two the
repository keeps; kopf is the choice that keeps one language, at the price of hand-written schemas and timer-driven
drift correction.

### 3.5 A minimal first scope

**Shipped as its own release** (ADR 0153: operators, CRDs and compute go in their own releases), its CRDs in that
release with `helm.sh/resource-policy: keep`, its image built by Dagger.

**CRDs (namespaced, `v1alpha1`):**

1. **`LakehouseProject`**: `spec.id`, `spec.protected`, `spec.deletionPolicy` (`Retain` default, or `Delete`).
   Reconciles through `POST /management/v1/projects` (idempotent) and the project protection door; marks the record
   `managed_by: operator`; reads it back each period and sets `Ready` / `Drifted` conditions with `observedGeneration`.
2. **`LakehouseWarehouse`**: `spec.project`, `spec.id`, the bucket binding, `spec.active`, `spec.protected`,
   `spec.trashGraceDays`, `spec.deletionPolicy`. Reconciles through the warehouse doors (`warehouses.py:164`, `:578`,
   `:601`, `:665`); `Delete` calls `DELETE /management/v1/warehouses/{id}` without `force`, and a 409 is reported as a
   condition, never retried with force.
3. **`LakehouseGrant`**: the object (project, warehouse, namespace or table, by id), the rung, the principal key.
   Reconciles through that object's grant door; on removal revokes the tuple it wrote through the revoke door.

Each reconcile authenticates with the operator's projected token, treats an idempotent door's 200 or 409-converged as
converged, and records in status only what the door returned.

**Prerequisites in the catalog:** LH-076 (#128), so the project door checks `can_create_project`; the `managed_by`
marker and its guard on the six spec doors of a project and a warehouse; the bootstrap hook granting `estate#operator`
to the operator's ServiceAccount when the operator is enabled.

**Explicitly out:**

- rendering any Deployment, Service, Job, ConfigMap or Secret of the lakehouse (the chart's, under Argo CD);
- the provisioning Jobs of § 3.1, and the platform's provisioning (store policies, bus users and permissions, signing
  pairs, secrets): the platform's operators own those, attested in values;
- the authorization model, OpenFGA's schema and the bootstrap tuples (the hooks', the trust root);
- namespaces, tables, data, compaction, index builds and maintenance runs (pipelines' and maintenance's);
- quality gates and maintenance policies until the first three CRDs have run on the estate;
- any CR field that holds a credential, and any Secret read.

**Four facts.** GitOps: met, projects, warehouses and grants become what git declares and converge continuously, with
drift reported as a condition. Cloud native: met, a declarative workload with status, leader election and its own
release. Lance-namespace-idiomatic: met, namespaces and tables stay the spec's doors, and the operator touches only
rask's levels above them. Zero trust: met, one namespaced Role with no Secret access, a projected token, a narrow
catalog rung, and no store, bus or OpenFGA credential.

## 4. Not confirmed

- **Whether Lakekeeper's migration Job is ever judged failed.** `RestartPolicy: OnFailure` (`lakekeeper_controller.go:1703`)
  with `job.Status.Failed >= 3` (`:1641`): I did not run a failing migration to see whether `status.failed` reaches the
  backoff limit or only the Job's `Failed` condition is set. The Kubernetes Job documentation was not read for this.
- **The unmerged branch of Lakekeeper's operator** (`patrick/vak-448-improve-the-upgrade-path-for-the-lakekeeper-operator`)
  was not read; only `main` was.
- **Whether a Kubernetes operator for Polaris is discussed on `dev@polaris.apache.org`.** I searched the repositories,
  their issues and pull requests and the site docs, not the mailing-list archive.
- **Whether Canonical's charm stores the root password as a Juju secret or in plain peer data.** `set_admin_password`
  updates the peer application databag (`src/core/models.py:443-444`); whether the data-platform library behind it
  routes that field to a Juju secret was not traced.
- **What NACK's `nkey` field on a `Stream` holds** (`nack/deploy/crds.yml:309-312`, "NATS user NKey"): a seed in the
  spec or a reference. NACK's `Account` takes creds from a Secret (`:1083-1094`). Settle it before rendering NACK
  objects, since a seed in a spec breaks the zero-trust fact.
- **Whether NACK's control-loop mode can manage a stream it did not create** without first deleting it, and how it
  behaves on rask's mirror stream `CATALOG_CONTROL_BROADCAST`. Not read.
- **Whether Argo CD v3.5.7 reports a custom resource's `Ready` condition as its health without a Lua health check.**
  Not read in Argo CD's source; it decides whether a sync wave can wait on a `LakehouseWarehouse`.
- **The grant doors' idempotence.** I read that project and warehouse create converge on a re-POST; I did not read
  whether every grant and revoke door answers a repeated call as success, which the `LakehouseGrant` reconcile assumes.
- **How a revoke-on-removal would tell its own tuple from a person's identical one.** An OpenFGA tuple holds no writer;
  the status record of "tuples this CR wrote" is the proposal, and whether the catalog should also keep a managed-grant
  record was not designed.
- **The framework comparison is from each project's own README and docs**, not from building an operator in each.
