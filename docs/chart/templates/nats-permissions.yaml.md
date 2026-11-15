# chart/templates/nats-permissions.yaml

Rationale for `chart/templates/nats-permissions.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `name: {{ include "lance.fullname" . }}-nats-permissions`

THE SIGNER'S TABLE. ConfigMap `<release>-nats-permissions`, key `permissions.json`, rendered in every mode (the dev
store's and the platform's), holds what the bus's signer needs to answer a connection with a session
(`lance.natsPermissions`):

- `audience`: `nats.auth.audience`, the audience the signer's TokenReview names for the token a pod presents.
- `sessionSeconds`: `nats.auth.sessionSeconds`, the longest a session lives.
- `users`: each SESSION user (a row of `nats.auth.users` with a ServiceAccount; a static user is not listed), keyed by its
  bus user name, with `serviceAccount` (`system:serviceaccount:<namespace>:<name>`) and the row's `publish` and `subscribe`
  lists, never empty.

The signer admits a token only under the name of a user whose `serviceAccount` is the token's, and signs the session with
the signing key scoped to that user's row, so the bus enforces the row, not the signer's code. In development the NATS
fixture's stand-in signer reads this ConfigMap and re-reads it when it changes (`deploy/fixtures/nats/callout`); on a
real deployment the platform's signer reads it, and `nats.auth.provisioned` attests that it does.
