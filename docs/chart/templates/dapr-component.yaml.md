# chart/templates/dapr-component.yaml

Rationale for `chart/templates/dapr-component.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled (not (.Capabilities.APIVersions.Has "dapr.io/v1alpha1")) }}`

The Dapr control plane is a prerequisite (ADR 0153), installed on its own release; rask never installs it. A render
with `dapr.enabled` on against a cluster that does not serve `dapr.io/v1alpha1` fails here, naming the fix, rather than
applying Components the API server refuses.

## `{{- $scopes := splitList "," (include "lance.sidecarScopes" .) | compact }}`

The apps whose pods run a sidecar and log in to the store: the catalog's and lineage's (for their cron ticks) and each
reader of `openbao.externalReaders`. `lance.sidecarScopes` is the store-reads table's apps less maintenance, whose worker
pod runs no sidecar (`_helpers.tpl.md`). With no such app the Component is not rendered.

## `apiVersion: dapr.io/v1alpha1`

Dapr secret store fronting the dev OpenBao or the platform's Vault or OpenBao (Vault-API compatible). No component of
this chart names a secret, so a lakehouse sidecar logs in through it and resolves nothing, and no app reads a secret
through it ([[XC-199]]): each service logs in to the store itself (`lakehouse.governed.secrets`), and the catalog's and
lineage's Configurations deny the sidecar's secrets API whole (`lance.daprAppSpec`). A reader in
`openbao.externalReaders` reads its own secrets through it, under its own role. The chart renders no store
([[XC-149]]): it names the dev fixture's (`openbao.enabled`) or the platform's (`openbao.externalAddr`).

## `- { name: vaultAuthMethod, value: kubernetes }`

EACH SIDECAR LOGS IN AS ITS OWN APP ([[XC-165]], ADR 0169). Dapr's Vault store logs in with the store's Kubernetes auth
method from 1.19 on (components-contrib#4448), presenting the pod's own ServiceAccount token and renewing its session in
the background, logging in again with the kubelet's current token when the session's max TTL ends. So no store token
exists anywhere: none rides this component, a Kubernetes Secret or a file, and none needs minting or rotating. The
static token Dapr 1.18 offered was read once at init and never renewed (v1.18.0 `vault.go:400-421`), so it could not be
both scoped to one app and short-lived.

## `- { name: vaultKubernetesRole, value: {{ include "lance.storeRole" (list . "{appID}") | quote }} }`

ONE COMPONENT, ONE ROLE PER APP. Dapr fills `{appID}` into any component's metadata when the sidecar starts
(`pkg/runtime/meta/meta.go:96-97`, 1.18.1 and 1.19.0-rc.4; documented as templated metadata values), so every app's
sidecar logs in to `<release>-<its own app id>`, a role bound to that app's ServiceAccount whose policy reads exactly its
app's secrets (`lance.storeReads`). The app's own process logs in to the same role, named in its environment
(`lance.secretStoreEnv`). The store refuses the rest, and a pod that claimed another app's id would still present its own
account's token, which that app's role does not admit.

## `- { name: vaultServiceAccountTokenPath, value: {{ include "lance.storeTokenFile" . }} }`

The token daprd logs in with, which the pod projects for `openbao.identity.audience` and mounts into daprd and into the
app's own container at this one path (`lance.storeTokenFile`, mounted by `lance.storeIdentity`): both are the pod's one
identity under one policy. The file is re-read on every login, so the kubelet's rotation reaches it.

## `- { name: caCert, value: {{ include "lance.storeCaFile" . }} }`

Rendered only where `openbao.ca.configMap` names the platform store's private CA: the file the sidecar mounts from that
ConfigMap (`lance.storeCaFile`, mounted by `lance.storeIdentity`), which the component checks the store's certificate against (components-contrib
`secretstores/hashicorp/vault/vault.go`, `caCert`: a path to a PEM file, read at e90662b02d70).

## `- { name: vaultKVUsePrefix, value: "false" }`

Dapr defaults vaultKVPrefix to "dapr", so it reads secret/data/dapr/<key>; our seed writes
secret/data/<key>. Drop the prefix so the path is the natural secret/data/<key> (the kv-v2 nuance).

## `- { name: skipVerify, value: "false" }`

TLS is verified wherever there is TLS: the platform's store is https (`lance.platformStoreAddr` refuses anything
else); the dev store is plain http, where the setting has nothing to verify, and 1.19 logs every `true` as unsafe.

## `{{- range $scopes }}`

ONE derivation, `lance.sidecarScopes` over `lance.secretScopes`, because these lists have other readers: the per-app
Configurations (observability.yaml) are rendered for the same apps, and `lance.appTokenEnv` decides by membership in
`lance.secretScopes` whether a workload takes its app-API token from the store or through the environment. Restating
the computation would let them agree at review and diverge later: a pod told to take its app token from a store it was
named no address for fails its start closed.

## `{{- if .Values.dapr.enabled }}`

The catalog commit reconcile cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

LH-497: the commit is the outbox. A write is announced after its commit, and a crash between the commit and the staging of its event loses the announcement; this cron has the catalog read each table's version log after its mark and announce every committed version lineage's feed lacks, under the run id derived from the commit and signed for the commit's author. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and an announcement two replicas make at once lands one feed row, since both carry the commit's run id.

## `{{- if and .Values.dapr.enabled .Values.catalog.controlEmit .Values.catalog.controlOutbox.enabled }}`

The catalog control relay cron binding (from its own rationale below).

### `{{- if and .Values.dapr.enabled .Values.catalog.controlEmit .Values.catalog.controlOu...`

THE CONTROL LANE'S RELAY (the other half of the #4 outbox, for `catalog.control.v1`).
The catalog STAGES every control event to `LANCE_CONTROL_OUTBOX_URI` before publishing and drops
it only once the stream acknowledged it, so a NATS blip leaves the event durable. Until this Component existed nothing read
that prefix: the estate's only outbox drain is lineage's reconcile cron, which parses each staged
object as an OpenLineage RunEvent and DELETES what it cannot — so a control event handed to it is
destroyed as poison rather than relayed. (The catalog now refuses to boot with the two prefixes
equal, for exactly that reason.)

What that cost: a subscriber that acts on `table_published` learns of a publication only from this
lane, so one lost publish was a publication nobody downstream heard of, with the tag advanced, the
route 200 and every pod green.

Delivered to `POST /<component name>` at the catalog's pod ROOT (never under a prefix), guarded by
require_dapr_token so only the sidecar's cron may drive the replay. The Component's name, the app's
LANCE_CONTROL_RELAY_BINDING_NAME and the served path are ONE string.

Like every cron binding here: schedule as Component config so the
cadence is a values edit rather than an app redeploy, scoped to the one consuming app-id, gated on
dapr.enabled (no control plane, no Component CRD, and the release would fail to apply).

## `{{- if .Values.dapr.enabled }}`

The catalog erasure release cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

ADR 0138: releases an erasure's pinning tags and branches when due. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string.

## `{{- if .Values.dapr.enabled }}`

The catalog declaration converge cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

ADR 0206 (XC-213): converges the estate's projects, warehouses and service grants onto `catalog.declared`, which the catalog mounts from the ConfigMap `<release>-catalog-declaration`. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks: the registry's conditional writes and OpenFGA's duplicate-tolerant write leave one record and one tuple, and an object two replicas made in the same tick is announced by each.

## `{{- if .Values.dapr.enabled }}`

The catalog manifest reclaim cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

ADR 0185 (LH-251): removes each root's `__manifest` versions older than `catalog.manifestReclaim.retainSeconds`, which no `dir` commit reclaims. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and two reclaims of one root remove the same versions.

## `{{- if .Values.dapr.enabled }}`

The catalog promotion expiry cron binding (from its own rationale below).

### `{{- if .Values.dapr.enabled }}`

ADR 0156 C1 (LH-466): expires a promotion hold nobody answered before its project's `review_hours` passed, leaving `published` where it was. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and each hold expires once by an ETag-guarded write.

## `{{- if and .Values.dapr.enabled .Values.services.lineage.relay.enabled }}`

The lineage relay cron binding (from its own rationale below).

### `apiVersion: dapr.io/v1alpha1`

Dapr cron INPUT binding — the sidecar POSTs to /<bindingName> on `schedule` to run lineage's relay tick:
drain the lineage outbox, prune the feed, forward the feed to the configured graph backend. Config lives
here, not in app code (no scheduler thread). Scoped to the lineage app; the route mounts only when
LINEAGE_RELAY_BINDING_NAME matches this name. Gated on dapr.enabled too: without the Dapr control
plane the Component CRD doesn't exist and the release would fail to apply.
