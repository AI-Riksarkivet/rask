# chart/templates/signing-provisioned.yaml

Rationale for `chart/templates/signing-provisioned.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and (include "lance.secretsViaDapr" .) (or (not .Values.openbao.devMode) .Values.openbao.externalAdd...`

Renders no object, like `auth-consistency.yaml`: a store nothing seeds must be told the signing keys exist (values.yaml `signing:`).
