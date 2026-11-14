# 0169. Each app reads only its own secrets: every sidecar logs in to the store as its own ServiceAccount (2026-10-18)

**Status:** accepted; amended 2026-11-14 (XC-199 step D2, below: each service logs in to the store itself). Resolves the
dev-store reach ADR 0168 names under "The dev secret store bounds that reach".
Source: owner, 2026-10-18, on XC-165 (#659): "Its own row, next", then "Widen XC-165" (the per-app secret-store
identities and the dev store's root token are this row's), then three rulings through the multi-question tool: Dapr
1.19's Kubernetes auth as the identity mechanism, a one-time carry for the live store, and one shared app-API token.

## Context

Every app scoped to `lance-secrets` could read every field of `secret/lance`: the catalog's data-plane key, lineage's and
maintenance's keys, the Postgres password, the IdP client secret, the zones' session key and the Ray token. Two things
made that so.

- **The Dapr scope cannot hold it alone.** Each app's Configuration was `defaultAccess: allow` with a deny list, and
  Dapr hands a sidecar a whole secret. Dapr checks a Configuration's scope only on the app-facing secrets API
  (`pkg/api/universal/secrets.go:35`); a component resolving its own `secretKeyRef` goes straight to the store with the
  sidecar's credential (`pkg/runtime/processor/secret/secret.go:162-170`), and the scope binds daprd, not the app
  container beside it. Both read the same in 1.18.1 and 1.19.0-rc.4.
- **The store could not tell apps apart.** Every sidecar presented one token: the dev OpenBao's published root token
  (`server -dev -dev-root-token-id=root`, which the probe header and the seed's env also carried), and in production
  one static platform token in `<release>-openbao-token`. Dapr 1.18's Vault store reads its token once, at init, and
  never renews it (components-contrib v1.18.0 `vault.go:400-421`), so a per-app token could be scoped but not
  short-lived. Kubernetes auth, which logs in as the pod's ServiceAccount and renews in the background, merged
  upstream on 2026-10-07 (components-contrib#4448) and ships in Dapr 1.19.0-rc.4, in no 1.18.x.

## Decision

1. **One secret per reader set.** The bundle is gone. Each app's S3 pair is `<plane>-s3` (`catalog-s3`, `lineage-s3`,
   `maintenance-s3`, beside `viewer-s3` and `media-s3`), fields `access_key` and `secret_key`; the shared values are
   each their own secret, read by exactly their readers (`dapr-app-token`, `dapr-state`, `postgres`,
   `frontend-session`, `oidc-client`, `publisher-oidc`, `ray-auth-token`). Two readers of one value read one secret,
   never a copy, so a rotation writes one place. docs/architecture/deployment.md lists them with their readers.
2. **Deny by default at the Dapr scope.** Each app's Configuration is `defaultAccess: deny` with `allowedSecrets` naming
   exactly what its code reads; a secret only its components resolve (its bus user, the state store's DSN) is in no
   app's list.
3. **The store enforces the same split, per sidecar.** One `lance-secrets` component logs every sidecar in with
   `vaultAuthMethod: kubernetes` as the role `<release>-{appID}`: Dapr fills `{appID}` into any component's metadata at
   sidecar start (`pkg/runtime/meta/meta.go:96-97`, documented as templated metadata values), so each sidecar logs in
   to its own app's role. Each role is bound to that app's ServiceAccount and to the audience `rask-openbao`, and holds
   one policy reading exactly what the app and its components read, plus its own token's lookup and renewal; its token
   lives 15 minutes, at most an hour, and daprd renews it or logs in again. The pod projects the ServiceAccount token
   for that audience and `dapr.io/volume-mounts` mounts it into daprd alone. No store token exists to deliver, mount
   or rotate.
4. **One table decides every read.** `lance.storeReads` (chart/templates/_helpers.tpl) lists, per app, its account,
   its code's reads and its components' reads; the Configurations, the component's scopes, the token volume, the roles
   and the policies are views of it. ESO's own role reads exactly the secrets the chart's ExternalSecrets name
   (`lance.esoReads`). The roles and policies render in every mode, as JSON documents in
   `<release>-secret-store-identities`, which the dev seed applies and a platform store's operator creates.
5. **No published token reads the dev store.** The dev server is no longer `server -dev`, which prints its root token
   and unseal key to its log (openbao 2.2.0 `command/server.go:2720,2732`): it starts uninitialized, in memory, from a
   config file. The seed initializes it, unseals it with the key on stdin, mints the store's administrator under an id
   the fixture generates in-cluster (`rask-openbao-root`, ESO's Password generator, mounted into the seed alone) and
   revokes the init token; the unseal key stays on the seed's memory volume. Neither probe carries a token.
6. **Dapr 1.19 is the floor.** The Dapr fixture pins 1.19.0-rc.4 until 1.19's GA release, and a production platform
   must run Dapr 1.19 or later.

## Considered options

- **Static per-app tokens on Dapr 1.18.1** (ESO's VaultDynamicSecret minting one orphan token per app into a Secret the
  Dapr operator resolves into that app's component). Rejected by the owner: daprd never renews it, so it must outlive
  its pod (a 768 h TTL, re-minted daily), which is scoped but not short-lived, held because a Secret was delivered, and
  a production operator would mint and rotate one per app.
- **Static tokens now, Kubernetes auth at Dapr 1.19 GA.** Rejected by the owner: two passes over one seam, building
  token minting and rotation only to delete them.
- **One component per app.** It enforces the same identity, at the cost of a store name per app in every service, a
  state store per app (a component names one secret store) and a second name for every component that resolves a
  secret. The `{appID}` role gives each sidecar its own login through one component.
- **Keeping the deny lists.** A deny list lets an app read every secret added after it was written, and Dapr's scope
  is not enforcement at the store; the reason recorded for it (a reader whose secret name is only known at runtime)
  no longer holds: the viewer's store registry is rendered configuration (`RASK_STORES`), and no door consumes a
  warehouse's `credential_ref` ([[LH-205]]).

## Consequences

- **A sidecar that cannot log in does not start.** Dapr 1.19's store logs in at component init, so a missing role, a
  sealed store or a store that is down fails the sidecar's start closed; a running sidecar keeps renewing, and logs
  in again when the store comes back.
- **The live estate's store is replaced once.** The new store's administrator token is not the old store's `root`, so
  the surge cannot carry the signing pairs and the bus trust root without a one-time step: before the rollout, the new
  administrator id is created in the outgoing store with the old token (the owner's ruling). A fresh install from git
  needs no such step.
- **XC-079 (#201) is answered.** Its gap, every daprd reading the store as root in dev and with one shared static
  token in production, is closed by per-app logins with no token at all; its closes-when names a mounted token file,
  which this decision makes unnecessary.
- **Residuals, named.** The app-API token stays one estate-wide secret read by the five apps with a Dapr door, so an app
  holding it can pose as another's sidecar on that app's Dapr-delivered routes (the owner's ruling; parked as a row).
  ESO is one identity for every pod without a sidecar (AGE, OpenFGA, the zones, the backups). The dev store speaks
  plain http inside the cluster; production refuses anything but https. Inside a pod, the app reaches its own
  sidecar's API unauthenticated: the pod is Dapr's unit of trust.

## Amendment (2026-11-14): each service logs in to the store itself

Source: #787 (XC-199, the lakehouse runs without Dapr), step D2, which the owner started with `/implement #787`. The one
ADR that supersedes the Dapr records is that row's last step; until it lands, this is what decisions 2 to 4 say.

A service reads its own secrets under its own login. It presents the token its pod projects for `rask-openbao` at the
store's Kubernetes auth method, as the role `<release>-<app id>` decision 3 already binds to its ServiceAccount, and
holds the store token that returns in memory (`lakehouse.governed.secrets`). The role, its binding, its audience and its
policy stand as decided and admit that login unchanged.

- **Decision 2.** Each app's Configuration is `defaultAccess: deny` with no `allowedSecrets`: no secret is served through
  a sidecar's secrets API. A sidecar's own login still resolves its components' `secretKeyRef`s (a bus user), which a
  Configuration's scope never bound.
- **Decision 3.** The projected token is mounted into the app's container as well as into daprd. A service logs in
  before it serves and fails its start closed when the store never admits it, logs in again once half its token's lease
  is used, and keeps the held token for nine tenths of the lease while a login is refused; it never renews one. While
  the loop is passing, only the start and the loop log in: a read that finds the login used up is not served until a
  pass has made the next one, so a store that is failing, or the API server behind its token review, is asked once a
  pass and never once a read. A read logs in itself only where no loop has made a pass for a minute (a tool, or a
  process whose loop was stopped or has stalled). It
  takes a token whose lease is a minute to the hour this decision bounds a store token at. A role written with no
  `token_ttl` issues one at the store's default of weeks, and under a minute the loop that keeps the login, which looks
  every 15 s and logs in again at half the lease, is sure of no pass to do it in before the token is used up: the
  service's start fails closed on either. The render refuses an `openbao.identity.tokenTtl` or `tokenMaxTtl` outside
  that range, a `tokenMaxTtl` under the `tokenTtl`, and a number written with a leading zero or more than five digits
  (Helm reads the first as octal and wraps past 64 bits on the second, where the store reads both as written), so git
  cannot declare a role its pods refuse (owner, 2026-11-14, for the hour; the minute,
  the order of the two and the one reading of a number follow from the loop and the render). daprd goes on renewing its
  own.
- **A refused secret is the store's answer, and the store says whether a token is dead** (owner, 2026-11-14). The store
  answers 403 both to a token it dropped and to a secret the policy does not name, so a reader never logs in on a
  refusal. The token asks the store about itself (`auth/token/lookup-self`, which each rendered policy already
  grants; the roles carry no `default` policy). A service's start asks it of the login it has just made; a login the
  store refuses the question is dropped and made again at the start's next attempt, and the start fails closed when
  none is answered within its budget, so no service runs under a policy without the grant. Its loop asks it every 15 s
  and logs in again on the pass that finds the token refused. When the store refuses the login that pass made as well
  (the grant was withdrawn, or a replacement store answers beside the one it replaces and the login landed on the one
  that is leaving), the next pass logs in again at once, and each further login the store refuses in turn waits twice
  the passes the last did, fifteen at most, until the store answers one; a login the lease asks for meanwhile counts as
  one of that row. The readiness report asks the same question at every probe and, while the loop is passing, logs in
  for none. Three review rounds had each found a hole in a reader-side guess at what a 403 meant, which is why the
  guess is gone; a fourth found that one refusal of a fresh token had switched the question off for the life of the
  process, a fifth that a start went on asking about a login a replaced store had already dropped, and a sixth that a
  login the lease asked for was followed by another a pass later. So a refusal is answered with a new login unless the
  store has just refused the last one, a login the loop makes while such a row is open counts in it, and nothing
  remembers a refusal past the store's next answer.
- **Decision 4.** `lance.storeReads` no longer hangs on `dapr.enabled`: every role, policy and network path renders
  with or without a sidecar, and only the app token's read, the components' bus users and maintenance's work-queue user
  follow it. The one secret name an overlay supplies to the table (`services.lineage.forward.authSecret.name`) is held to
  the secret-name pattern, since it is a path of lineage's policy.
- **A platform store under a private CA** is named once (`openbao.ca.configMap`) and handed to all three readers: the
  services, since Python checks a certificate against the bundle it ships with, the sidecar's component and ESO.

Measured on the dev OpenBao as the catalog's own account, 2026-11-13: a login answers a token and a 900 s lease; a read
answers the fields at `data.data`; a secret the policy does not name answers 403 whether or not it exists; a role the
store does not hold answers 400; a wrong account 403.
