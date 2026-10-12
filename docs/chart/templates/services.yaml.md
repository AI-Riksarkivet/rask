# chart/templates/services.yaml

Rationale for `chart/templates/services.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: apps/v1`

Catalog (producer) — creates Lance tables on S3 and PUBLISHES OpenLineage events to the Dapr
pubsub.jetstream component. The daprd sidecar (injected by the annotations) carries the publish to NATS.

## `args:`

command/args are split so the dev-only reload flags can be appended. These are the
fleet.yaml — so a synced file landed in the pod and the worker never re-read it.

## `- { name: LANCE_MAX_CONCURRENT_WRITES, value: {{ hasKey .Values.catalog "maxConcurrentWrites" | ternary .Va...`

Load-shed cap on concurrent in-flight Arrow-IPC writes (each buffers up to maxBodyBytes).
hasKey+ternary, not `| default`, for the same two reasons as LANCE_AUDIT_ENABLED below: a
key missing under `helm upgrade --reuse-values` must render the default (not "" → pydantic
int_parsing crash), and the meaningful 0 (shedding off) must stay settable — Sprig's
default() treats 0 as empty.

## `- { name: LANCE_TRASH_GRACE_DAYS, value: {{ hasKey .Values.catalog "trashGraceDays" | ternary .Values.catal...`

#75/#96 the recoverable-drop grace period. hasKey+ternary for the same two reasons as
above: `--reuse-values` with the key missing must render the default, and the meaningful
0 (recovery OFF, destroy immediately) must stay settable past Sprig's default().

## `- { name: LANCE_WAREHOUSES_ENABLED, value: {{ .Values.catalog.warehouses.enabled | default false | quote }} }`

#3-A per-warehouse physical multi-tenancy (admin control plane). On → POST /v1/warehouses
provisions a bucket per warehouse + binds namespaces; an unbound namespace still routes to
LANCE_REST_ROOT (backward-compatible). The warehouse registry lives under LANCE_REST_ROOT
(control_root defaults to it), so no extra bucket is needed.

## `{{- $reserved := list }}`

Buckets no warehouse may claim (audit 2026-07-23): the medallion ZONE buckets (gold can
live outside the shared bucket) — the catalog root/registry bucket is always reserved in-app.

## `- { name: LANCE_MULTIBASE_DATA_BASES, value: {{ .Values.catalog.multibase.dataBases | default (list) | join...`

#3-B Lance multi-base DATA distribution: the ALLOWLIST of bucket URIs a create may spread its
fragments across (?data_base=... per request, validated against this list). Empty = feature off.

## `{{- $allow := .Values.catalog.multibase.dataBases | default (list) }}`

[[LH-067]] A base's credential REFERENCE, rendered as the `base-uri=secret-ref` pairs the
setting parses. REFUSED HERE when the base is not also on the allowlist above: the two
values are independent, and the live estate carried a ref for a base the allowlist did
not name — so the request was refused before the per-base credential was ever reached
and nothing said why (measured 2026-09-22). A render-time failure names the mistake; the
runtime one is indistinguishable from a caller inventing a bucket.

## `- { name: LANCE_DAPR_SECRET_S3_FIELD, value: {{ include "lance.catalogSecretField" . | quote }} }`

NAMED HERE rather than left to the service's own default, which is what every sibling
already does (lineage, medallion, maintenance each render their own field). The catalog
was the one that did not, so the FIELD NAME lived only in its image — and when the seed
started writing a differently-named property, a running pod asked OpenBao for a field
that no longer existed and failed closed at startup on an estate whose store was
otherwise healthy. A name the chart writes and the chart seeds cannot drift apart.

## `- { name: LANCE_SECRETS_FROM_DAPR, value: "true" }`

The S3 secret comes ONLY from the Dapr secret store at boot; it is never in pod env. The
catalog fails closed if the store can't provide it. Needs the lance-secrets component
(scoped to catalog).

## `- { name: LANCE_VENDING_MODE, value: {{ .Values.vending.mode | quote }} }`

Data-plane credential vending (Track B). mode_b (default) = server-mediated, no creds leave the
catalog; sts = AssumeRole + per-table session policy against the store's STS endpoint (short-TTL
scoped creds the client uses for direct object I/O).

## `{{- with .Values.vending.encryption }}`

ENCRYPTION AT REST, carried INTO the vended credential (§ J5). Rendered only when
asked for: an empty value would reach the writer as an option object_store does not
recognise, and it drops those silently — plaintext under a config claiming otherwise.
The catalog REFUSES an invalid pairing at boot rather than passing it on.

## `- { name: LANCE_LINEAGE_OUTBOX_URI, value: "s3://{{ .Values.minio.bucket }}/_lineage_outbox" }`

#4 transactional outbox on the catalog's LINEAGE lane. The emit is inline-awaited and
best-effort AFTER the Lance write commits, so without staging a crash between the commit and
the publish loses the write announcement outright — and medallion's /bronze-arrival reacts to
that announcement, so the loss is not a provenance hole but a bronze->silver->gold run that
silently never happens.

The SAME prefix `LINEAGE_OUTBOX_URI` names below, because that is the only thing that drains
it: lineage's reconcile cron re-ingests and re-publishes what it finds there. A private prefix
would stage objects nothing ever reads, which looks like durability and is not.

## `- { name: LANCE_CONTROL_EMIT_ENABLED, value: {{ hasKey .Values.catalog "controlEmit" | ternary .Values.cata...`

Control-plane change-events (opt-in, best-effort — the governance/metadata stream). ON →
mutations publish a CatalogControlEvent onto a DEDICATED broadcast pubsub (no queueGroupName)
under topic catalog.control.v1; every replica buffers every event for GET /v1/events. hasKey+
ternary (NOT `| default`, which treats false as empty) so a key missing under
`helm upgrade --reuse-values` renders the default, not "" (pydantic bool_parsing crash).

## `- { name: LANCE_MAINTENANCE_WORK_PUBSUB, value: {{ printf "%s-%s" $.Values.maintenance.workPubsub $.Values....`

The on-demand compaction door publishes ONE work item here instead of rewriting fragments
in the request handler. Rendered from the SAME values the maintenance service reads, so a
202 is only ever returned where an executor is actually subscribed: `register_work_route`
is gated on this topic too, and a door that accepted work no worker consumes would be
strictly worse than the synchronous rewrite it replaced.

## `- { name: LANCE_MAINTENANCE_INDEX_TOPIC, value: {{ $.Values.maintenance.indexTopic | quote }} }`

Set, create_index / create_scalar_index publish one unit and answer with its id rather
than building inside this handler — the spec's own asynchronous model. The maintenance
service reads the SAME value, so neither side can believe a worker exists when it does not.

## `- { name: LANCE_CONTROL_OUTBOX_URI, value: "s3://{{ .Values.minio.bucket }}/_control_outbox" }`

#4 transactional outbox on the CONTROL lane + the cron that RELAYS it. `table_published` is
the only thing that wakes silver->gold (nothing re-reads the `published` tag), so a publish
lost to a NATS blip ended the cascade with every pod green. Staged here, re-published by
`catalog/api/control_relay.py` on the schedule in catalog-control-relay-cron.yaml.

A PRIVATE PREFIX, never `_lineage_outbox`: lineage's reconcile cron parses everything in
its prefix as an OpenLineage RunEvent and DELETES what it cannot, so a shared prefix
destroys these events rather than relaying them (the catalog refuses to boot on equality).

## `- { name: LANCE_CONTROL_RELAY_BINDING_NAME, value: {{ .Values.catalog.controlOutbox.bindingName | quote }} }`

The binding name IS the route this app serves it on — one string with the Component above.

## `{{- if .Values.dapr.enabled }}`

Explicit APP_API_TOKEN so require_dapr_token can verify the `dapr-api-token` header the sidecar
stamps on /control-events delivery → a forged direct in-cluster POST is 403'd. Without it the
guard silently no-ops and any pod could inject fabricated change-events into every replica's
buffer. Same secret + pattern as the lineage/medallion/compaction apps; the catalog
lifespan also asserts it is set when control eventing is on (fail-closed, not silently open).

## `{{- include "lance.appTokenEnv" (list $root $root.Values.services.catalog.daprAppId) | nindent 12 }}`

Same rule as the medallion's and maintenance's: this verifies the header the SIDECAR
stamps, so without one there is nothing to verify and the Secret is not rendered.

## `- { name: DAPR_API_TIMEOUT_SECONDS, value: {{ .Values.dapr.apiTimeoutSeconds | quote }} }`

Bound the DaprClient gRPC deadline (the SDK env) — the lineage publish is awaited INLINE in
every write handler, so a wedged sidecar/NATS would otherwise hang every catalog write.

## `{{- $cascadeWriters := list .Values.medallion.producer.serviceIdentity }}`

The cascade's WRITERS, granted `writer` + `publisher` + `validator` on every warehouse
this catalog creates — never `owner` (owner ruling 2026-09-10, zero trust). The RUNGS
live in `catalog/api/fga_deps.py::_CASCADE_RUNGS`; this list is only WHO.
Admission and authorization are different questions and this is deliberately NOT
RASK_SA_SUBJECTS: that map says who may SPEAK to the catalog and includes identities
such as maintenance's, which must never hold a cascade rung on a
tenant's data. Only the identities that actually move data between tiers appear here —
the producer (which resumes a human-approved promotion, so it publishes too) and the
stage runners.

A NAME ADDED HERE GETS ALL THREE RUNGS ON EVERY TENANT, which is why the list is
narrow: `validator` carries the accept-assertions override, and only the producer's
resume path actually needs it. Splitting the rungs per subject is the next step and
needs this to render identities WITH their rungs rather than as a flat list.

Without this a tenant is created, its tiers are created, rows land in bronze, lineage
records the run — and the promotion is refused with `403 can_update_tag` in a stage runner log
nobody is watching. Measured five times on the live estate, once AFTER a person had
already approved it.

## `{{- $maintainers := list }}`

THE SWEEP'S OWN RUNG, and the reason it is a second list rather than another entry
in the one above: a cascade identity moves data between tiers and holds
writer/publisher/validator, while maintenance rewrites HOW a dataset is stored and
must hold `maintainer` and nothing else. `can_maintain` neither implies nor is
implied by `can_write_data`, so merging the lists would grant each the other's
authority.
GATED EXACTLY LIKE THE BOOTSTRAP HOOK'S OWN GRANT (`bootstrap-admin.yaml`), so an
estate that vends no write credentials grants nothing — a standing tuple for a
service that is not using it is the kind nobody remembers to revoke.
Measured 2026-09-15: without this the catalog's list was empty in every deployment,
so `cascade_tuples` wrote no maintainer grant, and the 93 warehouses that had one
had it written by hand into the live store.

## `- { name: LANCE_AUDIT_ENABLED, value: {{ hasKey .Values.auth "audit" | ternary .Values.auth.audit true | qu...`

#41 compliance audit trail (who/what/outcome for authn/authz/vending) — configurable.
hasKey+ternary, NOT `| default`: Sprig's default() treats boolean false as empty (making
`false` un-settable), while a bare reference renders "" when the key is missing entirely —
which `helm upgrade --reuse-values` from a pre-#41 release does (live crash 2026-07-15,
pydantic bool_parsing on LANCE_AUDIT_ENABLED=""). hasKey defaults a MISSING key to true and
still honors an explicit false.

## `- { name: LANCE_FGA_LOCK_ROOT_CREATE, value: {{ hasKey .Values.auth "lockRootCreate" | ternary .Values.auth...`

Lock top-level (root) namespace/table creation to holders of a warehouse-level create grant.
Off in dev (self-serve); ON in prod (values-prod) — else any Dex-authenticated user can squat
unlimited root namespaces in the shared warehouse bucket (assessment finding). hasKey+ternary
like LANCE_AUDIT_ENABLED above: a missing key under --reuse-values must render the default,
not "" (pydantic bool_parsing crash).

F2-11. LOCKED BY DEFAULT ON A REAL DEPLOYMENT, and the derivation is the point: this
was an opt-in that only `values-prod.yaml` set, so every estate that did not think
about it let ANY authenticated subject mint a top-level namespace — measured live
2026-09-07, `LANCE_FGA_LOCK_ROOT_CREATE=false` on the running catalog. The same
shape F2-9 already fixed for credentials: answer "is this a real deployment?" once,
on a signal an operator cannot forget, rather than four times on a flag they can.
An explicit `auth.lockRootCreate` still wins in BOTH directions — a real deployment
that genuinely wants open self-serve says so, and says it deliberately.

## `sessionAffinity: ClientIP`

ONE CALLER, ONE REPLICA — because the control-event cursor is per-replica. Each catalog replica
subscribes to `catalog.control.v1` with NO queueGroupName (broadcast), so every replica keeps its
own ring buffer and its own monotonic counter (`core/control_buffer.py`). Round-robined across the
two replicas `values-prod.yaml` runs, the console hands replica A's cursor to replica B, which
reads it against a counter that never issued it.

MEASURED 2026-09-18 on two live replicas, and the symptom is worse than a reset. Thirty polls from
one pod with affinity OFF returned cursors `0, 0, 0, 0, 18, 18, 18, 0` and `reset=0` — a cursor
AHEAD of a replica's counter is not off-the-end, so that replica answers an EMPTY PAGE rather than
a reset. The console is not told to re-read; it is told there is nothing new, and the events that
replica holds are silently skipped. With affinity on, the same thirty polls held cursor 18
throughout.

AFFINITY RATHER THAN A SHARED BUFFER: the events are hints and the audit trail is the durable
record, so the console can always re-read authoritative state. Paying for a shared buffer to fix a
polling artefact is the wrong trade.

`ClientIP` works here because every caller is a POD with a stable address — the console's poll
runs server-side in the home zone and reaches this Service directly (`CATALOG_API`), never through
the gateway. Stage runners, medallion and maintenance are their own pods and keep spreading.
This is NOT a pattern to copy onto the gateway Service, which sits behind the Ingress: there every
request arrives from the ingress controller's single address, and pinning it would send the whole
estate to one replica.

## `timeoutSeconds: 10800`

Longer than any console session, so the cursor is not reset by the affinity expiring between
polls — which would be this same defect on a timer. kube-proxy's own default is 10800s; stated
here so the window is a decision rather than an inherited one.

## `apiVersion: dapr.io/v1alpha1`

Dapr cron INPUT binding — the sidecar POSTs to /<bindingName> on `schedule` to run the periodic
storage->graph reconciliation (B4), back-filling Lance writes whose lineage event was lost. Config lives
here, not in app code (no scheduler thread). Scoped to the lineage app; the route mounts only when
LINEAGE_RECONCILE_BINDING_NAME matches this name. Gated on dapr.enabled too: without the Dapr control
plane the Component CRD doesn't exist and the release would fail to apply.

## `apiVersion: apps/v1`

Lineage (consumer) — SUBSCRIBES (via its daprd sidecar) to the lineage topic and ingests into AGE.

## `args:`

command/args are split so the dev-only reload flags can be appended. These are the
fleet.yaml — so a synced file landed in the pod and the worker never re-read it.

## `{{- include "lance.appTokenEnv" (list $root $root.Values.services.lineage.daprAppId) | nindent 12 }}`

Explicit APP_API_TOKEN (recommended k8s approach) so require_dapr_token can verify the
`dapr-api-token` header the sidecar stamps on delivery → forged direct POSTs are 403'd.

Gated on the sidecar for the reason the guard exists: with no sidecar nothing stamps the
header, so there is nothing to verify — and the Secret is not rendered, so the reference
would dangle and the pod would never start.

## `- { name: LINEAGE_DATABASE_URL, value: "postgresql://{{ .Values.age.user }}@{{ include "lance.ageConnectHos...`

The AGE password is NOT in the connection string in pod env; lineage fetches it from the
secret store at boot and splices it into the URL. The URL carries user/host/db only.

## `- { name: LINEAGE_RUN_RETENTION_DAYS, value: {{ .Values.services.lineage.runRetentionDays | quote }} }`

§4 reliability knobs, all three pruned or bounded by the reconcile cron: Run-node retention
(0 = keep all), durable /events feed retention in days of ARRIVAL time (0 = unbounded), and
the AGE pool's per-statement timeout (bounds runaway traversals).

## `- { name: LINEAGE_DAPR_PUBSUB, value: {{ include "lance.subPubsub" (list $root .Values.services.lineage.dap...`

The lineage app's OWN subscriber component (queueGroupName=lineage, deliverPolicy=all) —
must match the Component name in dapr-component.yaml (both render lance.subPubsub).

## `- { name: LINEAGE_DLQ_TOPIC, value: "dlq.lineage.events" }`

Dapr-native DLQ (ships with the Resiliency CRD): lineage deliveries that exhaust the
sidecar retry schedule park here (visibility only — replay-from-stream stays the recovery).

## `- { name: LINEAGE_DLQ_PUBSUB, value: "{{ include "lance.subPubsub" (list $root .Values.services.lineage.dap...`

The parking subscription's OWN durable component (dapr-component.yaml, `-dlq` suffix) —
never the main deliverPolicy=all component, whose replay would re-park the whole retained
DLQ backlog.

## `- { name: LINEAGE_FRESHNESS_BUDGET_HOURS, value: {{ .Values.services.lineage.freshnessBudgetHours | quote }} }`

Freshness budget (data-contract gap #2): reconcile flags `stale` datasets whose newest
version commit is older than this many hours. Unset/0 = the axis is off.

## `- { name: LINEAGE_DECLARED_COLUMNS, value: {{ $declared | toJson | quote }} }`

Declared-columns patrol (Batch 23): DERIVED from the stage runners' requiredColumns — one
declaration source (values), two enforcement points (the gate at promotion, this sweep
after) — so the gate and the patrol can never disagree on what consumers declared.

## `- { name: DAPR_API_TIMEOUT_SECONDS, value: {{ .Values.dapr.apiTimeoutSeconds | quote }} }`

Bound the DaprClient gRPC deadline (the SDK env) — covers the boot-time secret-store fetch.

## `- { name: LINEAGE_DEMO_DATA_ENABLED, value: {{ hasKey .Values.services.lineage "demoData" | ternary .Values...`

Demo peek router — values-gated (unauthenticated metadata disclosure; prod ships it off).
hasKey+ternary (the LANCE_AUDIT_ENABLED convention): a key missing under --reuse-values (a
stored release predating demoData) must render the default, not "" (bool_parsing boot crash).

## `- { name: LINEAGE_RECONCILE_BINDING_NAME, value: {{ .Values.services.lineage.reconcile.bindingName | quote ...`

Mounts the cron reconcile route at this binding name (matches the bindings.cron component above).

## `{{- $linScoped := .Values.minio.lineageAccessKey }}`

THE LINEAGE PLANE'S SCOPED IDENTITY (§F2-1). The ACCESS KEY alone scopes the plane; the
secret is DERIVED in-cluster (`lance.scopedSecretFn`), so naming the identity needs no
secret anywhere. Empty falls back to the tenant ROOT, which is what an
install runs until one is named — a chart that silently repointed a live service at a
credential nobody created would take it down on upgrade.

## `- { name: LINEAGE_DAPR_SECRET_S3_FIELD, value: "lineage-s3-secret-key" }`

THE OTHER HALF OF THE PAIR, and the half that has bitten twice. `dapr_secret_s3_field`
defaults to `minio-secret-key` — the tenant ROOT's secret — so a SCOPED access key left
on that default is signed with a mismatched pair and every operation fails
`SignatureDoesNotMatch`. The Ray and maintenance pairs each paid for this.

## `- { name: RASK_OIDC_DISCOVERY_URL, value: "http://{{ include "lance.dexHost" . }}:{{ .Values.dex.port }}/de...`

Split-horizon: the issuer is the PUBLIC URL tokens carry; discovery/JWKS are fetched in-cluster.

## `- { name: RASK_OIDC_ALLOW_INSECURE, value: {{ ternary "false" "true" (hasPrefix "https://" .Values.dex.issu...`

Scheme-derived (see the catalog twin above): https issuer => HTTPS guard stays enforced.

## `- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }`

Operator-pinned FGA store id — the same auth.fgaStoreId every consumer + the bootstrap-admin
Job use (see the catalog twin above). Unset = provision-by-name at boot.

## `- { name: LINEAGE_READ_AUDIT_ENABLED, value: {{ hasKey .Values.services.lineage "readAudit" | ternary .Valu...`

Read-side audit (services.lineage.readAudit): record every ALLOWED FGA-gated data read into
lineage_reads — feeds GET /datasets/{name}/readers + the ReadersPanel. Rendered only under
auth.enabled (a real subject exists), default ON like the catalog's write-side
LANCE_AUDIT_ENABLED — and hasKey+ternary for the same --reuse-values reason.

## `- { name: LANCE_AUDIT_ENABLED, value: {{ hasKey .Values.auth "audit" | ternary .Values.auth.audit true | qu...`

#41. The SAME `auth.audit` key the catalog reads, because this service declares the
same `LANCE_AUDIT_ENABLED` alias and the chart rendered it into the catalog alone —
measured live 2026-09-09, the lever reached ONE service of ten while every other pod
ran on its field default (§ Q17-27). hasKey+ternary for the reason recorded at the
catalog's copy: `| default` makes an explicit false un-settable, and a bare reference
renders "" on a `--reuse-values` upgrade, which is a bool_parsing crash at boot.

## `{{- if and (.Values.signing).enforce (include "lance.secretsViaDapr" .) }}`

ENFORCEMENT (values.yaml `signing:`): both sets or neither, and never an empty signer list.
