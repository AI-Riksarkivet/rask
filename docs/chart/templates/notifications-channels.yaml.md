# chart/templates/notifications-channels.yaml

Rationale for `chart/templates/notifications-channels.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $ch := (.Values.services.notifications).channels | default dict }}`

The notification plane's CHANNELS — Dapr OUTPUT bindings, so the
SMTP host, the Slack webhook and their credentials live here and never in the service's env or
its code. Swapping SMTP for a provider API is a Component change, not a code change.

OFF BY DEFAULT (`services.notifications.channels.*.enabled`), and that is the safe direction
rather than a deferral: a channel Component that exists with no credentials produces a sidecar
that logs a failure per send, while a channel nobody enabled produces silence. The service
tolerates both — an opted-into channel whose binding is absent is skipped quietly — so the
estate's posture is chosen in values, not implied by a template.

SECRETS COME FROM THE SECRET STORE ONLY. `secretKeyRef` in a Component's metadata is resolved by
daprd against `lance-secrets`, which `notifications` is already scoped to (it inherits the scope
through `stateStore.scopes`). There is deliberately no plaintext alternative to fall back to.

## `- { name: port, value: {{ hasKey $ch.email "port" | ternary $ch.email.port 587 | quote }} }`

hasKey+ternary, never `| default` — an explicit `port: 0` is falsy, so a default would
silently resurrect 587 on a value someone deliberately set.

## `apiVersion: dapr.io/v1alpha1`

An HTTP binding rather than Dapr's `bindings.twitter`-style provider component: a Slack
incoming webhook IS an HTTP POST, and the URL is the credential — which is exactly why it comes
from the secret store and never from a value.

That URL is the operator's one Slack channel, declared here and nowhere else ([[CTL-34]]). A
`bindings.http` takes its target from the Component alone, so every subject who opts into Slack is
posted to it; the prefs door refuses a destination of the subject's own, which would be stored and
change nothing, and a per-subject webhook would put a credential in the inbox's state.
