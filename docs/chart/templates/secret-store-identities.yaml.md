# chart/templates/secret-store-identities.yaml

Rationale for `chart/templates/secret-store-identities.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- range $app, $reads := include "lance.storeReads" . | fromJson }}`

EVERY IDENTITY THAT READS THE SECRET STORE, AS DOCUMENTS, IN EVERY MODE ([[XC-165]], ADR 0169). One role and one policy per
app in `lance.storeReads`, and ESO's, as `role-<name>.json` and `policy-<name>.json`. The dev store's seed applies them
as they are (openbao-seed.yaml, which mounts this ConfigMap as `STORE_IDENTITIES`), a platform store's operator creates
the same roles and policies from a production render, and the render gate
(`tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`) reads the same documents to decide who can
read each secret. They are not in the seed's own ConfigMap because that renders only with `openbao.enabled`, and the
platform's store needs them most.

## `policy-{{ $role }}.json: {{ include "lance.storePolicy" (list $ (concat $reads.api $reads.components)) | quote }}`

EXACTLY THE APP'S READS: the secrets its code reads through its sidecar (its Configuration's `allowedSecrets`) and the
ones its components resolve with the sidecar's login (a bus user, the state store's DSN), each a named KV v2 data path
with `read` and nothing else: no list, no other path, no write. The policy also lets the token look itself up and renew
itself (`auth/token/lookup-self`, `auth/token/renew-self`), which daprd's background renewal needs once the role drops
the default policy. Zero trust applies to the thing fetching the secrets too.

## `role-{{ $role }}.json: {{ dict "bound_service_account_names" (list $reads.sa) "bound_service_account_namespaces" (list $.Release.Namespace) "audience" (include "lance.storeAudience" $) "token_policies" (list $role) "token_ttl" $ttl "token_max_ttl" $maxTtl "token_no_default_policy" true | toJson | quote }}`

THE APP'S OWN ACCOUNT, AND ONLY THE TOKEN ITS SIDECAR HOLDS. The role is `<release>-<app id>`, the name the one
`lance-secrets` component asks for as `<release>-{appID}`, bound to that app's ServiceAccount in the release's namespace
and to `openbao.identity.audience`: the kubelet projects a token for that audience into daprd alone
(`lance.storeIdentity`), so no other token of the account logs in. `token_no_default_policy` keeps the token to its one
policy (the store's default policy adds cubbyhole, wrapping and the token's own revocation). The token lives
`openbao.identity.tokenTtl` and at most `tokenMaxTtl`: daprd renews it in the background and logs in again with its
current projected token once it can renew no longer (components-contrib#4448, Dapr 1.19). A blank TTL fails the render,
since the store would grant its own default and maximum instead. Measured on openbao 2.2.0 through Dagger, 2026-10-18: a
role takes `audience`, `token_ttl`, `token_max_ttl` and `token_no_default_policy`, and a token from one app's role reads
that app's secret and is refused another's.

## `policy-{{ $es.role }}.json: {{ include "lance.storePolicy" (list . (include "lance.esoReads" . | fromJsonArray)) | quote }}`

ESO READS EXACTLY WHAT THE CHART'S EXTERNALSECRETS NAME (`lance.esoReads`), for the pods with no sidecar: AGE, OpenFGA, the
zones, the backups, the NATS fixture and each sidecar's app-API token. Its SecretStore logs in with ESO's own pod token
(it names no `serviceAccountRef`), so the role binds ESO's account and names no audience. ESO stays one identity for
every pod it writes for, a residual ADR 0169 names.
