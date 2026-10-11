# deploy/fixtures/observability/chart/templates/retention-job.yaml

Rationale for the observability fixture's `templates/retention-job.yaml`, kept beside the fixture rather than in its
templates. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

Durable retention: a post-install/post-upgrade Helm hook that sets a database-level TTL on
GreptimeDB's `public` database (observability.dbName). Every table inherits the DB TTL
unless it sets its own — the OTel Collector's filelog `opentelemetry_logs`,
`opentelemetry_traces`, and the metrics physical table — so all
three signals age out at observability.retention instead of growing forever on the single
local disk. Idempotent: ALTER DATABASE just re-sets the value on every upgrade. It runs as
a HOOK (not a plain Job) so it re-applies after the chart has (re)created GreptimeDB, and
the retry loop tolerates GreptimeDB still starting up.

The pod template carries an explicit component label: under the prod default-deny
NetworkPolicy an unlabelled hook pod is invisible to every allow rule, and the failure is
silent (kindnet does not enforce, so it only appears on a policy-enforcing CNI).
