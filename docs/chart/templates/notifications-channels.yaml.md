# chart/templates/notifications-channels.yaml

Rationale for `chart/templates/notifications-channels.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $ch := (.Values.services.notifications).channels | default dict }}`

The notification plane's CHANNEL — a Dapr OUTPUT binding, so the SMTP host and its credentials
live here and never in the service's env or its code. Swapping SMTP for a provider API is a
Component change, not a code change.

Email is the only channel because a push carries a row the visibility gate checked for one person,
so it goes to an address that person supplied. A shared channel such as a Slack webhook would hand
every reader of it a row nobody checked them against ([[CTL-34]]); estate events for operators are
the event stream's to carry.

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
