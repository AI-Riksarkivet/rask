# 0169. Each app reads only its own secrets: every sidecar logs in to the store as its own ServiceAccount (2026-10-18)

**Status:** accepted. Resolves the dev-store reach ADR 0168 names under "The dev secret store bounds that reach".
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
