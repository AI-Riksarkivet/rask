# chart/templates/secrets.yaml

Rationale for `chart/templates/secrets.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if not .Values.existingSecret }}`

App secret is generated only when the operator did not supply existingSecret.

## `# The ONE S3 endpoint — the operator Tenant's <tenant>-io:9000, or minio.externalEndpoint when set.`

NO `AWS_SECRET_ACCESS_KEY` HERE, and its absence is the point. This Secret shipped the object
store's ROOT secret and NOTHING read it — verified against the live release 2026-09-16: the only
consumers of this Secret are `rask-annotator` and `rask-search`, both taking `AWS_ACCESS_KEY_ID`
as `MEDIA_S3_ACCESS_KEY_ID`; no container mounts it with `envFrom`, and no template references it
by name outside this file. A credential at rest that nothing consumes is exposure bought for
nothing, on an estate whose posture is that a secret reaches a workload from ESO, the Dapr secret
store or STS. The ID stays because it is read and because the estate's own code calls an access
key id configuration, not a secret.
