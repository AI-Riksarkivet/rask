# chart/templates/backup-snapshot.yaml

Rationale for `chart/templates/backup-snapshot.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `serviceAccountName: {{ include "lance.fullname" . }}-snapshot`

Its own RBAC-bound SA, not the tokenless shared jobs SA: its kubectl creates the
VolumeSnapshot CR through the Role above, so a zero-grant SA fails every backup.

## `{{- include "lance.podSecurityContext" . | nindent 10 }}`

The pod runs nothing as root ([[XC-202]]): kubectl reads the pod's projected token and writes nothing but its discovery
cache, so the pod refuses a root container and runs under the runtime's default seccomp profile, like every first-party
pod (`lance.podSecurityContext`).

## `{{- include "lance.rootlessSecurityContext" 65532 | nindent 14 }}`

The kubectl image's container runs as uid 65532 with a read-only root: its one write is the discovery cache under
`$HOME`, which the `HOME` env points at the pod's `/tmp` volume.
