# chart/templates/external-secrets.yaml

Rationale for `chart/templates/external-secrets.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if not (.Capabilities.APIVersions.Has "external-secrets.io/v1/ExternalSecret") }}`

THE EXTERNAL SECRETS OPERATOR IS A PREREQUISITE ([[XC-004]]; owner, 2026-10-05: platform-run, rask never
installs it). Every Secret a pod without a sidecar mounts is written here from the store, and no credential
is a chart value, so with no operator nothing would deliver one: the render refuses rather than installing
a release whose pods wait on Secrets nobody creates. `.Capabilities` is the cluster's own answer on an
install or upgrade; a cluster-less render states the target with `--api-versions`.

## `{{- $stores := or (lookup "apps/v1" "StatefulSet" .Release.Namespace (include "lance.ageHost" .)) (lookup "...`

AN ESTATE THAT ALREADY HOLDS DATA KEEPS ITS CREDENTIALS. The AGE database and the object store keep the
password and root they were initialised with, so generating fresh ones beside them hands every consumer a
credential its server refuses. On an upgrade of an estate whose stores exist, the credential Secret must
already exist: created from the current credentials before the first upgrade, and rotated in place
afterwards (docs/OPERATORS.md 8). `lookup` answers only against a cluster, so a cluster-less render and a
fresh install, where no store exists yet, generate.

## `{{- if or (not $held) (dig "metadata" "ownerReferences" list $held) }}`

THE DEV OPENBAO'S CREDENTIALS, GENERATED IN THE CLUSTER. The render carries none: ESO's Password generator
fills `<release>-dev-credentials` once (`CreatedOnce`) and never again, and the dev OpenBao's seed mounts it
and writes each key into the in-memory store. It is a Secret rather than store-only because the store
forgets: the AGE database and the object store keep what they were initialised with across an OpenBao
replacement, so the next seed must write the same values. `keep` holds it through an uninstall for the same
reason the data volumes are held. Rendered only for the chart's own dev store (`lance.mintsDevCredentials`);
a real deployment is refused before it gets here (prod-credentials.yaml), and a platform store holds
credentials its operator provisioned. Rotation: docs/OPERATORS.md.
A Secret that exists with no owner was carried forward by the operator from the credentials the stores already
hold, so nothing here generates beside it; one ESO created (owned by this ExternalSecret) keeps it rendered,
since removing the owner would garbage-collect the Secret.

## `apiVersion: external-secrets.io/v1`

The in-chart Collector's credential for the audit store, mounted as files (otel-collector.yaml).

## `{{- if .Values.ray.auth.enabled }}`

Gated on ray.auth.enabled ALONE, matching `rask.rayAuthEnv`, which emits the consuming
secretKeyRef on that value by itself. Not on `ray.enabled`/`singleTenant.enabled`: configmap.yaml
states that "rask's Ray is managed outside this repo, so an external address is the NORMAL case",
and the fleet must present a bearer token to an external cluster too (measured 2026-08-16: a
narrower gate left rask-compute in CreateContainerConfigError and the release in pending-upgrade).

## `apiVersion: external-secrets.io/v1`

The control-root backup's S3 credential, written by ESO for a pod that MOUNTS it as a file.

A CronJob pod carries no Dapr sidecar, so the secret-store path is unavailable to it and ESO is the
one the estate admits. It is consumed as a FILE rather than a `secretKeyRef` env row deliberately:
`--credentials-file` is the only way `control_root_backup.py` accepts a credential without falling
back to the environment chain, which is the delivery path the estate forbids.

## `{{- $natsFiles := include "lance.natsFiles" . | fromJson }}`

The NATS credentials of the pods with no sidecar, as files (values.yaml `nats.auth`): the stream Job's admin and nats-box's monitor
creds, and the server's operator-mode and route includes.

## `apiVersion: external-secrets.io/v1`

THE DAPR APP-API TOKEN, for the injector. A pod annotated `dapr.io/app-token-secret: <this>` has daprd
stamp it as `dapr-api-token` on every delivery, and the app compares it with the store's `dapr-app-token`
(`RASK_APP_TOKEN_FROM_STORE`) or with `APP_API_TOKEN` (`lance.appTokenEnv`). Both read the one store field,
so the two sides agree by construction. Gated on either consumer: `dapr.sidecars` (the annotation) or
`auth.enabled` (the env row), so neither reference can dangle. A rotation reaches a sidecar only when its
pod restarts, since the injector reads the Secret at pod creation (docs/OPERATORS.md).

The zones' session-cookie sealing key (AES-256-GCM) and the confidential client secret, shared by
every zone (they all read the origin-wide cookie; home also exchanges the code). Rotating the
sealing key signs everyone out, and reaches a zone only when it restarts.

A third party's credential, provisioned in the store by the operator (values.yaml `secrets`).
