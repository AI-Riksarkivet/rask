# chart/templates/issuer-mirror.yaml

Rationale for `chart/templates/issuer-mirror.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.auth.issuerMirror.enabled (or .Values.openfga.enabled .Values.objectStore.enabled) }}`

[[XC-077]], [[XC-083]]: the cluster ServiceAccount issuer's discovery document and key set, republished without a
credential for the two verifiers that fetch them with none: the bundled OpenFGA, and the development object store, which
verifies the catalog's projected token (ADR 0170; `deploy/fixtures/rustfs` names this Service as its provider's
`configUrl`). Rationale in values.yaml `auth.issuerMirror`. Neither runs on a deployment whose OpenFGA and store are the
platform's, and then nothing renders.

## `args:`

No `command:`, so the image's tini, its ENTRYPOINT, stays the container's PID 1 and runs uvicorn's command line from
`args` ([[LH-313]]; `services.yaml.md` § `args:`).
