# chart/templates/openfga-authn.yaml

Rationale for `chart/templates/openfga-authn.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $fga := .Values.openfga -}}`

[[XC-077]]: OpenFGA's subject allow-list, and the issuer alias it must share with the tokens; rationale in values.yaml
`openfga.authn` and `auth.issuerMirror`. The mirror itself is `issuer-mirror.yaml`.

## `{{- if not $subjects }}{{- $subjects = "system:serviceaccount:-:no-fga-client" }}{{- end }}`

No FGA client renders (auth off): an empty list would admit every subject, so name one no ServiceAccount
can have (a namespace cannot be `-`).
