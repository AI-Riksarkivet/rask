# chart/templates/console.yaml

Rationale for `chart/templates/console.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.
The decision itself is recorded in `docs/adr/0193-the-inbox-is-retired-and-the-console-joins-the-product.md`.

## `{{- if .Values.console.enabled }}`

The console is the lakehouse's own UI, so the product chart ships it, as Lakekeeper ships its console in its server
(ADR 0193). It is one single-page app with no backend of its own: the image (`.docker/frontend.dockerfile`, image
`web-console`) holds the static build and its static server, `serve.ts`, which answers `/config.json` and sends the
strict CSP. The browser signs in with PKCE, keeps its tokens in memory, and calls `/api/*` same-origin with its bearer,
and every door authenticates that bearer itself.

`console.enabled: false` renders none of what follows: no ServiceAccount, ConfigMap, Service, Deployment or route, and
no edge NetworkPolicy (`network-policy.yaml`). An unmatched `/api/*` is then the Gateway's own 404, not `serve.ts`'s.

## `{{- $config := dict "signIn" false -}}`

The ConfigMap `<fullname>-console-config` holds the `config.json` the browser boots from. With `auth.enabled` it is
`{issuer, clientId, scopes}`: the issuer is `auth.oidc.issuer` (`lance.oidcIssuer`, the one the services verify tokens
against), the client is `console.oidc.clientId` (`rask-console` by default, a public client with no secret, which the
render refuses empty), and the scopes are `console.oidc.scopes` with `federated:id` and the lakehouse's audience scope
(`lance.oidcAudienceScope`: `auth.oidc.audienceScope`, or Dex's `audience:server:client_id:<auth.oidc.audience>`)
added and deduplicated. Every governed door refuses a token without either (`service_kit.governed.idp_identity`, the
audience check), so the chart adds them and no value can drop them. With `auth.enabled` off the config is
`{"signIn": false}` and the console sends no bearer.

Nothing in it is a secret. The Deployment carries `checksum/config` of it, so a change to the sign-in rolls the pod and
`serve.ts` never answers a stale file.

## `automountServiceAccountToken: false`

The ServiceAccount `rask-sa-console` exists so the pod runs as an identity of its own rather than the namespace's
`default`, and it mounts no token, on the account and on the pod: the console's server calls no door, holds no grant and
dials nothing, and the person's own bearer, in the browser, is the only credential that reaches the lakehouse. It is in
`lance.serviceAccounts`, so a client's values cannot claim the name, and not in `lance.ownAccounts`, so no converger
grants or keeps a role for it (`_helpers.tpl`).

## `securityContext:` / `lance.rootlessSecurityContext`

The pod runs as `console.runAsUser` (10001, the image's `USER`, named by number so `runAsNonRoot` can verify it) with
RuntimeDefault seccomp, and the container drops every capability, refuses privilege escalation and runs on a read-only
root filesystem. `/tmp` is an emptyDir capped at 64Mi, and the config is mounted read-only at `/etc/console`, where
`CONSOLE_CONFIG_FILE` names it. `PORT` passes `console.port` to `serve.ts`, so the port the Service, the probes and the
route name is the one the server listens on.

## `- { name: PORT, value: {{ .Values.console.port | quote }} }`

`serve.ts` listens on `PORT` (the image sets 3000), so the container is told `console.port` as well: the containerPort,
the Service and the probes name the value, and a server left on the image's port would answer none of them.

## `command: ["bun", "-e", "await Bun.sleep(...)"]`

The preStop hook holds the pod for `lifecycle.preStopSeconds` while the Gateway drops it from its endpoints, as every
fleet pod does. The image has no shell, so the hook is the image's own `bun` sleeping rather than `sleep`, which would
fail to exec and end the hook at once.

## `httpGet: { path: /, port: http }`

Readiness and liveness ask `serve.ts` for the page itself, the one thing the pod serves.

## `{{- if .Values.route.enabled }}`

The HTTPRoute `<fullname>-console` sends `PathPrefix /` to the console's Service on `route.parentRefs`, with the same
`route.hostnames` as the `/api` and `/dex` routes (`route.yaml`), so the page and every API it calls share one origin
and the browser needs no CORS. Gateway API matches the longest prefix first, so `/api/*` and `/dex` keep winning, and
an `/api/*` path no row names falls here, where `serve.ts` answers it 404 rather than the page.
