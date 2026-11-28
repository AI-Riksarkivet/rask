# chart/templates/catalog-migrate.yaml

Rationale for `chart/templates/catalog-migrate.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `name: {{ include "lance.fullname" . }}-catalog-migrate-{{ include "rask.bootstrapRev" . }}`

THE RELEASE MIGRATES THE CATALOG'S DATABASE SCHEMA (ADR 0230 §11). A bootstrap Job, not a Helm hook ("BOOTSTRAP JOBS"
in `_helpers.tpl.md`): the catalog's pods wait for the schema, so a post-upgrade hook would deadlock `--wait
--wait-for-jobs`, and a pre-upgrade one would run before the release has seeded the migrator's password. Applied in the
same wave as the Deployment, `--wait-for-jobs` waits for it beside the pods, each new catalog pod's init step waits for
the revision it applies (`services.yaml`, `python -m catalog.schema wait`), and the release succeeds once both are done.
Named by the release revision, so every upgrade runs it again (a plain Job's spec is immutable) and a run that finds the
schema at the image's head applies nothing. Two runs at once (an Argo CD sync beside a Helm upgrade) apply each revision
once: `catalog.core.schema.migrate` applies every pending revision in one transaction behind a transaction-scoped
advisory lock.

## `{{- include "lance.reconciledJob" "Sync" | nindent 4 }}`

Under Argo CD the Job is a `Sync` hook (`lance.reconciledJob`): every sync re-runs it, a reaped Job is no drift, and the
migration runs in the same phase as the Deployment it orders through the init step. No sync wave holds it after the
ExternalSecret it mounts: that Secret comes from a store the same release seeds, and the command's own wait for its
role (`RASK_DATABASE_CONNECT_WAIT_SECONDS`) orders it, as under Helm.

## `backoffLimit: 20`

A failed run is retried, and `lance.bootstrapJobDeadline` bounds them all: the migrator waits up to its connect wait
(600 s by default) for a server that does not yet admit its role (in development the postgres fixture's provisioning Job
creates it once the release has seeded its password), so the deadline, not the retry count, ends a run that will never
succeed.

## `serviceAccountName: {{ include "lance.fullname" . }}-sa-jobs`

The one-shot Jobs' tokenless account (`security-sa.yaml`): the migration calls no Kubernetes API and holds no projected
token. Its only credential is the migrator role's, below.

## `{{- include "lance.databaseCredential" (list . "volumes" "migrator") | nindent 8 }}`

THE MIGRATOR'S CREDENTIAL IS A FILE (ADR 0230 §8): the Secret `<release>-database-migrator`, which ESO writes from the
store's `postgres.catalog.secrets.migrator` (`external-secrets.yaml`), mounted at `lance.databaseCredentialDir` with the
files `username` and `password`, which the command reads at each connection it opens. Nothing of it is env or an
argument. The migrator owns the database and every object of schema `rask`, so only this Job holds it; the catalog's
pods hold the catalog role's.

## `- { name: RASK_DATABASE_CATALOG_ROLE, value: {{ $c.roles.catalog | quote }} }`

Each revision grants the catalog and maintenance roles exactly what their code reads and writes, by the names the
platform gave them (`postgres.catalog.roles`), so the grants are reviewed in git with the table they are on.

## `args: ["python", "-m", "catalog.schema", "migrate"]`

The catalog's own image runs the migration, so the revisions applied are the ones the image's code was built for. `args`,
never `command`: the image's ENTRYPOINT is its tini. It exits 0 naming the revision the database holds and 1 with the
reason it refused on one line, among them a database already at a revision this image does not know, which a newer
release migrated, and a statement the database refuses (a grant to a role it does not know), in the database's own words.
