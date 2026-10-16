# chart/templates/nats-provisioned.yaml

Rationale for `chart/templates/nats-provisioned.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and (not (include "lance.natsMints" .)) (not .Values.nats.auth.provisioned) }}`

Renders no object, like `signing-provisioned.yaml`: credentials nothing in this chart mints must be attested (values.yaml `nats.auth`).
