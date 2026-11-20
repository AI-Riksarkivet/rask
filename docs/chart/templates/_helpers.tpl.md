# chart/templates/_helpers.tpl

Rationale for `chart/templates/_helpers.tpl`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- define "rask.name" -}}`

=====================================================================================================
MERGED HELPERS — rask (base) + lance-ns (grafted 2026-07-27, lance-ns main@083b49a).

Both charts install as ONE release (`helm install rask ./chart`, values `fullnameOverride: rask`), so this
file carries BOTH named-template sets. A Go template `define` is last-one-wins and SILENT, so the merge
rule here is mechanical: rask's helpers keep the `rask.` prefix, lance-ns's keep the `lance.` prefix, and
nothing is defined twice. Verified at merge time — the two sets are disjoint (no lance-ns template was
renamed; every `include "lance.*"` in the grafted templates resolves to the original definition).

NAMING CONTRACT (the one thing that makes the two planes agree):

    include "rask.fullname"  → .Values.fullnameOverride            → "rask"
    include "lance.fullname" → .Release.Name                       → "rask"

They render the SAME string only because the release is named `rask` AND `fullnameOverride: rask`. That is
deliberate (ADR 0108: lance object names become `rask-*`), and `lance.fullname`
is kept on `.Release.Name` rather than delegated to `rask.fullname` because ~40 grafted lance templates
build sibling object names from a RAW `{{ .Release.Name }}-…` (the `-openbao`
Service, for one). Delegating would
move the Deployments but not those, splitting the estate the moment release name ≠ fullnameOverride.
Install under any other release name and the two planes diverge — keep them equal.

LABELS: `rask.labels` emits app.kubernetes.io/name=rask, `lance.labels` emits =lance-ns. Kept distinct on
purpose: rask-labelled pods (the console, the OpenFGA hooks, the issuer mirror) carry {name,instance,component}
while lance Services select on {component,instance} only (services.yaml). Different `name` values are what
guarantee a rask selector can never adopt a lance pod inside the shared release.
=====================================================================================================

---------------------------------------------------------------------------------------------------
rask: the base chart's helpers (names, labels and the cluster route).
---------------------------------------------------------------------------------------------------

## `{{- define "rask.componentLabels" -}}`

Component labels: pass (list . "<component>")

## `{{- define "rask.otelResourceAttrs" -}}`

THE single OTel resource identity for every pod in this chart.
Call: {{ include "rask.otelResourceAttrs" $root }}

ONE derivation, because two had drifted. The fleet emitted
`service.namespace=rask,deployment.environment=<Release.Namespace>` while the lakehouse and Ray
emitted `service.namespace=lance-ns,deployment.environment.name=<observability.environment>,
service.version=<chart>` — ZERO key overlap, so any cross-plane filter, join or dashboard variable
silently saw one half of the estate. Measured 2026-08-23: GreptimeDB had already materialised both
as separate physical columns in `opentelemetry_traces`, with 265,978 of 347,924 spans in a 3-hour
window invisible to a query written against the other name.

`deployment.environment` was RENAMED to `deployment.environment.name` in OTel semconv v1.27.0 and
the old key is marked deprecated. Only the new key is emitted — NOT both. A dual-write is a
permanent band-aid with no removal trigger, it breaks nothing here (nothing in this repo queries
the old name), and it would not close the seam anyway: the historical column already exists.

`service.version` is the CHART version, not the running image tag — it answers "which chart
rendered this pod", not "which build emitted this span".

## `{{- define "lance.fullname" -}}{{ .Release.Name }}{{- end -}}`

---------------------------------------------------------------------------------------------------
lance-ns — grafted verbatim from lance-ns main@083b49a chart/templates/_helpers.tpl.

Every one of these is referenced by a grafted lance template (services.yaml, maintenance.yaml,
object-store-provisioning.yaml, object-store-identities.yaml, openbao-seed.yaml, age-postgres.yaml,
network-policy.yaml, ha.yaml, …). None of them
was renamed: no lance name collided with a rask name.

VALUES THEY REQUIRE (the values.yaml merge must land these or the render breaks):
  age.externalHost · auth.enabled · catalog.controlEmit ·
  auth.oidc.{issuer,audience} · dex.{enabled,port} · image.catalog.{repository,tag} · lifecycle.preStopSeconds ·
  nats.url ·
  observability.{enabled,environment,export.endpoint,identity.audience} · openbao.{enabled,port,externalAddr} · pubsub.topic ·
  resources.{default,<component>} · objectStore.{bucket,port,externalEndpoint} · security.readOnlyRootFilesystem ·
  services.{catalog,lineage}.{port,appId} · services.lineage.relay.periodSeconds
`objectStore.bucket` (singular) is the one that is easy to lose: rask's values ship `objectStore.buckets` (a
LIST) and lance's templates read `objectStore.bucket` (a STRING) — both must exist.
---------------------------------------------------------------------------------------------------

Release name is the fullname (install as `helm install rask ./chart` → all names = rask-*).

## `{{- define "rask.bootstrapRev" -}}r{{ .Release.Revision }}{{- end -}}`

---------------------------------------------------------------------------------------------
BOOTSTRAP JOBS — the `helm install --wait` deadlock fix (live-proof 2026-07-28, defect 1).

A Job that another release resource must wait for IN ORDER TO BECOME READY cannot be a
post-install hook. helm's order is: pre-install hooks → apply manifests → (--wait) block until
every resource is Ready → post-install hooks. So `--wait` blocks on OpenFGA, OpenFGA cannot
start against an unmigrated schema, and the migration is queued BEHIND the wait. Circular; revs
1 and 2 died on "context deadline exceeded" and the OpenBao seed never fired, which is the only
reason scripts/e2e_stack.sh exists (it drops --wait and re-sequences by hand).

Moving the hook EARLIER does not fix it: a `pre-install` hook runs before ANY release manifest
exists, so the migrate Job would wait for an AGE Postgres that has not been created yet — the
same deadlock, one phase further left, and now it also blocks every OTHER resource.

The fix is to take those four Jobs OUT of the hook lifecycle entirely and apply them in the same
wave as everything else. `--wait` then waits for the Job to Complete *concurrently* with waiting
for the servers to become Ready, and the dependency resolves itself: the migrate pod's wait-age
init loops until the AGE StatefulSet (applied in the same wave) answers, migrates, exits; OpenFGA
stops crash-looping and goes Ready; `--wait` returns. No wrapper script, no ordering knowledge
outside the chart.

Work that depends on the APPS instead (the estate's first grants need a booted catalog) is the catalog's own: its
declaration converge grants them once it serves, and nothing waits on it.

A plain Job's spec is IMMUTABLE, so the name carries the release revision: an upgrade renders a
new name (new Job, re-runs — the same semantics `before-hook-creation` gave the hooks), helm
deletes the previous revision's Job because it left the manifest, and a re-render of the same
revision is byte-identical (no patch, no immutability error).
---------------------------------------------------------------------------------------------

## `{{- define "lance.reconciledJob" -}}`

A BOOTSTRAP JOB CONVERGES UNDER A GITOPS RECONCILER TOO ([[XC-164]]). Argo CD renders the chart with `helm template`, so
every sync is revision 1 and an install: a revision-named Job is always `…-r1`, its TTL reaps it, an auto-sync re-creates
it as drift, and a changed input meets the Job's immutable pod template. Each bootstrap Job therefore names the Argo CD
phase it runs in (`argocd.argoproj.io/hook`): as a hook it is not part of the desired state Argo CD diffs, so a reaped Job
is no drift, and `BeforeHookCreation` replaces it on every sync, which re-runs its idempotent work on each one. Argo CD
reads its own hook annotation in place of Helm's on the same object (gitops-engine `pkg/sync/hook/hook.go` `Types`, read
at Argo CD v3.5.7), so Helm still runs these Jobs as before. No sync wave orders them
after the ExternalSecrets they mount: a wave is a health gate (gitops-engine `sync_context.go`: the next wave waits for
this one to be Healthy), and those secrets come from a store the same release seeds, so a gate before them would hold the
first sync forever; each Job's own wait (an init container, a retrying script) orders it, as under Helm. Re-running a Job
only when its inputs change is [[XC-160]]'s.

## `{{- define "lance.fgaMaintainers" -}}`

The identities the release declares as maintainers, read by the catalog alone (`LANCE_FGA_MAINTAINERS`): it grants the rung
on every warehouse it mints and, at boot, on every one it holds, and its declaration converge grants it on the estate's
own warehouse, which has no record ([[XC-011]]); each takes back the rung from an own account it no longer names. One
source keeps the grants from disagreeing, which would make one grant what the other revokes on every reconcile ([[XC-164]]).

## `{{- define "lance.devCredentials" -}}`

ONE GENERATED SECRET PER DEV CREDENTIAL (owner, 2026-11-07, [[XC-164]]): `<release>-dev-credential-<key>`, each created
once by ESO's Password generator and kept. ESO generates a Secret's whole key set at once and refreshing it would replace
every password the stores were initialised with, so a key added to one shared Secret would never reach it. As its own
Secret a new credential is a new object, generated on the next reconcile, and the ones the stores hold are never
generated again. The dev OpenBao fixture mounts them as one directory (its `devCredentials`), and the seed reads the
keys this lists.

## `{{- define "lance.replicaBounds" -}}`

The fewest and most replicas a request-serving service runs: an autoscaler's `minReplicas` and `maxReplicas` while
`autoscaling.enabled`, else its own `replicas` for both. The PodDisruptionBudget reads the fewest, and the catalog's
and lineage's Deployments render the fewest as their `replicas`.

## `{{- define "lance.rollout" -}}`

How a request-serving Deployment rolls: no pod is taken away before the pod replacing it is Ready
(`maxUnavailable: 0`), and a quarter more than its replicas may run meanwhile (`maxSurge: 25%`, at least one).
Kubernetes' default lets a quarter of the replicas be down at once, which at one replica rounds to none and at four is
a pod's worth of requests refused on every upgrade ([[LH-483]]). The catalog, lineage, the console and the issuer mirror
render it; the maintenance worker keeps its own, a surge of exactly one, since each extra worker
takes tasks. None of them mounts a volume or a host port only one pod can hold, so a surge pod always schedules.

## `{{- define "rask.isRealDeployment" -}}`

IS THIS A REAL DEPLOYMENT? One definition, because two are two chances to answer it too narrowly at
once — the argument `prod-credentials.yaml` makes for itself, shared with `route.yaml`.

BOTH SIGNALS ARE FACTS THE DEPLOY ALREADY DEPENDS ON, never a flag someone must remember to arm. A
guard reachable only by remembering an opt-in protects the installs that did not need protecting.
`openbao.enabled=false` (the platform's secret store) is the explicit prod signal; failing that, images pulled from a registry off
this node means the deployment is not the local loop — `make k3s-up` renders `localImages=true` for
side-loaded images, and the three dev-registry addresses are unroutable from anywhere but the node.

Answers the string "true" or "" so a caller can use it in a `ternary` or an `if`.

## `{{- define "lance.provenanceNamespace" -}}`

The namespace the catalog keeps for provenance, rendered into both the catalog and lineage so the two cannot name
different ones. Every name's first segment is its warehouse, and the provenance namespace belongs to the estate's own
warehouse, `lance_catalog` (the catalog's `RASK_FGA_DEFAULT_WAREHOUSE_OBJECT`, which the chart leaves at its default,
as the catalog's declaration converge does). The catalog refuses to boot on a namespace outside it, so the
render refuses one first, naming the value, rather than leaving a pod to crash-loop on it.

## `{{- define "lance.ageHost" -}}{{ .Release.Name }}-age{{- end -}}`

In-cluster hostnames (subcharts derive their service name from the release).

## `{{- define "lance.objectStoreHost" -}}{{ include "rask.fullname" . }}-rustfs{{- end -}}`

The ONE object store's S3 host — the Service in front of the rustfs fixture's StatefulSet ([[XC-131]]).
P4's store unification (docs/adr/0124-chart-unification-one-control-plane-each-one-object-store.md) still holds ("the ONE store"); what changed with LH-133 is the store and
therefore its Service name, since a first-party StatefulSet names its own Service
to name is deleted, so the lakehouse and observability planes resolve the same endpoint.

## `{{- define "lance.catalogImage" -}}`

The lakehouse image (catalog, lineage, maintenance, and the chart's Jobs and hooks)
— every one of them runs the SAME image with a different entrypoint. A client runs its own image.

It DELEGATES to `rask.image`, which is the estate's one image contract: registry prefix, digest
pinning, and the side-loaded (`image.localImages`) form. This helper used to render
`printf "%s:%s" repository tag` on its own, which honoured NONE of them — so eleven containers came
out as the bare `lance-rest-catalog:dev`, i.e. Docker Hub, on a chart that had been configured with a
registry and a digest. That is not a cosmetic drift: it is why every deploy needed `kubectl set image`
fix-ups afterwards, and why a GitOps reconciler could never own this release.

`image.catalog.repository` survives as an OPTIONAL per-component name override (the component is not
named after the chart), and the tag/digest/prefix rules are now whatever the rest of the estate uses.

## `{{- define "lance.s3Endpoint" -}}`

CONSUMER endpoints — return the EXTERNAL override when set (the in-cluster component is then usually
disabled, e.g. a managed S3 / Postgres / Vault in prod), else the in-cluster address. The
component's OWN Service/StatefulSet keeps the plain *Host helper above; only the apps that CONNECT switch.
This is what makes the docs/DURABILITY.md tier-3 externalization real (values-prod.yaml sets the overrides).

THE S3 ENDPOINT HAS NO SILENT DEFAULT: with `objectStore.enabled=false` and no `externalEndpoint` the render fails.
The in-cluster address names the dev store's Service, so an estate without that store would otherwise dial a host
nothing serves; the S3 behind a real estate is the platform's, and the chart only names it.

## `{{- define "lance.otlpEndpoint" -}}`

The OTLP/HTTP endpoint every lakehouse service exports to: `observability.export.endpoint`, the platform's gateway
(ADR 0153, ADR 0215). `required`: the chart renders no Collector of its own, so a telemetry-on render that names no
gateway would leave every service exporting into nothing; it fails instead.

## `{{- define "lance.otlpAudience" -}}`

`observability.identity.audience` (default `rask-otlp`), the audience of the projected ServiceAccount token each
service presents on every export. `required`, because the gateway admits a token by its audience: an empty one would
project a token the gateway refuses. The mount path and `RASK_OTLP_TOKEN_FILE` (`lance.otelEnv`) are both derived from
it, so the file the SDK reads is the token the pod projects.

## `{{- define "lance.otlpExporters" -}}`

The components that export OTLP: `catalog`, `lineage`, and `maintenance-worker` with `maintenance.enabled`, as JSON.
`lance.platformEgress` hands it to the `observability.export.egress` row, so the NetworkPolicy admits exactly the pods
that dial the gateway, and one list feeds both, so a new exporter cannot reach the gateway's rules without being in it.

## `{{- define "lance.otlpIdentity" -}}`

The projected token for `lance.otlpAudience`, as the `mounts` or `volumes` half of `lance.identityTokens` (call:
include "lance.otlpIdentity" (list $root "mounts"|"volumes")), rendered only when `lance.otelEnabled`: a pod that
exports nothing projects no token for the gateway. Each exporting workload includes both halves, so its token sits at
`/var/run/secrets/rask/identity/<audience>/token`, the path `lance.otelEnv` names.

## `{{- define "lance.otelEnabled" -}}`

Whether the apps carry the OTel SDK wiring (instrument, otelEnv, the gateway's token): exactly when
`observability.enabled`. Non-empty string = on (helm `if` truthiness).

## `{{- define "lance.platformStoreAddr" -}}`

THE PLATFORM'S STORE IS NAMED, AND OVER TLS ([[XC-149]]). The chart renders no secret store, so with
`openbao.enabled` off the address is required: each service reads its own secrets from it under its own login, its bus
credential among them, and a render that names no store would leave each with nothing to log in to.
An http address is refused because every service presents its ServiceAccount token and its store
token to it, in the clear otherwise; the dev store alone is http, inside the namespace.

## `{{- define "lance.vaultAddrFQDN" -}}`

The SAME address, FULLY QUALIFIED — for a caller that is not in this namespace.
A bare service name resolves only within its own namespace. Every pod reading
`lance.vaultAddr` sits in this release's namespace, so the short form is
correct there. The ExternalSecrets OPERATOR does not: it runs in `external-secrets`, and its
SecretStore login failed with `lookup rask-openbao ... server misbehaving` — a DNS failure that
reads as an OpenBao outage rather than as a name that cannot resolve from where it is used.
Measured against the live cluster 2026-08-31.

## `{{- define "lance.busEnv" -}}`

The bus as a service's process reaches it (list $root <user>): its address (`RASK_BUS_URL`), the bus user the process
connects as (`RASK_BUS_USER`), the file its pod's bus token is projected to (`RASK_BUS_TOKEN_FILE`), the longest a
session lives (`RASK_BUS_SESSION_SECONDS`, `nats.auth.sessionSeconds`) and, with `nats.ca.configMap`, the CA file
(`RASK_BUS_CA_FILE`). Given for the catalog and lineage, and for maintenance's own user while the worker publishes events;
with an empty user only the token, session and CA values are given, which is how the maintenance worker's queue
connection (`maintenance-worker`) gets its own. Each service publishes its events, and takes its deliveries, on a
connection of its own as that user (`lakehouse.events.bus`, [[XC-199]]), presenting the pod's token (`lance.busIdentity` mounts
it) and holding no bus credential, with replies arriving under `_INBOX_<user>`, which `nats.auth.users` grants to that
user alone. The render refuses a user the table does not declare, a user that is not a session user (`lance.busSessionUser`:
it has no ServiceAccount, so it has no token to present), and a user whose row grants no inbox of its own, since the bus
would hand it no acknowledgement of a publish. Names, addresses and file paths, never a credential. A session the bus
ends is asked for again by the process on the same connection, and a connection the bus closes is opened again, each
consumer loop pulling again on the new one; no pod restarts for it.

## `{{- define "lance.busSessionUser" -}}`

The refusal a service's bus user must pass: the table declares it (`lance.natsUsers`) and it is a session user
(`lance.natsSessions`). A row without a ServiceAccount is a static user and fails here, since its pod would have no
ServiceAccount token to present.

## `{{- define "lance.busAudience" -}}`

`nats.auth.audience`, the audience of the token each session user's pod projects and the signer's TokenReview names. It must
be a lowercase name (`^[a-z0-9]([-a-z0-9]{0,52}[a-z0-9])?$`) because it names the pod's token volume and path.

## `{{- define "lance.busTokenFile" -}}/var/run/secrets/rask/identity/{{ include "lance.busAudience" . }}/token{{- end -}}`

Where `lance.identityTokens` mounts the projected token for the bus audience: `RASK_BUS_TOKEN_FILE`.

## `{{- define "lance.busSessionSeconds" -}}`

`nats.auth.sessionSeconds`, validated: a number, at least 60 (a pod's refresh never races its own connect) and below 600,
the lifetime of the token a pod presents (`lance.identityTokens`, `expirationSeconds: 600`), so a refresh never presents
a token about to lapse. It is rendered into the permission table and into each pod.

## `{{- define "lance.busCaConfigMap" -}}`

`nats.ca.configMap`, the ConfigMap in the release's namespace holding the CA bundle the bus's certificate chains to, or
empty for the system's trust store. Setting it requires `nats.ca.key`.

## `{{- define "lance.busIdentity" -}}`

What a session user's pod mounts to reach the bus: the projected token for the bus audience (`lance.identityTokens`) and,
with `nats.ca.configMap`, that ConfigMap's bundle read-only at `/etc/rask/bus-ca/ca.crt` (`lance.busCaFile`). The same
include renders the volumes and the mounts (`list $root "volumes"` and `"mounts"`) of the catalog, lineage, the maintenance
worker and the stream Job.

## `{{- define "lance.natsTable" -}}`

The NATS permission table as issued (values.yaml `nats.auth`): `users`, plus each `flagged` grant whose key holds. A key
is one value path, or several joined by `,` that must all be true, so a grant whose call site exists only under two
switches is issued under both ([[LH-559]]).

## `{{- define "lance.natsUsers" -}}`

The NATS users of lance.natsTable, sorted and validated: a name becomes a secret name and a shell word, a subject a single-quoted shell word.

## `{{- define "lance.natsSessions" -}}`

The session users of the table, as JSON {user: ServiceAccount}. The lakehouse's own users take their ServiceAccount from
the chart (`lance.natsOwnAccounts`: `catalog`, `lineage`, `maintenance` and `maintenance-worker`, and `admin` for the
stream Job), and a values row supplying `serviceAccount` for one of them is refused. A client's row is a session user when
it names a `serviceAccount` of the release's namespace; the render refuses one that is not a name, or that is a
ServiceAccount the lakehouse renders or `default`, since a session user's token names an identity of its own.
`lance.natsSessionUsers` lists them and `lance.natsStaticUsers` lists the rest (`monitor`, `object-store`, a client's row
without a ServiceAccount).

## `{{- define "lance.natsPermissions" -}}`

The JSON the signer reads from ConfigMap `<release>-nats-permissions` (`nats-permissions.yaml`): the audience, the longest
session in seconds, and for each session user its ServiceAccount as `system:serviceaccount:<namespace>:<name>` with its
publish and subscribe lists. Only session users are listed.

## `{{- define "lance.busConsumers" -}}`

EVERY DURABLE CONSUMER A SERVICE PULLS, IN ONE DERIVATION ([[XC-199]]), as a JSON list of {stream, name, config}. Its
readers must agree: the stream Job's declare phase converges each consumer to its config, its retire phase removes a
consumer of this release that the list no longer names (`nats-stream-job.yaml.md`), and
`tests/chart/test_every_bus_user_may_do_what_its_service_is_configured_to.py` fails a render whose consumers a bus user
cannot pull. Every consumer is a pull consumer with explicit acks; no service creates, edits or deletes one, and its bus
user holds only `$JS.API.CONSUMER.MSG.NEXT.<stream>.<consumer>` and `$JS.ACK.<stream>.<consumer>.>` on it. Each
config carries the release's mark, `metadata: {"rask.dev/release": "<release name>"}`, which is what makes a consumer
this release's to retire.

- `LINEAGE/lineage-ingest`, filtered to `pubsub.topic`: lineage's ingest lane. It starts by stream sequence
  (`start: position`), and the Job declares the start it holds back to it, since a start cannot be edited.
  Its ack wait is `services.lineage.delivery.ackWaitSeconds` (lineage cuts a handling 10 s short of it), its
  `max_ack_pending` is `services.lineage.deliveriesInFlight`, the bound across every replica, and its `max_deliver` is
  unlimited (-1): lineage's own rule ends a delivery, parking it after `services.lineage.delivery.attempts`
  deliveries `retrySeconds` apart (`lakehouse.events.bus_delivery`), since a pull consumer has no dead letter.
- `MAINTENANCE_TASKS/maintenance-worker`, `MAINTENANCE_SCHEDULES/maintenance-due` and
  `STORE_EVENTS/maintenance-store-events`, under `maintenance.enabled`: the worker's queue, its fired schedules and
  the store's change events, each delivered from the stream's first message.
- `LINEAGE/maintenance-arrival`, under `maintenance.arrivalEnabled`, filtered to `pubsub.topic`: the worker's
  write-event lane. It delivers only what is published after it is created (`deliver_policy: new`), because a replayed
  announcement acts twice: replaying the retained stream would queue a pass for every table written in the window,
  which each table's own scheduled pass reaches anyway, within `maintenance.queue.recheckHours`.

A TASK OR AN ANNOUNCEMENT IS DELIVERED ONCE MORE THAN `maintenance.queue.maxDeliver`. A worker whose run fails on the
last delivery ends the message itself, but a pass that takes its replica down (out of memory, or past twice
`maintenance.queue.maxPassSeconds`, when the worker exits) never gets there, and JetStream keeps a work-queue message
whose deliveries ran out (measured on nats-server 2.15.2, 2026-11-09): it would hold its table's subject, refusing
every later pass of the table, until the stream's day-long `max_age`. The extra delivery reaches a worker, which ends
the message without handling it and counts it (`maintenance.tasks.ended`, the alert MaintenanceTasksEnded, for a task;
`maintenance.arrivals.settled` at `ended` for an announcement), and holds a task's next try
`maintenance.queue.recheckHours` out, since the hourly audit queues a table nothing holds. `maintenance-due` alone
sets no limit (-1).

## `{{- define "lance.mintsDevCredentials" -}}`

Whether the chart generates the dev OpenBao's credentials (external-secrets.yaml): only for the dev store (`openbao.enabled`), which the openbao fixture runs and this chart seeds.

## `{{- define "lance.natsMints" -}}`

Whether the dev OpenBao issues the NATS credentials: the dev store this chart seeds is the issuer of the dev bus (deploy/fixtures/nats).

## `{{- define "lance.natsFiles" -}}`

The Secrets ESO writes for the NATS pods that read a credential from a file, as JSON {target: [store secrets]}, present only where the dev store issues the bus (`lance.natsMints`): `nats-box-creds` (the static user `nats-user-monitor`), `nats-auth` (`nats-server` and `nats-route`), `nats-signer` (`nats-callout`, the stand-in signer's keys) and, with `objectStore.enabled`, `object-store-bus-creds` (the static user `nats-user-object-store`). This chart's ExternalSecrets write only `object-store-bus-creds`; the ESO policy grants all of them, because the NATS fixture's ExternalSecrets read the others from the dev store. No session user's credential is in this list: it has none.

## `{{- define "lance.otelEnv" -}}`

OTel SDK env for an app (call: include "lance.otelEnv" (list $root "<service.name>")). The apps run
under `opentelemetry-instrument` and export all three signals OTLP. OTel-first: the target is the platform's gateway
(lance.otlpEndpoint), which adds whatever its store needs, so the app stays backend-agnostic (plain OTLP, no
vendor headers). The SDK appends /v1/{traces,metrics,logs} to the endpoint.

## `- { name: OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER, value: "rask_service_account" }`

Each export is the service's own, authenticated (ADR 0215). The OTel SDK's OTLP/HTTP exporters load the
`requests.Session` they post with from the entry point this names (group `opentelemetry_otlp_credential_provider`);
`rask_service_account` is `lakehouse.governed.otlp_identity`, a session that reads the pod's projected token from
`RASK_OTLP_TOKEN_FILE` on every request and sends it as a bearer, so a rotated token is used from the kubelet's next
write and no telemetry secret exists. The exporter itself stays the SDK's.

## `- { name: RASK_OTLP_TOKEN_FILE, value: "/var/run/secrets/rask/identity/{{ include "lance.otlpAudience" $root }}/token" }`

The token `lance.otlpIdentity` projects for `observability.identity.audience`, at the path `lance.identityTokens`
mounts it under. The gateway verifies its signature against the cluster issuer's keys and its audience against the
same value, so a 401 at the gateway names a token that is missing here or minted for another audience.

## `- { name: OTEL_LOGS_EXPORTER, value: "otlp" }`

Logs go out via the OTel SDK (OTLP → the platform's gateway; in development the observability fixture's Collector →
GreptimeDB `opentelemetry_logs`) — the "three signals, one SDK, all OTLP" path. NO double-ingest: the app pods carry
`lance.dev/logs=otlp`, which a gateway that also tails pod logs keys its drop of the file-tailed copy on (the
development gateway's filter processor, `deploy/fixtures/observability/docs/collector.yaml.md`), so the file-tailed infra logs (no OTel
SDK) land in the SAME `opentelemetry_logs` table without duplicating the app logs. The filter drops ONLY
what the SDK also exported: a file-tailed line is a duplicate iff a root-logger handler emitted it, and those
all begin with an ISO date — so the condition additionally requires `IsMatch(body, "^[0-9]{4}-...")`. A pre-SDK crash and a uvicorn.error record therefore
survive in `opentelemetry_logs`, unparsed but present.

DO NOT "SIMPLIFY" THAT BACK TO THE LABEL. It is POD-SPEC metadata, stamped by the ReplicaSet at creation, so
it is never absent during a crash loop. A filter keyed on the label's absence deleted every crash log on 10 pods —
measured 2026-08-23, 0 survivors of 10.

## `- { name: OTEL_METRIC_EXPORT_INTERVAL, value: "5000" }`

Default metric export interval is 60s — too slow to observe in a demo/test. Push every 5s.

## `- { name: OTEL_SEMCONV_STABILITY_OPT_IN, value: "http" }`

The HTTP instrumentation emits OpenTelemetry's stable HTTP conventions and nothing else: `http.server.request.duration`
in seconds, labelled `http.route` and `http.response.status_code`, which the alert rules and dashboards read
(`deploy/alerting/rules.yml` § `lance-http`). Without it the instrumentor emits `http.server.duration` in
milliseconds under `http.target` and `http.status_code`, which those rules select nothing of (measured on
opentelemetry-instrumentation-fastapi 0.66b2, 2026-11-16). Read once, when `opentelemetry-instrument` starts the
process, so it is an environment variable and no setting of the app's.

## `- { name: OTEL_PYTHON_FASTAPI_EXCLUDED_URLS, value: "/livez,/readyz,/healthz,/api/health,/metrics" }`

Don't trace the k8s probe endpoints — they're hit every 10s and bury real request spans (otel
signals.md § Exclude noisy endpoints). The FastAPI instrumentor reads this itself, launcher or not.

Covers every path a probe is pointed at, not just the operational pair — including `/api/health`, which remains
served as a badge even though no probe asks for it. `/healthz` matches no route any service mounts. A list that leaves
out a probed path lets every pod trace its own kubelet polls: measured 2026-08-23, 31,916 spans in three hours across
four services, and each probe costs several spans, since ASGI instrumentation
emits a child per send and receive.

## `{{- define "lance.preStop" -}}`

Rollout drain: a preStop sleep holds SIGTERM until endpoint removal has propagated to kube-proxy and
the Gateway, so in-flight requests drain instead of hitting connection-refused — this is what makes
the apps' /readyz shutting_down branch actually reachable during a rollout. Pairs with pod-level
terminationGracePeriodSeconds (grace > preStop + app drain). sh exists in every app image
(python-slim).

## `{{- define "lance.deadlineGrace" -}}`

The grace period of a pod whose doors answer at a deadline: `lifecycle.preStopSeconds` + its `requestDeadlineSeconds`
+ `lifecycle.teardownSeconds` (call: include "lance.deadlineGrace" (list $root <requestDeadlineSeconds>)). The catalog
and lineage answer every door by that deadline (ADR 0203), so a pod stopped while one runs holds SIGKILL off for the
preStop sleep, the whole deadline and the lifespan's teardown, and every request in flight is answered. Work a door
still does past its deadline ends with the pod. Both
`terminationGracePeriodSeconds` and the `--timeout-graceful-shutdown` `rask.shutdownArg` derives read this one sum, so
the two cannot disagree. Every other pod keeps `lifecycle.terminationGracePeriodSeconds`.

## `{{- define "rask.shutdownArg" -}}`

values.yaml lifecycle. Arg: $root, or (list $root <pod grace>)

## `{{- define "lance.resources" -}}`

HTTP health probes for the FastAPI app workloads (catalog/lineage/maintenance). Two
distinct signals: readiness (/readyz) is dependency-aware (503 until the pool/namespace is up AND again once
draining) so k8s only routes traffic to a truly-ready pod; liveness (/livez) is process-up only (never
checks a backend — a slow dependency must NOT trigger a restart loop). Liveness runs slower + more tolerant
(failureThreshold 3 × 20s) so a busy-but-alive worker is never SIGKILLed. One helper = every app agrees.

Soft anti-affinity: spread a component's replicas across NODES so a single node drain/failure can't
take the whole service down — otherwise the prod replicas:2 can co-locate and the PodDisruptionBudget buys
nothing (audit: "the HA replica count buys nothing"). ScheduleAnyway (NOT DoNotSchedule) so single-node
kind still schedules every replica. Gated by the caller on podDisruptionBudget.enabled (the prod HA signal
that also bumps replicas). Call: include "lance.spreadConstraints" "<component-label>". (prod-readiness P2)

Per-workload resource tier: resources.<comp> if defined, else resources.default. Lets a stateful store
(age) or the Arrow-IPC-buffering catalog be sized ABOVE the stateless-pod default without a
per-template edit — just set resources.<comp> in values(-prod). Every workload shared one 1-CPU/512Mi
default before, so the stores + the 256MiB-body catalog were sized like request pods (audit). Call:
include "lance.resources" (dict "root" $ "comp" "catalog"). (prod-readiness P2)

## `{{- define "rask.proxyHeaderArgs" -}}`

THE TRUSTED-PROXY POSTURE the fleet images document, rendered so production actually runs it.
Call: include "rask.proxyHeaderArgs" $root   (emits two uvicorn args).

Six dockerfiles end with `CMD ["uvicorn", …, "--proxy-headers", "--forwarded-allow-ips", …]` and
a comment reading as a deployment requirement ("MUST be the cluster edge's CIDR at deploy time
… never '*'"). The chart overrides `command`/`args` for every one of them, so NONE of it ran —
six images documenting a posture production never had. Owner decision 2026-08-27: the chart
carries it.

`127.0.0.1` — uvicorn's own default, and what those CMDs hardcode — is the value you would use
if the proxy ran in the same pod. It is the Gateway's data plane, in another pod on
another IP, so the match can never succeed and every forwarded header is silently ignored. Set
`forwardedAllowIps` to the data plane's pod CIDR to make the client IP real (`deploy/values-local.yaml`
names k3s's pod CIDR, 10.42.0.0/16, where Traefik runs). The default
stays 127.0.0.1 so this changes nothing silently on an existing install.

`*` IS REFUSED HERE, not in a comment. It trusts every peer, so any client's `x-forwarded-for`
becomes the resolved client IP — the dockerfiles already said "never '*'", and this finding is
precisely about a comment not being a mechanism.

## `startupProbe:`

startupProbe gates liveness+readiness until boot completes: the FastAPI lifespan (the secret store login,
asked again for about two minutes while the store does not admit the pod, + FGA provision) runs BEFORE uvicorn accepts connections, so nothing
answers /livez during boot. Without this, liveness (armed ~70s in) SIGKILLs a still-initializing pod
into CrashLoopBackOff exactly when a dependency is already slow. 30×10s = 300s boot budget, then the
fast liveness/readiness cadence takes over. (prod-readiness P1)

## `{{- define "lance.tcpProbes" -}}`

TCP health probes for non-HTTP-health workloads — the SvelteKit web pod (no /readyz route) and the object store
(S3 API, no health route). A successful TCP accept on the serving port is the liveness/readiness signal.
Call: include "lance.tcpProbes" "<portName>" (the named container port to dial).

## `{{- define "lance.rootSecurityContext" -}}`

Container hardening applied to every APP container (our images: catalog/lineage/web/maintenance).
runAsNonRoot enforces the image's non-root USER (catalog uid 10001, web `bun`) at admission — a manifest that
regressed to root fails to start instead of running privileged. drop ALL caps + no privilege escalation +
the RuntimeDefault seccomp profile = the restricted PodSecurity baseline. readOnlyRootFilesystem is on by
default (values.security.readOnlyRootFilesystem) — each app mounts an emptyDir at /tmp for scratch (pyarrow
spill, OTel), so nothing needs a writable rootfs. Container-level (NOT pod-level) so it never touches the
busybox wait-age initContainer (which legitimately runs as root).

The same baseline for a container whose IMAGE runs as root, so the uid must be pinned rather than
inherited. `lance.securityContext` relies on the image's own USER (catalog uid 10001, web `bun`), and
`runAsNonRoot: true` without a uid fails admission on an image that declares none — which is why the
busybox waiters carried a hand-written context instead of the helper.

UNCONDITIONAL, and that is the change ([[XC-061]]). These contexts sat behind
`security.infraContexts.enabled`, which values.yaml defaults OFF — so the pods the flag exists to make
PSA-restricted-compliant were not compliant by default, and the flag's own comment names the
consequence: "labeling the namespace restricted would REJECT this pod until the init is hardened".
Gating the hardening behind a flag postpones exactly the work that makes the flag safe to flip.

`readOnlyRootFilesystem` IS INCLUDED, which the hand-written contexts omitted: both waiters run
`sh -c "until nc -z host port; do sleep 3; done"` and write nothing at all.

Usage: {{- include "lance.rootlessSecurityContext" 65532 | nindent 10 }}

The baseline MINUS the non-root pair, for an image whose tool does not work as any other uid.
MEASURED 2026-09-23 before this existed, because "it needs root" is the easiest claim to accept without
checking: the `nats` CLI answers `could not load schema` for every non-root uid tried — 65532, 1000 and
65534 — on nats-box 0.14.5 AND 0.19.7, while the same command from the same image as root succeeds.
Bisected key by key: `readOnlyRootFilesystem` alone is fine; `runAsNonRoot`/`runAsUser` is what breaks
it. So this drops exactly the pair that fails and keeps the four that do not.

NOT A GENERAL ESCAPE HATCH. A container using this is still named in the hardening ratchet for the two
keys it lacks, so it cannot quietly stay here once the image can run rootless.

Usage: {{- include "lance.rootSecurityContext" . | nindent 10 }}

## `readOnlyRootFilesystem: {{ .Values.security.readOnlyRootFilesystem }}`

UNCONDITIONAL. No development loop writes into a running container, so nothing needs a
container's filesystem unlocked, and a chart a reconciler applies should not carry a values flag
that weakens it.

## `{{- define "lance.tmpMount" -}}`

The writable-/tmp scratch pair that makes readOnlyRootFilesystem feasible: an emptyDir volume + its
mount. Emitted as a container volumeMount via "lance.tmpMount" and a pod volume via "lance.tmpVolume" so a
read-only rootfs still has a place for pyarrow/Lance spill + OTel. Only needed when readOnlyRootFilesystem
is on; harmless (an unused tmpfs) when off, so unconditionally included keeps the templates uniform.

IT CARRIES A sizeLimit, because an emptyDir without one may grow until the NODE's disk is full — and
DiskPressure evicts every pod on that node, not the one that filled it. This volume is also where
starlette spools each multipart file part, so on the media apps it is directly reachable from an
unauthenticated request. With the bound, the kubelet evicts the offending POD and the node survives.
See security.tmpSizeLimit.

## `{{- define "rask.image" -}}`

A FULLY-RESOLVED image reference for a first-party component. Call:
  include "rask.image" (list $root "lance-rest-catalog")

GITOPS IS THE FIRST-CLASS CONSUMER of this chart, and that decides the two rules below.

1. `image.repository` is REQUIRED. An empty one renders a BARE `lance-rest-catalog:dev` — and a
   bare name is not a local image, it is `docker.io/library/lance-rest-catalog:dev`. That only
   appears to work because `make k3s-import` had side-loaded the tag into the node's containerd,
   so the kubelet never had to pull. A reconciler has no such side channel: every pod
   ImagePullBackOffs, and the error blames Docker Hub rather than the missing setting. Measured
   here 2026-08-04, on `controlplane:dev` and `compute:dev`, after a `helm upgrade` reset every
   Deployment to that default.

   Side-loaded images are still supported — but as an EXPLICIT opt-in (`image.localImages: true`),
   never as the fallback you reach by forgetting.

2. `image.digest` wins over `image.tag` when set. A tag is a mutable pointer; GitOps wants the
   deployed artifact to be exactly what the commit says, and a digest is the only reference that
   cannot drift under it. Setting both is not an error — the digest simply wins, so an automation
   can keep writing a human-readable tag alongside it.

## `{{- $override := "" -}}{{- if gt (len .) 2 -}}{{- $override = index . 2 -}}{{- end -}}`

Optional 3rd element: an explicit tag that beats image.tag. A digest still wins over it, so a pinned reconciler is never overridden by a tag.

## `{{- $perDigest := "" -}}{{- if $i.digests -}}{{- $perDigest = index $i.digests $name | default "" -}}{{- en...`

PER-COMPONENT pins (#135). The fleet is NOT built as one tag and never has been: on a live
estate this chart's single `image.tag` had to describe four different references at once
(a catalog tag across 11 services, a different backend tag, a zone tag, and an ingest
DIGEST under a repo name the chart cannot even produce). It cannot, so `helm upgrade`
rewrote every image to one tag that was not on the node — measured twice on 2026-08-06,
once taking the whole fleet to ImagePullBackOff and costing 22 `kubectl set image` calls to
recover. That made DEPLOYING A DESTRUCTIVE ACT, which is why an entire day of fixes reached
the cluster by hand instead of through the chart.
`image.digests.<component>` and `image.tags.<component>` are read HERE, so a pin is real
rather than documentation. Precedence, most specific first:
  digests.<c>  >  digest  >  tags.<c>  >  <call-site override>  >  tag
A digest still beats every tag, so a reconciler's content pin is never undone by a tag.

## `{{- printf "%s:%s" $name (required "image.tag must be set" ($override | default $i.tag)) -}}`

Side-loaded: a bare name the kubelet must already hold. Never valid for a remote cluster.

## `{{- define "lance.governedAuthAck" -}}`

── the governed-auth env, defined ONCE ─────────────────────────────────────────────────────
`(include "lance.governedOidcEnv" $root)` -> RASK_OIDC_*, everywhere, for every service.

The helper takes no variable PREFIX: the auth field-set is declared once, in
`lakehouse.governed.settings`, and binds one RASK_* name each. A prefix parameter here would
emit variables nothing reads.

Split into OIDC / FGA / PINS rather than one block, because a caller may need a subset: a
service that runs OIDC without FGA would otherwise have an authorizer silently enabled that its
block never asked for.

Safe to set estate-wide: these change behaviour ONLY where a route declares an auth dependency,
so a service with no gated route is unaffected and cannot quietly 401 a surface nobody gated.
(Measured in #90: scoping the block to one service left the one that streamed page IMAGE BYTES
wide open.)

The acknowledgement a service needs when authentication is deliberately OFF.

`assert_authentication_configured` refuses to boot a governed service whose auth is off with nobody
having said so (Q17-6 / §F2-2), because "off because I meant it" and "off because nothing set it" are
indistinguishable and only the second is a vulnerability. `auth.enabled: false` is a SUPPORTED
toggle — the open dev/demo profile — so the chart says which one it means rather than leaving the
pod to refuse.

Emitted in the `else` of every `if .Values.auth.enabled` that includes `governedOidcEnv`, so the two
can never drift into a profile that sets neither.

A real deployment refuses `auth.enabled: false` (`prod-credentials.yaml`, [[XC-184]]), so the acknowledgement is only
ever a development render's.

## `{{- define "lance.identityTokens" -}}`

A caller's projected identity, one volume per door audience it calls ([[LH-220]], D1); rationale in
values.yaml `auth.serviceAccountIssuer`. Call: include "lance.identityTokens" (list "volumes"|"mounts" <aud>...).

## `{{- define "lance.accountOf" -}}`

The username the cluster's issuer gives a ServiceAccount of the release's namespace,
`system:serviceaccount:<release namespace>:<serviceaccount>`: the `sub` of its tokens. The one place the chart spells it.
Call: include "lance.accountOf" (list $root <serviceaccount name>).

## `{{- define "lance.subjectOf" -}}`

The subject a ServiceAccount of the release's namespace is, `kubernetes~` and its username (`lance.accountOf`, ADR 0156
C5): the principal every grant, author and signing identity names, and the one place the chart joins the issuer prefix.
Call: include "lance.subjectOf" (list $root <serviceaccount name>).

## `{{- define "lance.saName" -}}`

The ServiceAccount of one of the release's own services, `<release>-sa-<component>` (security-sa.yaml), the one place
the chart spells the name. Call: include "lance.saName" (list $root <component>).

## `{{- define "lance.saSubject" -}}`

The subject one of the release's own services is: `kubernetes~system:serviceaccount:<release namespace>:<release>-sa-<name>`
(ADR 0156 C5), the subject of the ServiceAccount its pod runs as (`lance.subjectOf` of `lance.saName`). Every grant,
author and signing identity the chart names for a service is this, so none can name a subject the pod does not
authenticate as. Call: include "lance.saSubject" (list $root <name>).

## `{{- define "lance.saComponents" -}}`

The first-party services each given a ServiceAccount of their own, `<release>-sa-<component>` (security-sa.yaml):
one list, so the accounts the chart renders and the accounts an external store reader may not bind
(`lance.serviceAccounts`) cannot drift apart.

## `{{- define "lance.objectStoreIdentities" -}}`

THE WORKLOADS THAT REACH THE STORE, each by its own ServiceAccount token ([[XC-083]], [[XC-084]], ADR 0170): the
catalog and lineage always, maintenance with `maintenance.enabled`, and each backup CronJob while it is on. Read off
`lance.saComponents`, so whether a backup runs is decided in one place, and limited to the components with a store
policy. One list names both what `object-store-identities.yaml` renders a policy for and what the provisioning hook
creates at the store, so a workload cannot reach the store under a policy git does not declare. No workload on it holds
a key pair.

## `{{- define "lance.objectStorePolicy" -}}`

ONE IDENTITY'S CANNED POLICY, as IAM JSON, which the store grants every session a token naming
`<release>-sa-<component>` is exchanged for. Call: include "lance.objectStorePolicy" (list $root <component>). Each
component's statements are a define of their own, `lance.objectStorePolicy.<component>`, chosen by name, so a component
with none fails the render as an undefined template.

- `catalog`: the whole data plane it governs, buckets included, since every vend is a session policy narrowed from it.
- `lineage`: list, get and delete under `<control bucket>/_lineage_outbox/`, the one outbox it drains, and get and put on
  exactly `<control bucket>/_timers/lineage-relay.json`, its relay timer's claim record.
- `maintenance`: `ListAllMyBuckets`; list and `GetBucketLocation` on every bucket; get on each table's history and no
  data ([[LH-575]]): `_versions/*.manifest`, `_refs/*.json` and `_transactions/*.txn` under any key, each root's
  `__manifest/` index, and the bare `_versions`, `tree` and `_mem_wal` keys, which pyarrow HEADs before it lists a
  directory, a refused HEAD raising where an absent one answers "not there". Each history grant is anchored on its file's
  suffix because a grant's `*` spans `/`, and a writer may name a data directory `_versions`: no data, deletion, index or
  blob file ends in `.manifest`, `.json` or `.txn`. Get on the control-root records it judges by (`_locations`, `_trash`,
  `_policies`, `_warehouses`, `_projects`, `_bases`, `_file_identities`, `_maintenance_due`, `_maintenance_files`,
  `_lineage_outbox`), and no backup; put and delete only on `<control bucket>/_maintenance_due/*`,
  `<control bucket>/_maintenance_files/*` (its copy of the commit records it has read, `lakehouse.file_identities`, which
  no erasure or hold reads), `<control bucket>/_policies/state/*` and `<control bucket>/_lineage_outbox/*`. What a pass,
  the audit and the scan read under it was measured on pylance 13.0.0 behind a counting proxy: opens, version lists,
  `explain_cleanup_old_versions`, transactions, `tracked_files()` and the compaction planner read only listings,
  `_versions/` and `_refs/`, and index health reads `describe_indices()`, which reads no index file; a table's whole pass
  over a table with a tag, a branch, a deletion and an index (re-measured 2026-11-12) got only `.manifest` and `.json`
  keys under them, the `__manifest` index and the control records. A data file, an index file, a deletion vector and a
  backup are out of its reach, so a compromised maintenance pod reads no tenant's rows.
  Owner, 2026-11-03: a compromised maintenance pod must not be able to defeat an erasure or a hold, so every other
  maintenance write is signed by a catalog vend (the table's `maintain` tier, or a session deleting one due trash
  record's bytes), and the trash records themselves are the catalog's to write (ADR 0197).
- `backup-pg`: list, write and prune under `<control bucket>/_backups/pg/`.
- `backup-control-root`: read the control-root record prefixes it copies, and write and prune under
  `backups.controlRoot.dest`.

## `{{- $statements = append $statements (dict "Sid" "ClaimItsOwnTimersWindows" "Effect" "Allow" "Action" (list "s3:...`

Lineage's relay is a fixed timer (`lakehouse.timers`): each replica reads the timer's claim record and tries one
conditional write that claims the window, and the replica whose write lands runs the tick. Get and put on that one key
are all the claim needs; no list and no delete, and no other timer's record, so a lineage pod can neither claim nor
block a catalog timer's window. Rendered only with `services.lineage.relay.enabled`, so a lineage that runs no relay
holds no write at the store. A store answers the read of a missing key as forbidden to an identity that may not list
(S3 does), which the timer takes as no claim: its create-if-absent write is the store's answer to whether one exists.
Pinned by `tests/chart/test_scoped_policies_reach_runtime_minted_warehouses.py`.

## `{{- define "lance.objectStoreSessionEnv" -}}`

THE FOUR VARIABLES EVERY STORE-REACHING POD GETS, under its own prefix (`LANCE`, `LINEAGE`, `MAINTENANCE`, `RASK` for a
backup): `<PREFIX>_S3_WEB_IDENTITY_TOKEN_FILE` (its token for `objectStore.identity.audience`), `_STS_ENDPOINT` (the
store's own, so boto3 never posts the token to the public AWS STS), `_ASSUME_ROLE_ARN`
(`objectStore.identity.roleArns.<component>`, `required`; AWS assumes it, a store granting the policy the token's
subject names ignores it) and `_SESSION_SECONDS` (`required`, since each reads it as a typed int). One include, so the services (`lakehouse.store_session`) and the
backups (`JobStoreIdentity`, `job_client`) cannot read the identity under different names. Call: include
"lance.objectStoreSessionEnv" (list $root <PREFIX> <component>).

## `{{- define "lance.serviceAccounts" -}}`

Every ServiceAccount the product chart renders: the `lance.saComponents` accounts, the backup snapshot's and
OpenFGA's, each rendered beside its own workload, and, with `console.enabled`, the
console's `rask-sa-console` (`console.yaml`). `lance.storeReads` refuses an
external reader whose role would bind one of them, or the namespace's shared `default`: a role bound to the lakehouse's
own account hands that reader's reads to a lakehouse pod, and one bound to `default` hands them to every pod that names
no account ([[XC-165]]).

## `{{- define "lance.saAccount" -}}`

The ServiceAccount username one of the release's services runs as, `system:serviceaccount:<release namespace>:<release>-sa-<name>`
(`lance.accountOf` of `lance.saName`): the `sub` the cluster's issuer puts in its tokens, and `lance.saSubject` without
the issuer prefix. The object store's
identity provider maps a token to the canned policy its `sub` names, so the provisioning hook writes each store
identity's policy under this name (`lance.objectStoreIdentities`, ADR 0170).
Call: include "lance.saAccount" (list $root <name>).

## `{{- define "lance.saVerifier" -}}`

The SA verifier a door builds beside Dex: its env, and the API-audience token + cluster CA it fetches
the issuer's keys with. Any token the issuer signs for the door's audience is a subject, from any namespace, and
OpenFGA alone authorizes it: no list of accounts is rendered. Renders nothing with auth off.
Call: include "lance.saVerifier" (list $root "env"|"volumes"|"mounts" <door>).

## `{{- define "lance.saFetch" -}}`

The API-audience token and cluster CA a reader of the SA issuer's discovery and keys fetches with, at
/var/run/secrets/rask/sa-fetch. Call: include "lance.saFetch" "volumes"|"mounts".

## `{{- define "lance.oidcIssuer" -}}`

The estate's one trust anchor, `auth.oidc.issuer`: every governed door's RASK_OIDC_ISSUER renders from it
([[XC-148]]). Without the development Dex it is the platform's IdP, and the doors fetch the keys they
check each token against from it, so an http issuer is refused at render: over http, whoever answers
that address on the path chooses the keys.

## `{{- if $root.Values.dex.enabled }}`

Split-horizon, for the development Dex only: the issuer is the URL the browser sees and tokens carry;
discovery and JWKS are fetched in-cluster at `http://<release>-dex`. Without Dex (every real
deployment, prod-credentials.yaml) the doors fetch from the issuer itself.

## `- { name: RASK_OIDC_ALLOW_INSECURE, value: {{ ternary "true" "false" $root.Values.dex.enabled | quote }} }`

The http escape hatch opens exactly when the development Dex is the IdP, because its in-cluster
discovery URL is http whatever the issuer's own scheme. Keyed to the issuer's scheme instead, a
production render that left the issuer at a dev default trusted `rask-dex` over http on every door
([[XC-148]]). Stated as `"false"` otherwise rather than left to the setting's default, so the render
itself shows the HTTPS guard on.

## `- { name: RASK_OIDC_IDP_ID, value: {{ include "lance.oidcIdpId" $root | quote }} }`

How every governed door names a caller ([[XC-214]], ADR 0208), one include each, so every door carries the same rules.
`RASK_OIDC_IDP_ID` is the issuer's idp id: its people are `<idp-id>~<subject>` (OpenID Connect Core 1.0 § 2 makes `sub`
unique and never reassigned within its issuer, so that is one person whatever the vendor) and its clients signed in for
themselves `<idp-id>-client~<client id>`. `RASK_OIDC_SUBJECT_CLAIMS` names the claims read before `sub`, for a provider
whose `sub` is not the stable id a grant needs. `RASK_OIDC_CONNECTORS` names a Dex that brokers several upstream
providers connector by connector instead ([[LH-063]], ADR 0159 D5): a door keys a person on `<idp-id>~<user id>` from the
token's `federated_claims` and refuses a token whose connector the map does not name. `RASK_OIDC_CLIENT_MARKER` names
the claim an issuer marks its client's own token with, where the client's `sub` is not its id (Keycloak, Entra).
`RASK_OIDC_PROVIDERS` names further issuers, each with the same keys. Every one but the idp id is rendered only when set.

## `{{- define "lance.oidcIdpIdFormat" -}}`

An idp id a person may carry: a string of lower-case letters, digits and `-`, never `kubernetes` and never ending in
`-client`, the machine identities', since a person named under either would read as a machine at every door.

## `{{- define "lance.oidcIdpId" -}}`

`auth.oidc.idpId`, or `dex` with `dex.enabled`, so the development estate keeps `dex~` people and `dex-client~` clients;
refused at render when empty without the development Dex, and when `lance.oidcIdpIdFormat` refuses it.

## `{{- define "lance.oidcConnectors" -}}`

`auth.oidc.connectors`, with the development Dex's one connector, its password database `local`, as `dex` when
`dex.enabled`, the map names no `local` of its own and `auth.oidc.subjectClaims` is empty, so the development estate keeps
the principals its grants name whatever `auth.oidc.idpId` says: the map, or else the subject claims, picks the rule. Refused for an empty connector id, and for an idp id `lance.oidcIdpIdFormat` refuses. The service refuses
the same settings at boot (`OidcSettings`, a `dict[str, str]`, which takes no number for an idp id); the render refusal
names the value before any pod starts. Empty, it renders nothing, which is what tells the console not to ask for
`federated:id`.

## `{{- define "lance.oidcClientMarkerFormat" -}}`

A client marker as JSON (`claim`, and `value` when set), refused without a claim or on one every verified token carries
(`iss`, `sub`, `aud`, `exp`, `iat`), which would mark nothing.

## `{{- define "lance.oidcClientMarker" -}}`

`auth.oidc.clientMarker` through `lance.oidcClientMarkerFormat`, rendered only when set.

## `{{- define "lance.oidcProviders" -}}`

`auth.oidc.providers` as JSON in the service's own key names, rendered only when set. Each issuer is refused at render
as the service refuses it at boot (`OidcSettings`), so a bad entry fails the release rather than crash-looping a pod:
when it is not https (no development Dex stands behind a further issuer), when it is named twice (`auth.oidc.issuer`
included), when its idp id or a connector's is missing or `lance.oidcIdpIdFormat` refuses it, when an idp id it names
(its own or a connector's) is one another issuer names, when it sets subject claims beside connectors or a claim that is
not one, and when `lance.oidcClientMarkerFormat` refuses its marker.

## `{{- define "lance.oidcSubjectClaims" -}}`

`auth.oidc.subjectClaims` as JSON, rendered only when set: refused beside any connector map `lance.oidcConnectors`
renders (the development Dex's `local` default included), which reads its user id from `federated_claims` and no
subject claim, and for an entry that is not a claim name.

## `{{- define "lance.bootstrapAdmin" -}}`

`auth.bootstrapAdmin`, which the catalog's declaration grants the estate's two standing authorities ([[XC-011]]), must be a
person's principal, `<idp-id>~<subject>`. A bare name is refused at render because no token could ever prove it, a `user:`
id because the catalog encodes the principal itself (`fga.user_object`), and a `kubernetes~` or `<idp-id>-client~` principal
because a machine administers no tenant (LH-433).

## `{{- define "lance.governedFgaEnv" -}}`

The FGA client coordinates. Separate from OIDC — see above.

## `{{- define "lance.openfgaAudience" -}}rask-openfga{{- end -}}`

What an OpenFGA client presents ([[XC-077]]): its projected token for the audience OpenFGA accepts (values.yaml
`openfga:`). Every pod that names OpenFGA renders all three parts under the condition its URL has. Call:
include "lance.fgaToken" (list "env"|"volumes"|"mounts").

## `{{- define "lance.fgaWriters" -}}`

THE SERVICES THAT WRITE OPENFGA TUPLES, space-joined: the catalog alone (ADR 0197). OpenFGA authorizes none of its
callers, so a client able to write one tuple writes any; every other service asks the catalog's doors. The network policy
admits exactly these, beside the model hook, to OpenFGA's API.

## `{{- define "lance.openfgaClients" -}}`

The components whose ServiceAccounts OpenFGA admits, comma-joined and sorted: the one list `lance.openfgaSubjects`
qualifies and the OpenFGA pod's `rask.dev/openfga-clients` annotation must equal (openfga-authn.yaml).

## `{{- define "lance.openfgaSubjects" -}}`

The ServiceAccounts OpenFGA admits (`authn.oidc.subjects`), comma-joined: exactly the pods that render
`lance.fgaToken`, under the same conditions, qualified by the release namespace. Maintenance is not one: OpenFGA has no
per-tuple access control, so a token able to write one tuple writes any, and maintenance asks the catalog's grant doors
instead (ADR 0197).

## `{{- define "lance.fgaEstateRoles" -}}`

THE MACHINE ROLES ON THE ESTATE ROOT THE RELEASE DECLARES, one JSON object from each account's subject to the roles it
holds there, which `lance.declaration` renders as the declaration's `estate.roles`, and which the catalog grants and
converges the release's own accounts to ([[LH-442]], [[LH-563]], [[XC-011]]). Empty with auth off. Lineage holds `decider` (the catalog
decision door's `check`, estate-wide, since lineage records every tenant's runs) and `governance_prober` (its `governed`
and `locate`), on its in-release subject (`lance.saSubject`). A client of the lakehouse, none of which this chart
renders, is granted its roles at the catalog's access doors as its own identity, and this chart declares none.
With `maintenance.enabled`, maintenance's subject holds `reclaimer` (`can_reclaim_trash`, [[XC-084]]), the rung every
trash door under `/management/v1/trash/{kind}/{uuid}` checks, since maintenance's own session writes nothing of the
trash, and `reconciler` (`can_reconcile_grants`), the rung every grant door under `/management/v1/grants` checks, since
maintenance holds no OpenFGA token (ADR 0197).

## `{{- define "lance.ownAccounts" -}}`

THE RELEASE'S OWN ACCOUNTS, the subject of every ServiceAccount it renders (`lance.subjectOf` of each of
`lance.serviceAccounts`), which the catalog reads as `LANCE_OWN_ACCOUNTS`:
the accounts whose estate roles and provenance `writer` are what git declares ([[LH-563]]). Built from the list the
release renders rather than from a name prefix, since a client's ServiceAccount may carry the release's prefix and is no
account of this release. The console's account is left out: it mounts no token, calls no door and holds no role, so no
converger has anything of it to grant or keep.

## `{{- define "lance.governedFgaPins" -}}`

The operator-pinned FGA store id, optional. It is the SAME auth.fgaStoreId the model hook publishes into,
so a pinned store can never diverge from the store the services check against. Unset =
provision-by-name at boot (`lance-catalog`). No model id is pinned: each service checks against the model its
image carries (ADR 0082).

## `{{- define "lance.bootstrapJobDeadline" -}}`

lance.bootstrapJobDeadline — the wall-clock ceiling on a bootstrap Job.

WHY EVERY WAITING JOB NEEDS ONE, measured rather than argued. `backoffLimit` counts POD failures, and
these Jobs wait on a dependency with a loop. If the dependency never answers and the loop is unbounded, it never exits, so the container never exits, so the pod
never fails, so `backoffLimit` never counts and `ttlSecondsAfterFinished` never fires (a TTL applies
only after completion). The Job runs forever. Observed on the live estate 2026-09-24:
the store-provisioning Job of revision r229 Running 27h, its pod still printing its wait line, provisioning nothing.

`activeDeadlineSeconds` is the only one of the three that bounds a pod that is HEALTHY and stuck, so it
is the backstop the other two cannot be. Thirty minutes is deliberately generous — a bootstrap that has
not finished in that long is not slow, it is stuck — so it fails no deploy that was going to succeed.

OBSERVED DOING IT, not assumed: a probe Job on this cluster carrying `until false; do echo wait-forever;
sleep 3; done` — r229's exact shape — and a 20-second deadline was terminated by Kubernetes with
`Failed=DeadlineExceeded` (2026-09-24). The three-bound reasoning above is only worth anything if the
one bound that applies actually fires here.

## `{{- define "lance.objectStoreAudience" -}}`

The audience every lakehouse workload's store token is projected for, `objectStore.identity.audience` (ADR 0170). One
value names both halves: the token each store-reaching pod mounts (`lance.identityTokens`) and the client id the store trusts as its
identity provider's (`deploy/fixtures/rustfs` `identityProvider.clientId`, which
`tests/chart/test_a_provisioned_identity_is_one_the_service_uses.py` holds equal). `required`: a blank audience would
leave every workload no identity at the store, and no workload holds a key pair to fall back to.

## `{{- define "lance.secretNamePattern" -}}^[a-z0-9][a-z0-9-]{0,252}${{- end -}}`

WHAT A SECRET NAME IS, written once: lowercase letters, digits and `-`, one path segment. A name becomes a path of a
store policy (`lance.storePolicy`) and a segment of the path a service reads, so a wildcard or a `/` in one would widen
the policy it lands in. The render holds every name an overlay supplies to it (a client reader's `secrets`, the
forwarder's `authSecret.name`), and the services refuse to read any other (`lakehouse.governed.secrets`).

## `{{- with $v.services.lineage.forward.authSecret.name }}{{- if not (regexMatch (include "lance.secretNamePattern" $root) (toString .)) }}`

The one secret name the lakehouse's own table takes from an overlay. It goes into lineage's policy as written, so `*`
there would let lineage read every secret in the store: refused at the render, before a policy document carries it.

## `{{- define "lance.storeReads" -}}`

WHO READS WHAT FROM THE STORE, ONE TABLE ([[XC-165]], ADR 0169): app id -> {its ServiceAccount, `api` (the secrets its
code reads under its own login to the store, `lakehouse.governed.secrets`)}. Every place that decides a read is a view of it: its store role's policy grants
`api` (the seed's `policy-<release>-<app>.json`), its environment names that role (`lance.secretStoreEnv`), and an app
with no read gets no store at all (`lance.secretScopes`). Two lists that agreed when written are how a read gets
refused at runtime, or granted past its owner, so nothing restates it.

No lakehouse service reads a bus credential: each is a session user that presents its ServiceAccount token to the bus
(`lance.busEnv`, [[XC-199]]), so no `nats-user-<app>` is in any app's `api` list. Pinned by
`tests/chart/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`.

The split is by READER SET, because the store grants by secret name: a secret two apps read is one secret both name
(a signer's public key), never a copy, so a rotation writes one place. The verifier
lists follow the env the same flags render (`LINEAGE_SIGNERS` wherever auth is on, the doors under
`signing.doors`), and a signer reads its own pair. Maintenance's worker reads the signers' public keys when its write-event lane checks
signatures (`auth.enabled`, `maintenance.arrivalEnabled`, and `signing.doors` not `off`).

A CLIENT'S READER (`openbao.externalReaders`) is an ALLOWLIST, because the store grants by name and a client's values are
not the lakehouse's to trust: `<app>` may hold only `nats-user-<app>` (refused when `<app>` is a session user, which holds no such secret), names of its own (`<app>-<name>`) and
`signing-public-<name>`. Each refusal closes one way a client's values could reach a name another identity holds: an
app id that is no lowercase name, a lakehouse app (enabled or not), one whose own names would cover a family the store issues
per identity (`nats-user-<name>`, for every static user of the issued table including a client's own, `signing-key-<name>`,
`signing-public-<name>`), one whose own names would cover a name ESO, a lakehouse identity or the dev store's seed holds
(the seed-only names are listed here, beside the ones the table and `lance.esoReads` derive) or another reader's id, a
role `<release>-<app>` equal to ESO's (`externalSecrets.role`, whose policy and role keys the identities ConfigMap
would carry twice), a ServiceAccount the lakehouse renders or `default` (`lance.serviceAccounts`), and an empty or
non-name secret list. Pinned by tests/chart/test_every_secret_in_the_dev_store_has_exactly_its_readers.py.

## `{{- define "lance.esoReads" -}}`

What ESO's role reads: exactly the secrets the chart's ExternalSecrets (and the NATS fixture's, through rask's
SecretStore, `lance.natsFiles`) name, under the same conditions that render them. No app's secret is here.

## `{{- define "lance.storeRole" -}}`

`<release>-<app id>`: the store role an app logs in as, which its environment names (`lance.secretStoreEnv`). The
release prefix keeps two releases apart in one platform store.

## `{{- define "lance.storePolicy" -}}`

A store policy as JSON, which OpenBao reads as HCL: read on each secret's KV v2 data path, and the token's own
lookup and renewal, which a role with no default policy does not otherwise grant: a service asks about its own token
to learn the store dropped it. Nothing else: no list,
no metadata, no write.

## `{{- define "lance.storeIdentity" -}}`

The token an app logs in with, for an app that reads the store: the pod asks the kubelet for a ServiceAccount token
for `openbao.identity.audience` (600 s, rotated by the kubelet, bound to the pod). The `mounts` part puts it in the app's
own container alone, at the path its environment names in `RASK_SECRET_STORE_TOKEN_FILE`. Both parts render from the one
membership, because a mount naming a volume the pod lacks is a pod the API server refuses.

With `openbao.ca.configMap` named, the same two parts carry the platform store's CA: the volume `secret-store-ca` (that
ConfigMap's key under the file name of `lance.storeCaFile`), mounted into the app at that file's directory.

## `{{- define "lance.storeTokenSeconds" -}}`

A STORE TOKEN'S LIFETIME, HELD TO A MINUTE AT LEAST AND AN HOUR AT MOST ([[XC-199]], ADR 0169). Reads one of
`openbao.identity.tokenTtl` and `tokenMaxTtl` as seconds and fails the render on a lifetime past an hour, on one under a
minute (a service's loop logs in again at half its token's life and looks every 15 s, so a shorter one leaves it no
certain pass to do that in), on none at all (a role with no lifetime of its own issues tokens at the store's default,
which is weeks), and on a form it cannot read (it reads `<n>s`, `<n>m`, `<n>h` and a bare number of seconds, each of
five digits at most with no leading zero: Helm reads `05000` as octal 2560 where the store reads 5000, a product past
64 bits wraps around to a small one, and the role document carries the value as written). A service refuses a
login whose lease is outside that range (`lakehouse.governed.secrets`), so a role the render let through would start no
pod: the refusal belongs where git declares the role. The template that calls it (secret-store-identities.yaml) also
refuses a `tokenMaxTtl` under the `tokenTtl`, since the store would then issue the shorter one and the role would not be
the one git declares. The role documents keep the value as it was written.

## `{{- define "lance.storeTokenFile" -}}/var/run/secrets/rask/openbao/token{{- end -}}`

Where a pod's projected store token is, written once: the volume's file name and its mount in the app
(`lance.storeIdentity`) and the app's `RASK_SECRET_STORE_TOKEN_FILE` (`lance.secretStoreEnv`) both derive from it, so
the mount and the login cannot come to name two files. The app holds no
path of its own: `lakehouse.governed.secrets` refuses a store named without one.

## `{{- define "lance.storeCaFile" -}}/etc/rask/secret-store-ca/ca.crt{{- end -}}`

Where a platform store's CA is in a pod, written once: the ConfigMap volume's file name and its mount
(`lance.storeIdentity`), and the app's `RASK_SECRET_STORE_CA_FILE` derive from it.

## `{{- define "lance.storeCaConfigMap" -}}`

The ConfigMap holding the CA a platform store's certificate chains to, or nothing where none is named
(`openbao.ca`). A CA is public, so a ConfigMap carries it and the platform's own tooling may write it; each of the
store's readers is handed it by name: the services (`RASK_SECRET_STORE_CA_FILE`, since Python checks a certificate
against the bundle it ships with, never the node's) and ESO's SecretStore
(`caProvider`). Refused beside `openbao.enabled`: the development store serves plain HTTP inside the namespace, so a CA
named there would say a store is verified that is not.

## `{{- define "lance.secretStoreEnv" -}}`

WHERE AN APP'S SECRET STORE IS, AND WHO IT LOGS IN AS ([[XC-199]]). The store's address (`lance.vaultAddr`), the app's own
role (`lance.storeRole`), the Kubernetes auth mount and the KV mount ESO's SecretStore names
too (`externalSecrets.kubernetesAuthPath`, `externalSecrets.kvMount`), and the path of the projected token
(`lance.storeTokenFile`). A service that is given the address logs in to the store before it serves and fails its start
closed when the store never admits it, reads every secret under that login, and signs and keys its commit ids from what
it reads there (`lakehouse.governed.secrets`). Its object-store identity is then its ServiceAccount token alone: its
start refuses a key pair in its environment (`lakehouse.store_session.require_env_pair`), and nothing in the process
signs with one (`key_pair_in_force`). Rendered for exactly the apps `lance.storeReads` lists, so a pod told a store
always has a role there.

## `{{- define "lance.storeAudience" -}}`

The one audience a pod's store login is bound to, read by the projected token (`lance.storeIdentity`) and by every
app's role (secret-store-identities.yaml). A blank one fails the render: a role with no audience admits any token of its
ServiceAccount, so a token projected for another door would log in to the store.

## `{{- define "lance.secretScopes" -}}`

The app-ids that read the secret store: every app with a read in `lance.storeReads`. It has several readers that
must not drift: the
store token's volume and the service's own store settings (`lance.secretStoreEnv`) follow it, and the NetworkPolicy
admits exactly these apps' pods to the store (`lance.secretReaderComponents`). One membership decides them all, so no
pod is told to read a store it was named no address, role or token for: such a read fails the pod's start closed.

Usage: {{ splitList "," (include "lance.secretScopes" .) }}

## `{{- define "lance.secretReaderComponents" -}}`

The component labels of the pods that dial the store (each service logs in to it), for the NetworkPolicies that admit them: the scope above, where an app's id is its pods' component label,
and `maintenance-worker`, the label of maintenance's worker pods, which run under maintenance's app id (ADR 0198's
amendment of 2026-11-10) and so appear in no scope of their own. A policy that ranged the scope alone would lock the
worker out of the store, and its start would fail closed. The worker's label is named only where its Deployment is
rendered (`maintenance.enabled`): a label no pod carries admits whoever takes it. The object
store's list (`lance.objectStoreClients`) names it under the same condition.

## `{{- define "lance.allocatorEnv" -}}`

The glibc allocator bound every lakehouse container carries.

WHY IT IS NOT A DEFAULT WORTH INHERITING: glibc sizes its arena cap from `sysconf(_SC_NPROCESSORS_ONLN)`
— the HOST's core count — and a cgroup CPU *quota* does not reduce visible CPUs. Measured 2026-09-21 in
these containers, `nproc` reads 64 while `cpu.max` reads `100000 100000` (one CPU), and the four
lakehouse pods held 34/41/42/59 secondary arenas of 64MB each. Each arena keeps its own free lists and
is trimmed independently, so RSS settles at the SUM of per-arena high-water marks and climbs until every
arena has seen its worst case — which is how `rask-maintenance` reached its 512Mi limit with a flat
Python heap and a Lance session at 7% of its cap.

MEASURED, NOT ASSUMED, in the image these pods run (Debian glibc 2.41): 64 threads forcing arena growth
reserved 65 arenas unset and 1 at `MALLOC_ARENA_MAX=2`. Two rather than one because a single arena
serialises every allocation in a 100+ thread process on one lock.

NOT A SECRET: a libc tunable with no confidentiality, read by glibc at startup and by nothing else.
Pinned by `tests/chart/test_the_lakehouse_bounds_its_allocator_arenas.py`.

## `{{- define "lance.signers" -}}`

EVENT SIGNING ([[LH-064]]; values.yaml `signing:`). `lance.signers` is the ONE declaration, an (identity, key name, app-id,
control-event role) per signer: the mint and seed loops, the store's reads (`lance.storeReads`), RASK_SIGNING_IDENTITY, LINEAGE_SIGNERS and the doors' env
are views of it. The identity is the signer's service-account subject (lance.saSubject), the author its events are authorized as;
the key name is its ServiceAccount's name, which `lakehouse.governed.signing_key.signing_key_name` derives from the identity,
because a subject's `~` and `:` belong in no secret name, OpenBao path or shell word.

## `{{- define "lance.signingKeys" -}}`

The key names the store holds a pair under (list $root [$appId]): every signer's, or one app-id's. Each must be a DNS label.

## `{{- define "lance.delegatorIdentities" -}}{{- include "lance.saSubject" (list . "catalog") -}}{{- end -}}`

Who may sign for a person: the catalog, which authenticated that person.

## `{{- define "lance.signingIdentitiesForApp" -}}`

The identities one app-id signs as (list $root $appId).

## `{{- define "lance.signingEnv" -}}`

RASK_SIGNING_IDENTITY for one Deployment (list $root $appId): one env row when the store is on and that app-id signs.

## `{{- define "lance.controlSignerRoles" -}}`

Every control-event role -> its identities (values.yaml `signing.doors`).

## `{{- define "lance.doorEnv" -}}`

A door host's env (list $root $takesControlEvents [$signerRoles]); values.yaml `signing.doors`.

## `{{- define "lance.signingShapes" -}}`

The shape checks the mint and seed scripts share: an NKEY user seed is SU + 56 and a public key U + 55 base32 characters.

## `{{- define "lance.platformBuckets" -}}`

The PLATFORM buckets, comma-joined: `objectStore.buckets`. The provisioning hook
(`object-store-provisioning.yaml`) creates them on the dev store and `orphan_buckets` exempts them, both from here.

## `{{- define "lance.maintenancePlatformBuckets" -}}`

Buckets `orphan_buckets` must never report, as ONE expression both maintenance deployments read.
The category's contract is that a clean report certifies the estate, so a finding no operator can
action holds the total above zero forever and blocks the trash purge with it — the config's own
comment records `rask-observability` doing exactly that until `objectStore.buckets` was wired in.

A MULTIBASE DATA BASE IS THE SAME KIND OF THING and was missed: `catalog.multibase.dataBases` is an
estate DECLARATION that a bucket exists and may be addressed, and no warehouse record claims it, so
the reconcile called it an orphan. Measured live 2026-09-24: `orphan_buckets: 'lh067-second-store'`
— declared in this repo's own `values-local.yaml` — was one of the two findings holding
`trash_purge_blocked`. Declared here rather than in each template, because two copies of a bucket
list is how the first one drifted.

## `{{- define "rask.catalogPublicPrefix" -}}`

The public prefix of the catalog's route, and the one value both its `rask.apiRoutes` row and the catalog's
`LANCE_PUBLIC_PATH_PREFIX` render from. The row forwards the path unrewritten: a `URLRewrite` rebuilds the path from its
decoded form, so the `%2B` and `%2F` the catalog judges a name by would arrive as `+` and `/` (measured on Traefik
3.7.8, whose URLRewrite sets `RawPath` from `EscapedPath()`, [[LH-526]]). The catalog strips the prefix itself.

## `{{- define "rask.lineageDatasetNamespace" -}}`

The namespace every table's OpenLineage dataset is named under, rendered into the catalog
(`LANCE_LINEAGE_DATASET_NAMESPACE`), maintenance (`MAINTENANCE_LINEAGE_DATASET_NAMESPACE`) and lineage
(`LINEAGE_CATALOG_DATASET_NAMESPACE`) from this one helper, so the two producers name a table alike and lineage reads a
dataset under any other catalog's namespace as another estate's ([[LH-553]]). It names the catalog's endpoint, as OpenLineage names a catalog's tables by the
catalog serving them (`unitycatalog://{host}`, https://openlineage.io/docs/spec/naming): the release's first public
hostname with the catalog's public prefix, so two estates' tables are two datasets in one graph backend. A development
estate with no hostname names the catalog's in-cluster Service, unique per release and namespace.

## `{{- define "rask.apiRoutes" -}}`

THE ONE ROUTE TABLE: the doors the cluster route publishes, as a JSON list of
`{prefix, component, service, port, rewrite, paths}`, one row per service. Each of `paths` is published at
`<prefix><path>`: with `rewrite` true `route.yaml` renders one rule per path whose `URLRewrite` `ReplacePrefixMatch`
lands on `<path>`; with it false, one rule matching every path, forwarded unchanged. `/docs`, `/redoc` and
`/openapi.json` join each row's paths under `docs.enabled` only. `network-policy.yaml` renders one
`<release>-edge-<component>` policy per row admitting that port, so a published service and an admitted port come from
one list and cannot drift.

The rows mirror the gates of the templates that render each backend: catalog and lineage always
(services.yaml), so the route never names a Service that did not render.

A new door a service mounts at its root is a path in its row; the gate in
`tests/chart/test_the_cluster_route_names_one_backend_per_api_prefix.py` fails until it is, and fails on a path that
reaches only the pod's probes. A client's own prefixes are not rows: its
release publishes them with an HTTPRoute of its own.
