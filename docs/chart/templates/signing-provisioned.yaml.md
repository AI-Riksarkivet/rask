# chart/templates/signing-provisioned.yaml

Rationale for `chart/templates/signing-provisioned.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and (not .Values.openbao.enabled) (not (.Values.signing).provisioned) }}`

Renders no object, like `auth-consistency.yaml`: the platform's store, which nothing here seeds, must be told the signing keys exist (values.yaml `signing:`).
