# chart/templates/secret-store-identities.yaml

Rationale for `chart/templates/secret-store-identities.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- range $app, $reads := include "lance.storeReads" . | fromJson }}`

EVERY IDENTITY THAT READS THE SECRET STORE, AS DOCUMENTS, IN EVERY MODE ([[XC-165]], ADR 0169). One role and one policy per
app in `lance.storeReads`, and ESO's, as `role-<name>.json` and `policy-<name>.json`. The dev store's seed applies them
as they are (openbao-seed.yaml, which mounts this ConfigMap as `STORE_IDENTITIES`), a platform store's operator creates
the same roles and policies from a production render, and the render gate
(`tests/chart/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`) reads the same documents to decide who can
read each secret. They are not in the seed's own ConfigMap because that renders only with `openbao.enabled`, and the
platform's store needs them most.

## `policy-{{ $role }}.json: {{ include "lance.storePolicy" (list $ $reads.api (hasKey ($.Values.openbao.externalReaders | default d...`

EXACTLY THE APP'S READS: the secrets its code reads under its own login (`lakehouse.governed.secrets`), each a named
KV v2 data path with `read` and nothing else: no list, no other path, no write. The policy also lets the token look itself up
(`auth/token/lookup-self`), which a service's question about its own token needs once the role drops the default
policy. A lakehouse service's policy grants no renewal, since a service logs in again and never renews; an external
reader's does (`auth/token/renew-self`), for a client whose own agent keeps one token alive. Zero trust applies to the thing fetching the secrets too.

## `role-{{ $role }}.json: {{ dict "bound_service_account_names" (list $reads.sa) "bound_service_account_namespaces" (list $.Release.Namespace) "audience" (include "lance.storeAudience" $) "token_policies" (list $role) "token_ttl" $ttl "token_max_ttl" $maxTtl "token_no_default_policy" true | toJson | quote }}`

THE APP'S OWN ACCOUNT, AND ONLY THE TOKEN PROJECTED FOR THE STORE. The role is `<release>-<app id>`, the name the app's
environment gives it (`lance.secretStoreEnv`), bound
to that app's ServiceAccount in the release's namespace and to `openbao.identity.audience`: the kubelet projects a token
for that audience into the app's container (`lance.storeIdentity`), so no other token of the account logs
in. `token_no_default_policy` keeps the token to its one
policy (the store's default policy adds cubbyhole, wrapping and the token's own revocation). The token lives
`openbao.identity.tokenTtl` and at most `tokenMaxTtl`: a service logs in again once half its own token's lease is
used, on a loop it keeps, uses the held one for nine tenths of the lease while a login is refused, and never renews.
A blank TTL fails the render,
since the store would grant its own default and maximum instead, and so does one outside the minute to the hour a
service takes a lease in, or in a form the chart cannot read (`lance.storeTokenSeconds`), and a `tokenMaxTtl` under the
`tokenTtl`, which would have the store issue the shorter of the two. Measured on openbao 2.2.0 through Dagger, 2026-10-18: a
role takes `audience`, `token_ttl`, `token_max_ttl` and `token_no_default_policy`, and a token from one app's role reads
that app's secret and is refused another's.

## `policy-{{ $es.role }}.json: {{ include "lance.storePolicy" (list . (include "lance.esoReads" . | fromJsonArray)) | quote }}`

ESO READS EXACTLY WHAT THE CHART'S EXTERNALSECRETS NAME (`lance.esoReads`), for the pods that do not log in to the store
themselves: OpenFGA and the development Postgres (the `postgres` secret, while the chart runs OpenFGA), the
object store's root and the NATS fixture. Its SecretStore logs in with ESO's own pod token
(it names no `serviceAccountRef`), so the role binds ESO's account and names no audience. ESO stays one identity for
every pod it writes for, a residual ADR 0169 names.
