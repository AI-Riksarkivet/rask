# chart/templates/dapr-dashboard.yaml

Rationale for `chart/templates/dapr-dashboard.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `serviceAccountName: {{ include "lance.fullname" . }}-dapr-dashboard`

Its own RBAC-bound SA, not a tokenless one from security-sa.yaml: the dashboard reads the
API (the ClusterRole above), and a zero-grant SA would leave it Ready but broken.

## `{{- include "lance.rootlessSecurityContext" 65532 | nindent 10 }}`

`daprio/dashboard` declares no USER, so it runs as root unless a uid is pinned here —
read off the image config with `dagger core container from --address=... user`, which
answers empty. 65532 is the uid Dapr's own chart gives this image. It writes nothing:
it reads the k8s API through the SA above and serves 8080.
