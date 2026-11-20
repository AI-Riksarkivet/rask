# chart/templates/services.yaml

Rationale for `chart/templates/services.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: apps/v1`

Catalog (producer): creates Lance tables on S3 and publishes its OpenLineage events and its control-plane change events
on NATS JetStream, over a connection of its own as the bus user `lance.busEnv` names it (`lakehouse.events.bus`). On the same
connection each replica follows the control topic into its own buffer (`catalog.api.control_lane`).

## `args:`

No `command:`. Each image's ENTRYPOINT is its tini (`.docker/*.dockerfile`), and a container's `command` replaces the
ENTRYPOINT, so a container that set one made uvicorn its PID 1 ([[LH-313]]). The kernel drops a PID 1's closing re-raise
of SIGTERM, so a threaded request held a PID 1 uvicorn 25.1 s past a 3 s graceful-shutdown bound (measured 2026-09-28,
uvicorn 0.51.0), and it drops a SIGKILL sent from inside the PID 1's own namespace, so nothing in the pod could kill it.
The chart therefore passes uvicorn's whole command line as `args`, which the image's ENTRYPOINT runs, so the init
survives whatever renders the pod: `opentelemetry-instrument` first when tracing is on, then `uvicorn`, the module, bind
address, port and graceful-shutdown flag (`rask.shutdownArg`, sized from the pod's termination grace), one per line.
Pinned by `tests/chart/test_uvicorn_drains_inside_its_pods_grace_period.py`; XC-095's catalog drill proves the PID 1 on
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
hasKey+ternary, not `| default`, for the same two reasons as RASK_AUDIT_ENABLED below: a
key missing under `helm upgrade --reuse-values` must render the default (not "" → pydantic
int_parsing crash), and the meaningful 0 (shedding off) must stay settable — Sprig's
default() treats 0 as empty.

## `- { name: LANCE_TRASH_GRACE_DAYS, value: {{ hasKey .Values.catalog "trashGraceDays" | ternary .Values.catal...`

#75/#96 the recoverable-drop grace period. hasKey+ternary for the same two reasons as
above: `--reuse-values` with the key missing must render the default, and the meaningful
0 (recovery OFF, destroy immediately) must stay settable past Sprig's default(). No `| int`:
a fraction of a day is a valid grace (the schema types it `number`), and truncating one would
turn the dev estate's 0.04 into 0, which switches recoverable drops off ([[LH-518]]).

## `- { name: LANCE_RESERVED_BUCKETS, value: {{ include "lance.platformBuckets" . | quote }} }`

Buckets no warehouse may claim (audit 2026-07-23): the platform's own buckets (`objectStore.buckets`), so no tenant
governs a bucket every tenant's data may sit in. The catalog root and registry bucket are always reserved in-app.

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

## `{{- include "lance.objectStoreSessionEnv" (list $root "LANCE" "catalog") | nindent 12 }}`

THE CATALOG'S STORE IDENTITY IS ITS SERVICEACCOUNT ([[XC-083]], ADR 0170). The pod projects a token for
`objectStore.identity.audience` (the volume `lance.identityTokens` renders), and the catalog exchanges it through STS
`AssumeRoleWithWebIdentity` for every session it signs with: its own IO's, renewed at half life, and each vend's
(`lakehouse.storage.store_session`). It holds no key pair, in its environment or in the store, so nothing it reads outlives a
session. The helper renders the four variables every store-reaching pod gets (`_helpers.tpl.md`
`lance.objectStoreSessionEnv`): the token file, the STS endpoint, the role `objectStore.identity.roleArns.catalog` and
the session length.

The STS endpoint is the store's own, where RustFS and Ceph RGW serve STS. Always named: without it boto3 would send the
catalog's token to the public AWS STS, and the catalog refuses to boot (`Settings._validate_store_identity`). An AWS
deployment needs its regional STS endpoint here, for which the chart has no value yet.

## `{{- include "lance.secretStoreEnv" (list $root .Values.services.catalog.appId) | nindent 12 }}`

THE CATALOG LOGS IN TO THE SECRET STORE AS ITSELF ([[XC-199]]). The store's address, its role `<release>-catalog`, the
mounts and the projected token's path (`lance.secretStoreEnv`, `_helpers.tpl.md`): its secrets (its signing key and its
commit-id key) come ONLY from the store, read under the
process's own login at boot and again on a TTL (ADR 0166). None is in pod env. Lineage's Deployment carries
the same include for its own role.

## `- { name: LANCE_VENDING_MODE, value: {{ .Values.vending.mode | quote }} }`

Data-plane credential vending (Track B). web_identity (default) = the catalog's own token exchanged through
`AssumeRoleWithWebIdentity` with a per-table session policy (short-TTL scoped creds the client uses for direct object
I/O, named for the caller); mode_b = server-mediated, no creds leave the catalog.

## `{{- with .Values.vending.encryption }}`

ENCRYPTION AT REST, carried INTO the vended credential (§ J5). Rendered only when
asked for: an empty value would reach the writer as an option object_store does not
recognise, and it drops those silently — plaintext under a config claiming otherwise.
The catalog REFUSES an invalid pairing at boot rather than passing it on.

## `- { name: LANCE_LINEAGE_EMIT_ENABLED, value: "true" }`

The catalog announces every committed write on the bus. `lance.busEnv` renders `RASK_BUS_URL` and `RASK_BUS_USER` in
every render, since `nats.url` is required, and a catalog told to emit on the bus with no bus named refuses to start
(`catalog.core.config`), so the switch is on wherever the catalog runs. Pinned by
`tests/chart/test_invariants.py::test_the_catalog_is_switched_to_publish_on_the_bus_it_is_named`.

## `- { name: LANCE_LINEAGE_OUTBOX_URI, value: "s3://{{ .Values.objectStore.bucket }}/_lineage_outbox" }`

#4 transactional outbox on the catalog's LINEAGE lane. The emit is inline-awaited and
best-effort AFTER the Lance write commits, so without staging a crash between the commit and
the publish loses the write announcement outright — and a pipeline polling the commit feed reacts
to that announcement, so the loss is not only a provenance hole but a downstream run that silently
never happens.

The SAME prefix `LINEAGE_OUTBOX_URI` names below, because that is the only thing that drains
it: lineage's relay records and re-publishes what it finds there. A private prefix
would stage objects nothing ever reads, which looks like durability and is not.

## `- { name: LANCE_PROVENANCE_NAMESPACE, value: {{ include "lance.provenanceNamespace" $root | quote }} }`

The namespace the catalog keeps for provenance (ADR 0158): lineage's feed, edge list and outbox refusals are
tables in it. It is a namespace of the estate's own warehouse (`lance_catalog$provenance`: the first segment of every
name is its warehouse, and the estate's is the id of `RASK_FGA_DEFAULT_WAREHOUSE_OBJECT`), and the catalog refuses to
boot on one outside it. The catalog creates it at boot, protects it, and never announces a write under it, because each of
those rows records an announcement. Rendered from the same value as lineage's `LINEAGE_PROVENANCE_NAMESPACE`, so
the two services cannot name different namespaces.

## `- { name: LANCE_CONTROL_EMIT_ENABLED, value: {{ hasKey .Values.catalog "controlEmit" | ternary .Values.cata...`

Control-plane change-events (opt-in, best-effort: the governance/metadata stream). ON: mutations publish a
CatalogControlEvent on catalog.control.v1 over the catalog's own connection to the bus, and every replica follows
every event on that connection, by reading the stream by sequence, and
buffers it for GET /management/v1/events. hasKey+
ternary (NOT `| default`, which treats false as empty) so a key missing under
`helm upgrade --reuse-values` renders the default, not "" (pydantic bool_parsing crash).

## `- { name: LANCE_CONTROL_OUTBOX_URI, value: "s3://{{ .Values.objectStore.bucket }}/_control_outbox" }`

#4 transactional outbox on the CONTROL lane + the timer that RELAYS it. A control event lost
to a NATS blip (a grant, a tag's move) reaches nobody — not a notifier on the control
lane, not any replica's GET /management/v1/events buffer — with every pod green. Staged here, re-published by
`catalog/api/control_relay.py` on the catalog's `catalog-control-relay` timer.

A PRIVATE PREFIX, never `_lineage_outbox`: lineage's relay parses everything in
its prefix as an OpenLineage RunEvent and DELETES what it cannot, so a shared prefix
destroys these events rather than relaying them (the catalog refuses to boot on equality).

## `- { name: LANCE_CONTROL_RELAY_PERIOD_SECONDS, value: {{ required "catalog.controlOutbox.periodSeconds is unset —...`

The control relay's period. Each of the catalog's periodic ticks is a fixed timer (`lakehouse.timers`,
`catalog/api/timers.py`): a loop in every catalog replica that wakes once a period, claims the window with one
conditional write of `_timers/<timer>.json` under the control root, and runs the tick only where that write landed, so
a tick runs once a window whatever the replica count. A period of 0 runs no timer. Rendered only where the control
outbox is, since without it there is nothing to relay.

## `- { name: LANCE_ERASURE_RELEASE_PERIOD_SECONDS, value: {{ required "catalog.erasure.releasePeriodSeconds is un...`

The periods of the catalog's other timers (`LANCE_ERASURE_RELEASE_PERIOD_SECONDS`,
`LANCE_COMMIT_RECONCILE_PERIOD_SECONDS`,
`LANCE_DECLARATION_CONVERGE_PERIOD_SECONDS`, `LANCE_MANIFEST_RECLAIM_PERIOD_SECONDS`), each claimed per window as the
control relay's is. Rendered whatever carries the bus: a timer needs only the object store and the catalog. Each is
`required`, so a release upgraded with `--reuse-values` from one without them fails the render rather than running no
timer.

## `{{- if not .Values.autoscaling.enabled }}`

While an HPA scales a service, its Deployment renders no `replicas`: a rendered count is desired state, and Argo CD's
self-heal would set it back each time the HPA moved it, so the two would flap ([[XC-164]]). Without the field the
Deployment keeps the count the HPA last set. Turning autoscaling on in an existing release removes the field, which sets
the Deployment to one replica until the HPA scales it back to `autoscaling.minReplicas`, within its first sync period.
`tests/chart/test_an_autoscaled_service_leaves_its_replica_count_to_the_autoscaler.py` holds it.

## `- { name: LANCE_FGA_MAINTAINERS, value: {{ include "lance.fgaMaintainers" $root | quote }} }`

MAINTENANCE'S OWN RUNG: maintenance rewrites HOW a dataset is stored and must hold
`maintainer` and nothing else. `can_maintain` neither implies nor is implied by
`can_write_data`, so no writer identity may ride this list. ADR 0156 C4 narrows the
maintain vend tier to this identity alone (LH-473, #599).
GATED by `maintenance.vendWriteCredentials` (`lance.fgaMaintainers`), so an
estate that vends no write credentials grants nothing — a standing tuple for a
service that is not using it is the kind nobody remembers to revoke.
Measured 2026-09-15: without this the catalog's list was empty in every deployment,
so `cascade_tuples` (the warehouse-create grants, `catalog/api/fga_deps.py`) wrote no maintainer grant, and the 93 warehouses that had one
had it written by hand into the live store.

## `- { name: LANCE_OWN_ACCOUNTS, value: {{ include "lance.ownAccounts" $root | quote }} }`

The release's own accounts (`lance.ownAccounts`), whose machine roles on the estate the catalog's declaration converge
holds to what git declares ([[XC-011]]): the catalog's boot takes back a
`writer` on the provenance namespace that `LANCE_PROVENANCE_WRITERS` no longer names only from one of these, so a
`writer` granted at the namespace's access door to an account outside the release stands ([[LH-563]]). Rendered empty,
the boot takes back nothing, and a writer the release stopped naming keeps writing lineage's tables.

## `- { name: RASK_AUDIT_ENABLED, value: {{ hasKey .Values.auth "audit" | ternary .Values.auth.audit true | qu...`

#41 compliance audit trail (who/what/outcome for authn/authz/vending) — configurable.
hasKey+ternary, NOT `| default`: Sprig's default() treats boolean false as empty (making
`false` un-settable), while a bare reference renders "" when the key is missing entirely —
which `helm upgrade --reuse-values` from a pre-#41 release does (live crash 2026-07-15,
pydantic bool_parsing on RASK_AUDIT_ENABLED=""). hasKey defaults a MISSING key to true and
still honors an explicit false.

## `apiVersion: apps/v1`

Lineage (consumer): pulls the lineage topic from the durable consumer `lineage-ingest` on its own connection to the bus
(`lineage.api.ingest_lane`, `lance.busEnv`) and records each event in its feed and edge tables, Lance tables the
catalog governs (ADR 0158). It keeps no database. Its replicas share the consumer, so each event is delivered to one
of them.

## `args:`

No `command:`. Each image's ENTRYPOINT is its tini (`.docker/*.dockerfile`), and a container's `command` replaces the
ENTRYPOINT, so a container that set one made uvicorn its PID 1 ([[LH-313]]). The kernel drops a PID 1's closing re-raise
of SIGTERM, so a threaded request held a PID 1 uvicorn 25.1 s past a 3 s graceful-shutdown bound (measured 2026-09-28,
uvicorn 0.51.0), and it drops a SIGKILL sent from inside the PID 1's own namespace, so nothing in the pod could kill it.
The chart therefore passes uvicorn's whole command line as `args`, which the image's ENTRYPOINT runs, so the init
survives whatever renders the pod: `opentelemetry-instrument` first when tracing is on, then `uvicorn`, the module, bind
address, port and graceful-shutdown flag (`rask.shutdownArg`, sized from the pod's termination grace), one per line.
Pinned by `tests/chart/test_uvicorn_drains_inside_its_pods_grace_period.py`; XC-095's catalog drill proves the PID 1 on
the kind lane.

`rask.proxyHeaderArgs` adds `--proxy-headers --forwarded-allow-ips=<forwardedAllowIps>`: the
cluster route sends public traffic here through the Gateway's data plane, so without trusting its
`X-Forwarded-For` every anonymous request carries the data plane's address and the rate limiter keys
the whole public estate as one client.

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
the secret in the secret store, which lineage reads under its own login on every tick, and only lineage's store role
reads it (`lance.storeReads`).

## `{{- $delivery := required "services.lineage.delivery is unset — upgrade with --reset-then-reuse-values, not --reuse-values" .Values.services.lineage.delivery }}`

The ingest lane's numbers, rendered from the same values the stream Job declares `lineage-ingest` with
(`lance.busConsumers`), so the bound lineage handles at once and the consumer's `max_ack_pending` are one value, and the
ack wait lineage cuts a handling 10 s short of is the consumer's own. `LINEAGE_DELIVERY_ATTEMPTS` and
`LINEAGE_DELIVERY_RETRY_SECONDS` are lineage's own rule for ending a delivery (`lakehouse.events.bus_delivery`): a delivery
it cannot record is asked for again after the retry delay, and on its last attempt is parked in the provenance
namespace's `refusals` table, from which the estate admin's replay door (`POST /admin/dlq/parked/{run_id}/replay`)
records it. The consumer's own `max_deliver` is unlimited, so this rule is the only one that ends a delivery; nothing
is published to a dead-letter topic. 5 attempts 120 s apart (the defaults) ride out an outage of 8 minutes. Each is
`required`, so a release upgraded with `--reuse-values` from one without them fails the render.

## `- { name: LINEAGE_RELAY_PERIOD_SECONDS, value: {{ required "services.lineage.relay.periodSeconds is unset — up...`

The relay's period: lineage runs its tick on a fixed timer (`lakehouse.timers`, `lineage/api/relay.py`), a loop in
every replica whose window one replica claims with a conditional write, so the tick runs once a window whatever the
replica count. Never a schedule on the bus: the relay is what records events while the bus is away.

## `- { name: LINEAGE_TIMER_ROOT, value: "s3://{{ .Values.objectStore.bucket }}" }`

The control root the relay's claim record lives under, `_timers/lineage-relay.json`. Lineage's store policy reaches
that one key with get and put and nothing else beside its outbox (`ClaimItsOwnTimersWindows`, `_helpers.tpl.md`).

## `{{- include "lance.objectStoreSessionEnv" (list $root "LINEAGE" "lineage") | nindent 12 }}`

LINEAGE'S STORE IDENTITY IS ITS SERVICEACCOUNT ([[XC-084]], ADR 0170). The pod projects a token for
`objectStore.identity.audience` beside its `rask-catalog` one, and lineage exchanges it through STS
`AssumeRoleWithWebIdentity` for the sessions its outbox drain signs with (`lakehouse.storage.store_session`). It holds no key
pair. The session's reach is its canned policy, `<release>-sa-lineage` in the ConfigMap
`<release>-object-store-identities` (`lance.objectStorePolicy`): list, get and delete under `_lineage_outbox/`, get and
put on its relay timer's claim record `_timers/lineage-relay.json`, and nothing else, so a compromised lineage pod can
drain its own outbox and reach no table.

## `{{- include "lance.governedOidcEnv" . | nindent 12 }}`

Lineage's OIDC coordinates come from the same helper as every other governed door, so its trust anchor
cannot drift from theirs: `lance.governedOidcEnv` in _helpers.tpl.

## `- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }`

Operator-pinned FGA store id — the same auth.fgaStoreId every consumer + the model hook
use (see the catalog twin above). Unset = provision-by-name at boot.

## `- { name: RASK_AUDIT_ENABLED, value: {{ hasKey .Values.auth "audit" | ternary .Values.auth.audit true | qu...`

#41. The SAME `auth.audit` key the catalog reads, because this service declares the
same `RASK_AUDIT_ENABLED` alias and the chart rendered it into the catalog alone —
measured live 2026-09-09, the lever reached ONE service of ten while every other pod
ran on its field default (§ Q17-27). hasKey+ternary for the reason recorded at the
catalog's copy: `| default` makes an explicit false un-settable, and a bare reference
renders "" on a `--reuse-values` upgrade, which is a bool_parsing crash at boot.

## `{{- $signers := include "lance.signingIdentities" . | splitList " " | compact }}`

ENFORCEMENT (values.yaml `signing:`): wherever auth is on, both sets, and never an empty signer list.
