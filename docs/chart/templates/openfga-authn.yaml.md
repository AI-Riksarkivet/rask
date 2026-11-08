# chart/templates/openfga-authn.yaml

Rationale for `chart/templates/openfga-authn.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $fga := .Values.openfga -}}`

[[XC-077]]: OpenFGA's subject allow-list, and the issuer alias it must share with the tokens; rationale in values.yaml
`openfga.authn` and `auth.issuerMirror`. The mirror itself is `issuer-mirror.yaml`.

## `{{- if ne $named (include "lance.openfgaClients" .) }}`

OpenFGA reads its subject list into its env at start, and a ConfigMap edit restarts no pod, so a running server keeps
admitting a client the chart stopped naming until something replaces it. Measured on the dev estate 2026-11-08: after
helm revision 23 dropped maintenance from the list, the ConfigMap named catalog, hooks and jobs while the OpenFGA pod,
started the day before, still held maintenance's subject. The subchart renders `podAnnotations` with no tpl, so the pod
template names its clients through the literal `openfga.podAnnotations."rask.dev/openfga-clients"`, and the render is
refused unless that literal is `lance.openfgaClients`: a change to the client set then changes the pod template, which
every reconcile (Helm or Argo CD) rolls. The auth toggle changes the list without the literal; it fails closed when auth
is turned on (nothing is admitted until the pod restarts), and with auth off no service presents a token.

## `{{- if not $subjects }}{{- $subjects = "system:serviceaccount:-:no-fga-client" }}{{- end }}`

No FGA client renders (auth off): an empty list would admit every subject, so name one no ServiceAccount
can have (a namespace cannot be `-`).
