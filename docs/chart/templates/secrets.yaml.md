# chart/templates/secrets.yaml

Rationale for `chart/templates/secrets.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if not .Values.existingSecret }}`

App secret is generated only when the operator did not supply existingSecret.

## `HCP_ENDPOINT: {{ include "lance.s3Endpoint" . | quote }}`

THE S3 ENDPOINT AND NO STORE CREDENTIAL, neither half. Every workload that touches the store presents its own scoped
identity: a Dapr app reads its secret from the secret store, a pod without a sidecar mounts a Secret ESO writes, and a
Lance client takes a session the catalog vends. The access key id of each identity is config on the pod that uses
it, so nothing reads one from this Secret. The store's root is named only by
the scoped-users hook and the dev store's seed (ADR 0168): on RustFS the root secret signs every STS session token, so a root access key id
here would be the first half of the one credential that can forge any vended session, mounted into every pod with
`ambientStorage` (fleet.yaml). `tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py` holds the root's
identity to those two.
