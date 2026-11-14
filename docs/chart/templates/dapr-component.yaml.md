# chart/templates/dapr-component.yaml

Rationale for `chart/templates/dapr-component.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled (not (.Capabilities.APIVersions.Has "dapr.io/v1alpha1")) }}`

The Dapr control plane is a prerequisite (ADR 0153), installed on its own release; rask never installs it. A render
with `dapr.enabled` on against a cluster that does not serve `dapr.io/v1alpha1` fails here, naming the fix, rather than
applying Components the API server refuses.

## `{{- $scopes := splitList "," (include "lance.sidecarScopes" .) | compact }}`

The apps whose pods run a sidecar and log in to the store: the catalog's and lineage's, and each reader of
`openbao.externalReaders`. `lance.sidecarScopes` is the store-reads table's apps less maintenance, whose worker
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
Configurations (observability.yaml) are rendered for the same apps, and `rask.daprAnnotations` decides by membership in
`lance.secretScopes` which Configuration a pod's sidecar runs under and whether it mounts the store identity. Restating
the computation would let them agree at review and diverge later.
