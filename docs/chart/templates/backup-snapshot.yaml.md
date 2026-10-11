# chart/templates/backup-snapshot.yaml

Rationale for `chart/templates/backup-snapshot.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `serviceAccountName: {{ include "lance.fullname" . }}-snapshot`

Its own RBAC-bound SA, not the tokenless shared jobs SA: its kubectl creates the
VolumeSnapshot CR through the Role above, so a zero-grant SA fails every backup.
