# chart/templates/object-store-identities.yaml

Rationale for `chart/templates/object-store-identities.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `kind: ConfigMap`

EVERY STORE IDENTITY'S CANNED POLICY, AS GIT DECLARES IT ([[XC-083]], [[XC-084]], ADR 0170, ADR 0197): one ConfigMap,
`<release>-object-store-identities`, rendered in every mode, whether or not the release runs the development store. No
lakehouse workload holds a store key; each projects a ServiceAccount token for `objectStore.identity.audience` and
exchanges it at STS `AssumeRoleWithWebIdentity`, and the store grants the session the policy named after the token's
subject. This is the one place those policies are written down, so a platform's S3 operator creates them from this
object, and the development store's provisioning hook (`object-store-provisioning.yaml`) writes the same documents.

The hook does not mount this ConfigMap: as a `pre-upgrade` hook it runs before the release manifest applies, so a
mounted copy would be the previous revision's, or absent on the upgrade that adds it. It renders the same helper and
passes each policy inline (`--policy NAME=JSON` to `scripts/rustfs_admin.py`), so the two cannot disagree.

## `{{- range $component := include "lance.objectStoreIdentities" . | fromJsonArray }}`

One key per workload that reaches the store (`lance.objectStoreIdentities`): the catalog and lineage always, maintenance
with `maintenance.enabled`, and each backup CronJob while it is on. A workload not on the list has no policy, so its
token's exchange is refused and its boot fails closed.

## `{{ include "lance.saName" (list $ $component) }}.json: |`

ONE KEY PER IDENTITY, `<release>-sa-<component>.json`: the ServiceAccount the workload runs as (`lance.saName`). The
store names the policy by the account the token's `sub` carries, `system:serviceaccount:<namespace>:<release>-sa-<component>`
(`lance.saAccount`), which the provisioning hook writes it under; a ConfigMap key cannot hold that name's `:`, so the key
is the ServiceAccount's own name. The value is `lance.objectStorePolicy` for that component; `_helpers.tpl.md` states
each policy's reach.
