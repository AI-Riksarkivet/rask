# chart/templates/services.yaml

Rationale for `chart/templates/services.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: apps/v1`

Catalog (producer) — creates Lance tables on S3 and PUBLISHES OpenLineage events to the Dapr
pubsub.jetstream component. The daprd sidecar (injected by the annotations) carries the publish to NATS.

## `args:`

No `command:`. Each image's ENTRYPOINT is its tini (`.docker/*.dockerfile`), and a container's `command` replaces the
ENTRYPOINT, so a container that set one made uvicorn its PID 1 ([[LH-313]]). The kernel drops a PID 1's closing re-raise
of SIGTERM, so a threaded request held a PID 1 uvicorn 25.1 s past a 3 s graceful-shutdown bound (measured 2026-09-28,
uvicorn 0.51.0), and it drops a SIGKILL sent from inside the PID 1's own namespace, so nothing in the pod could kill it.
The chart therefore passes uvicorn's whole command line as `args`, which the image's ENTRYPOINT runs, so the init
survives whatever renders the pod: `opentelemetry-instrument` first when tracing is on, then `uvicorn`, the module, bind
address, port and graceful-shutdown flag (`rask.shutdownArg`, sized from the pod's termination grace), one per line.
Pinned by `tests/unit/test_uvicorn_drains_inside_its_pods_grace_period.py`; XC-095's catalog drill proves the PID 1 on
the kind lane.

`rask.proxyHeaderArgs` adds `--proxy-headers --forwarded-allow-ips=<forwardedAllowIps>`: the
cluster route sends public traffic here through the Gateway's data plane, so without trusting its
`X-Forwarded-For` every anonymous request carries the data plane's address and the rate limiter keys
the whole public estate as one client.

## `- { name: LANCE_PUBLIC_PATH_PREFIX, value: {{ include "rask.catalogPublicPrefix" . | quote }} }`

The prefix the cluster route publishes the catalog under, which its route forwards unrewritten and the catalog strips
from the bytes its client sent (`catalog.api.path_reading`, [[LH-526]]). Named by the same helper as the route's row in
`rask.apiRoutes`, so the prefix routed and the prefix stripped cannot drift; the route render test holds them equal.

## `- { name: LANCE_MAX_CONCURRENT_WRITES, value: {{ hasKey .Values.catalog "maxConcurrentWrites" | ternary .Va...`

Load-shed cap on concurrent in-flight Arrow-IPC writes (each buffers up to maxBodyBytes).
hasKey+ternary, not `| default`, for the same two reasons as LANCE_AUDIT_ENABLED below: a
key missing under `helm upgrade --reuse-values` must render the default (not "" → pydantic
int_parsing crash), and the meaningful 0 (shedding off) must stay settable — Sprig's
default() treats 0 as empty.

## `- { name: LANCE_TRASH_GRACE_DAYS, value: {{ hasKey .Values.catalog "trashGraceDays" | ternary .Values.catal...`

#75/#96 the recoverable-drop grace period. hasKey+ternary for the same two reasons as
above: `--reuse-values` with the key missing must render the default, and the meaningful
0 (recovery OFF, destroy immediately) must stay settable past Sprig's default(). No `| int`:
a fraction of a day is a valid grace (the schema types it `number`), and truncating one would
turn the dev estate's 0.04 into 0, which switches recoverable drops off ([[LH-518]]).

## `- { name: LANCE_WAREHOUSES_ENABLED, value: {{ .Values.catalog.warehouses.enabled | default false | quote }} }`

#3-A per-warehouse physical multi-tenancy (admin control plane). On → POST /management/v1/warehouses
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

## `- { name: LANCE_S3_WEB_IDENTITY_TOKEN_FILE, value: "/var/run/secrets/rask/identity/{{ include "lance.objectStoreAudience" . }}/token" }`

THE CATALOG'S STORE IDENTITY IS ITS SERVICEACCOUNT ([[XC-083]], ADR 0170). The pod projects a token for
`objectStore.identity.audience` (the volume `lance.identityTokens` renders), and the catalog exchanges it through STS
`AssumeRoleWithWebIdentity` for every session it signs with: its own IO's, renewed at half life, and each vend's. It
holds no key pair, in its environment or in the store, so nothing it reads outlives a session.

## `- { name: LANCE_S3_STS_ENDPOINT, value: "{{ include "lance.s3Endpoint" . }}" }`

The store's own endpoint, where RustFS and Ceph RGW serve STS. Always named: without it boto3 would send the catalog's
token to the public AWS STS, and the catalog refuses to boot (`Settings._validate_store_identity`). An AWS deployment
needs its regional STS endpoint here, for which the chart has no value yet.

## `- { name: LANCE_DAPR_SECRET_S3_FIELD, value: {{ include "lance.baseSecretField" . | quote }} }`

The field a data base's credential reference carries its secret in (`catalog.multibase.baseCredentialRefs`), named
here, as lineage and maintenance name theirs, so a field the seed writes and a field the catalog asks for cannot drift
apart.

## `- { name: LANCE_SECRETS_FROM_DAPR, value: "true" }`

The catalog's secrets (its signing key, a data base's credential reference) come ONLY from the Dapr secret store, read
at boot and again on a TTL (ADR 0166); none is in pod env. With it on, the catalog refuses to boot without its store
token (`Settings._validate_store_identity`), so no key pair can stand in for its ServiceAccount. Needs the
lance-secrets component (scoped to catalog).

## `- { name: LANCE_VENDING_MODE, value: {{ .Values.vending.mode | quote }} }`

Data-plane credential vending (Track B). web_identity (default) = the catalog's own token exchanged through
`AssumeRoleWithWebIdentity` with a per-table session policy (short-TTL scoped creds the client uses for direct object
I/O, named for the caller); mode_b = server-mediated, no creds leave the catalog.

## `{{- with .Values.vending.encryption }}`

ENCRYPTION AT REST, carried INTO the vended credential (§ J5). Rendered only when
asked for: an empty value would reach the writer as an option object_store does not
recognise, and it drops those silently — plaintext under a config claiming otherwise.
The catalog REFUSES an invalid pairing at boot rather than passing it on.

## `- { name: LANCE_LINEAGE_OUTBOX_URI, value: "s3://{{ .Values.objectStore.bucket }}/_lineage_outbox" }`

#4 transactional outbox on the catalog's LINEAGE lane. The emit is inline-awaited and
best-effort AFTER the Lance write commits, so without staging a crash between the commit and
the publish loses the write announcement outright — and a pipeline polling the commit feed reacts
to that announcement, so the loss is not only a provenance hole but a downstream run that silently
never happens.

The SAME prefix `LINEAGE_OUTBOX_URI` names below, because that is the only thing that drains
it: lineage's relay cron records and re-publishes what it finds there. A private prefix
would stage objects nothing ever reads, which looks like durability and is not.

## `- { name: LANCE_PROVENANCE_NAMESPACE, value: {{ .Values.services.lineage.provenanceNamespace | quote }} }`

The namespace the catalog keeps for provenance (ADR 0158): lineage's feed, edge list and outbox refusals are
tables in it. The catalog creates it at boot, protects it, and never announces a write under it, because each of
those rows records an announcement. Rendered from the same value as lineage's `LINEAGE_PROVENANCE_NAMESPACE`, so
the two services cannot name different namespaces.

## `- { name: LANCE_CONTROL_EMIT_ENABLED, value: {{ hasKey .Values.catalog "controlEmit" | ternary .Values.cata...`

Control-plane change-events (opt-in, best-effort — the governance/metadata stream). ON →
mutations publish a CatalogControlEvent onto a DEDICATED broadcast pubsub (no queueGroupName)
under topic catalog.control.v1; every replica buffers every event for GET /management/v1/events. hasKey+
ternary (NOT `| default`, which treats false as empty) so a key missing under
`helm upgrade --reuse-values` renders the default, not "" (pydantic bool_parsing crash).

## `- { name: LANCE_CONTROL_OUTBOX_URI, value: "s3://{{ .Values.objectStore.bucket }}/_control_outbox" }`

#4 transactional outbox on the CONTROL lane + the cron that RELAYS it. A control event lost
to a NATS blip (a grant, a `table_published`) reaches nobody — not the notifications control
lane, not any replica's GET /management/v1/events buffer — with every pod green. Staged here, re-published by
`catalog/api/control_relay.py` on the schedule in catalog-control-relay-cron.yaml.

A PRIVATE PREFIX, never `_lineage_outbox`: lineage's relay cron parses everything in
its prefix as an OpenLineage RunEvent and DELETES what it cannot, so a shared prefix
destroys these events rather than relaying them (the catalog refuses to boot on equality).

## `- { name: LANCE_CONTROL_RELAY_BINDING_NAME, value: {{ .Values.catalog.controlOutbox.bindingName | quote }} }`

The binding name IS the route this app serves it on — one string with the Component above.

## `{{- if .Values.dapr.enabled }}`

Explicit APP_API_TOKEN so require_dapr_token can verify the `dapr-api-token` header the sidecar
stamps on /control-events delivery → a forged direct in-cluster POST is 403'd. Without it the
guard silently no-ops and any pod could inject fabricated change-events into every replica's
buffer. Same secret + pattern as the lineage and maintenance apps; the catalog
lifespan also asserts it is set when control eventing is on (fail-closed, not silently open).

## `{{- include "lance.appTokenEnv" (list $root $root.Values.services.catalog.daprAppId) | nindent 12 }}`

Same rule as maintenance's: this verifies the header the SIDECAR
stamps, so without one there is nothing to verify and the Secret is not rendered.

## `- { name: DAPR_API_TIMEOUT_SECONDS, value: {{ .Values.dapr.apiTimeoutSeconds | quote }} }`

Bound the DaprClient gRPC deadline (the SDK env) — the lineage publish is awaited INLINE in
every write handler, so a wedged sidecar/NATS would otherwise hang every catalog write.

## `{{- $maintainers := list }}`

THE SWEEP'S OWN RUNG: maintenance rewrites HOW a dataset is stored and must hold
`maintainer` and nothing else. `can_maintain` neither implies nor is implied by
`can_write_data`, so no writer identity may ride this list. ADR 0156 C4 narrows the
maintain vend tier to this identity alone (LH-473, #599).
GATED EXACTLY LIKE THE BOOTSTRAP HOOK'S OWN GRANT (`bootstrap-admin.yaml`), so an
estate that vends no write credentials grants nothing — a standing tuple for a
service that is not using it is the kind nobody remembers to revoke.
Measured 2026-09-15: without this the catalog's list was empty in every deployment,
so `cascade_tuples` (the warehouse-create grants, `catalog/api/fga_deps.py`) wrote no maintainer grant, and the 93 warehouses that had one
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

`ClientIP` works here because every caller it governs is a POD with a stable address reaching the
Service through kube-proxy — the console's poll runs server-side in the home zone and reaches this
Service directly (`CATALOG_API`), not through the cluster route. Maintenance runs its own pods and
keeps spreading.
Public traffic through the Gateway is not pinned by this: a Gateway API data plane balances across the
Service's endpoints itself rather than through its ClusterIP, so the data plane's single source
address never meets kube-proxy's affinity. An implementation that forwarded to the ClusterIP instead
would send every edge request to one replica.

## `timeoutSeconds: 10800`

Longer than any console session, so the cursor is not reset by the affinity expiring between
polls — which would be this same defect on a timer. kube-proxy's own default is 10800s; stated
here so the window is a decision rather than an inherited one.

## `apiVersion: dapr.io/v1alpha1`

Dapr cron INPUT binding — the sidecar POSTs to /<bindingName> on `schedule` to run lineage's relay tick:
drain the lineage outbox, prune the feed, forward the feed to the configured graph backend. Config lives
here, not in app code (no scheduler thread). Scoped to the lineage app; the route mounts only when
LINEAGE_RELAY_BINDING_NAME matches this name. Gated on dapr.enabled too: without the Dapr control
plane the Component CRD doesn't exist and the release would fail to apply.

## `apiVersion: apps/v1`

Lineage (consumer) — SUBSCRIBES (via its daprd sidecar) to the lineage topic and records each event in its
feed and edge tables, Lance tables the catalog governs (ADR 0158). It keeps no database.

## `args:`

No `command:`. Each image's ENTRYPOINT is its tini (`.docker/*.dockerfile`), and a container's `command` replaces the
ENTRYPOINT, so a container that set one made uvicorn its PID 1 ([[LH-313]]). The kernel drops a PID 1's closing re-raise
of SIGTERM, so a threaded request held a PID 1 uvicorn 25.1 s past a 3 s graceful-shutdown bound (measured 2026-09-28,
uvicorn 0.51.0), and it drops a SIGKILL sent from inside the PID 1's own namespace, so nothing in the pod could kill it.
The chart therefore passes uvicorn's whole command line as `args`, which the image's ENTRYPOINT runs, so the init
survives whatever renders the pod: `opentelemetry-instrument` first when tracing is on, then `uvicorn`, the module, bind
address, port and graceful-shutdown flag (`rask.shutdownArg`, sized from the pod's termination grace), one per line.
Pinned by `tests/unit/test_uvicorn_drains_inside_its_pods_grace_period.py`; XC-095's catalog drill proves the PID 1 on
the kind lane.

`rask.proxyHeaderArgs` adds `--proxy-headers --forwarded-allow-ips=<forwardedAllowIps>`: the
cluster route sends public traffic here through the Gateway's data plane, so without trusting its
`X-Forwarded-For` every anonymous request carries the data plane's address and the rate limiter keys
the whole public estate as one client.

## `{{- include "lance.appTokenEnv" (list $root $root.Values.services.lineage.daprAppId) | nindent 12 }}`

Explicit APP_API_TOKEN (recommended k8s approach) so require_dapr_token can verify the
`dapr-api-token` header the sidecar stamps on delivery → forged direct POSTs are 403'd.

Gated on the sidecar for the reason the guard exists: with no sidecar nothing stamps the
header, so there is nothing to verify — and the Secret is not rendered, so the reference
would dangle and the pod would never start.

## `- { name: LINEAGE_CATALOG_URL, value: {{ printf "http://%s-catalog:%v" (include "lance.fullname" $root) $r...`

Lineage's store IS the catalog: every row of its feed, edge list and outbox refusals is written through the
catalog's `merge_insert` door and read back through its `query` door, as lineage's own principal, so each write is
authorized, judged and audited like any other writer's.

## `- { name: RASK_CATALOG_IDENTITY_TOKEN_FILE, value: /var/run/secrets/rask/identity/rask-catalog/token }`

The projected `rask-catalog` ServiceAccount token (the `lance.identityTokens` mount below), which lineage presents
to the catalog and re-reads on every call: the kubelet rotates it before it expires. The catalog grants this
subject `writer` on the provenance namespace (`LANCE_PROVENANCE_WRITERS`), which reaches its tables and nothing else.

## `- { name: LINEAGE_EVENTS_RETENTION_DAYS, value: {{ .Values.services.lineage.eventsRetentionDays | quote }} }`

Durable /events feed retention in days of ARRIVAL time (0 = unbounded), pruned on the relay tick.

## `{{- with .Values.services.lineage.forward }}`

The forwarder (ADR 0158): the relay tick sends every recorded event, as standard OpenLineage over the HTTP transport, to
`<url>/<endpoint>`; an empty URL forwards nothing. The API key is never a value: `LINEAGE_FORWARD_API_KEY_SECRET` names
the secret in the Dapr secret store lineage reads it from on every tick, and only lineage's Configuration allows it and
only lineage's store login reads it (`lance.storeReads`).

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

## `- { name: DAPR_API_TIMEOUT_SECONDS, value: {{ .Values.dapr.apiTimeoutSeconds | quote }} }`

Bound the DaprClient gRPC deadline (the SDK env) — covers the boot-time secret-store fetch.

## `- { name: LINEAGE_RELAY_BINDING_NAME, value: {{ .Values.services.lineage.relay.bindingName | quote }} }`

Mounts the relay tick's route at this binding name (matches the bindings.cron component above).

## `{{- $_ := required "objectStore.lineageAccessKey names lineage's scoped store identity; …" .Values.objectStore.lineageAccessKey }}`

THE LINEAGE PLANE'S SCOPED IDENTITY (§F2-1), and `required`: a blank access key fails the render, since the only
other pair lineage could present is the store's root, whose secret signs every STS session token on RustFS
(ADR 0168). The ACCESS KEY alone scopes the plane; the secret is DERIVED in-cluster (`lance.scopedSecretFn`), so
naming the identity needs no secret anywhere.

## `- { name: LINEAGE_DAPR_SECRET_KEY, value: "lineage-s3" }`

Lineage's own secret in the store, which only lineage's Configuration allows and only its store login reads ([[XC-165]]).

## `- { name: LINEAGE_DAPR_SECRET_S3_ID_FIELD, value: "access_key" }`

The scoped identity's key id, read from the store with its secret ([[XC-082]], ADR 0166): a rotation
issues a new id, so the id cannot be configuration.

## `- { name: LINEAGE_DAPR_SECRET_S3_FIELD, value: "secret_key" }`

THE OTHER HALF OF THE PAIR, named beside the key id because a scoped key signed with any other
secret fails `SignatureDoesNotMatch` on every operation (measured on the Ray and maintenance pairs).
`dapr_secret_s3_field` defaults to the same field, so a service run without the chart reads its own pair too.

## `{{- include "lance.governedOidcEnv" . | nindent 12 }}`

Lineage's OIDC coordinates come from the same helper as every other governed door, so its trust anchor
cannot drift from theirs: `lance.governedOidcEnv` in _helpers.tpl.

## `- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }`

Operator-pinned FGA store id — the same auth.fgaStoreId every consumer + the bootstrap-admin
Job use (see the catalog twin above). Unset = provision-by-name at boot.

## `- { name: LANCE_AUDIT_ENABLED, value: {{ hasKey .Values.auth "audit" | ternary .Values.auth.audit true | qu...`

#41. The SAME `auth.audit` key the catalog reads, because this service declares the
same `LANCE_AUDIT_ENABLED` alias and the chart rendered it into the catalog alone —
measured live 2026-09-09, the lever reached ONE service of ten while every other pod
ran on its field default (§ Q17-27). hasKey+ternary for the reason recorded at the
catalog's copy: `| default` makes an explicit false un-settable, and a bare reference
renders "" on a `--reuse-values` upgrade, which is a bool_parsing crash at boot.

## `{{- if (.Values.signing).enforce }}`

ENFORCEMENT (values.yaml `signing:`): both sets or neither, and never an empty signer list.
