# chart/templates/nats-provisioned.yaml

Rationale for `chart/templates/nats-provisioned.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and (not (include "lance.natsMints" .)) (not .Values.nats.auth.provisioned) }}`

Renders no object, like `signing-provisioned.yaml`: what nothing in this chart mints must be attested (values.yaml
`nats.auth.provisioned`). Against the platform's store (`openbao.enabled=false`) the chart issues no bus user, so the
render fails until the operator attests two things, each named in the message: that the platform's signer admits every
session user of ConfigMap `<release>-nats-permissions` (a ServiceAccount's token, under its user's name, for a session of at
most `nats.auth.sessionSeconds` signed by a signing key scoped to its row), and that the store holds
`nats-user-<user>` (KV v2, fields `jwt` and `seed`) for every static user.
