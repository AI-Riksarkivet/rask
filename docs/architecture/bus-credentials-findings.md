# Bus credentials: how a lakehouse pod proves itself to NATS, read and measured

> Research note for the bus-credentials step of #787 (D7), read with [ADR 0199](../adr/0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md)
> and [ADR 0198](../adr/0198-maintenance-is-driven-by-commits-and-a-clone-keeps-its-source-by-a-tag.md). Primary sources only,
> fetched 2026-11-10 into the session's scratch folder (`…/scratchpad/bus-creds/src`): nats-server `v2.15.2`
> (`5a815f11a`), nats.py `v2.16.0` (`3547a63b2`, byte-identical to the `nats/aio/client.py` in this repository's `.venv`),
> nats-io/jwt `v2.8.2` (`82017236d`, the version nats-server 2.15.2's `go.mod:12` pins), nats-architecture-and-design
> `main` (`fac1b7e7f`), nats.docs `main` (`f115becf6`), NACK `main` (`4f08e6d39`), the NATS Helm chart `nats-2.15.2.tgz`
> vendored in `deploy/fixtures/nats/chart/charts`, OpenBao `v2.2.0` (`helper/builtinplugins/registry.go`), the Kubernetes
> documentation pages named below, and NIST SP 800-207. Paths under nats-server are relative to its `server/`; under nats.py
> to `nats/src/nats/`. Measurements are in § 6, each with its script.

**The answer.** Yes: the pod proving itself with its own projected ServiceAccount token over TLS, and receiving a bus user
scoped to its service's subjects that expires in minutes, is the best-practice shape, and it works with nats-py 2.16.0 on
nats-server 2.15.2 in operator mode (measured, § 6). The mechanism that implements it is **NATS auth callout in operator mode,
with the server's `default_sentinel` set and one scoped account signing key per service**: the pod sends only its token
(`token=` a callable that re-reads the projected file), the platform's callout service verifies it with a TokenReview against
the bus audience, and answers with a user that the service's scoped signing key signs and that expires in minutes; the
scope on that key, not the callout's code, is the permission the server enforces. Measured: when a user expires, nats-py
closes for good (it never reconnects), so the client must refresh (`force_reconnect()` before expiry) and rebuild on
close; with both, a JetStream pull worker ran across 3 to 6 expiries per run with no restart, no lost acknowledged message
and no duplicate. **The interim**, until the platform's callout runs, is what #787 decided: each pod reads its static bus
credential from the store into memory under its own store login. That credential is mechanism (a) below and fails "short-lived"
until D7 lands.

## 1. The standard each option is judged by

- **Zero trust** (NIST SP 800-207 § 2.1): "All communication is secured regardless of network location. Network location
  alone does not imply trust." (tenet 2); "Access to individual enterprise resources is granted on a per-session basis. …
  Access should also be granted with the least privileges needed to complete the task." (tenet 3); "All resource
  authentication and authorization are dynamic and strictly enforced before access is allowed." (tenet 6). The owner's
  form: no identity trusted by position, no secret through env, every credential scoped and short-lived, the server refuses
  what was not granted.
- **GitOps**: everything rask needs is declared in git and converges on a reconcile.
- **Cloud native**: the bus, its TLS and the issuing of users are the platform's (#787's decision, "Nats will be run secured
  and hardened by the platform team"); rask's pods are declarative workloads that name them.
- **Lance-idiomatic**: not relevant here. No option touches a Lance table or a namespace door; the bus carries the
  lakehouse's events and work queues, not data.

## 2. What the sources say

**Expiry on a live connection.** The server arms a timer at the user JWT's `exp` (`client.go:1376-1393`, `setExpiration`;
`client.go:6113-6114`, `time.AfterFunc(d, c.authExpired)`), for an ordinary JWT (`auth.go:1181`) and for a callout user
(`auth_callout.go:341`). At expiry it sends `-ERR 'User Authentication Expired'` and closes with `AuthenticationExpired`
(`client.go:2565-2568`). The expiry is measured on the server's clock (`client.go:1384`, `time.Now().Unix()`).

**What nats-py does with it.** The parser lowercases every `-ERR` (`protocol/parser.py:137`). `_process_err`
(`aio/client.py:1587-1615`) reconnects only for `stale connection`, reports `permissions violation` and keeps the
connection, and for anything else, `user authentication expired` included, schedules `_close(Client.CLOSED)`: closed for
good, with `closed_cb` called. `user_jwt_cb`, `signature_cb` and a callable `token` are called only while building
`CONNECT` (`aio/client.py:1814-1848`), so fresh callbacks matter only to a new connection. On a refused initial connect with
`max_reconnect_attempts=-1` the client retries for ever (`aio/client.py:616-640`). `force_reconnect()`
(`aio/client.py:1617-1629`) drops the transport and reconnects, re-running the callbacks and re-subscribing. A token is sent
beside a JWT "for auth callouts" (`aio/client.py:1825-1830`); with no `signature_cb` the token is sent alone
(`aio/client.py:1842-1846`). TLS takes an `ssl.SSLContext` (`aio/client.py:418-420`); `inbox_prefix` sets the reply prefix
(`aio/client.py:430`).

**Auth callout** ([ADR-26](https://github.com/nats-io/nats-architecture-and-design/blob/main/adr/ADR-26.md), status
Implemented). "The mechanism will be used in both server configuration and operator mode servers" (ADR-26:18-19). The
request is signed by the server's nkey and goes to `$SYS.REQ.USER.AUTH`; "In operator mode, the response must be signed by
the account bound to the client" (ADR-26:52-53); the response's subject must be the per-connection user nkey the server
generated and its audience the server's id (ADR-26:157-163, enforced at `auth_callout.go:126-134`). The callout account
names which accounts a user may be bound to (ADR-26:173-178; `auth_callout.go:185-193`). In operator mode a user signed by a
**scoped** signing key gets that key's permissions, and a user claiming its own permissions is refused
(`auth_callout.go:239-253`; jwt `v2/signingkeys.go:94-106`, "scoped users require no permissions or limits set"). The
operator-mode prose page says only "Decentralized Auth — Coming soon!"
(`docs/running-a-nats-service/configuration/securing_nats/auth_callout.md:157-159`); the ADR, the server code and `nsc edit
authcallout` (nsc 2.15.0) implement it, and § 6 measures it.

**`default_sentinel`.** In operator mode, a client that presents no NATS JWT is given the configured bearer sentinel
(`auth.go:883-889`); the option must be a bearer user JWT (`jwt.go:72-88`). With the sentinel in the callout account, the pod
holds no NATS credential at all, only its identity token.

**Scoped signing keys.** "With scoped signing keys, you associate a signing key with a set of permissions. This
configuration lives on the account JWT" (`docs/using-nats/nats-tools/nsc/signing_keys.md:188`). When an account update
removes a signing key or alters its scope, the server closes every connection whose user it signed
(`accounts.go:4040-4056`). Account revocations close a callout user issued before the revocation's time, by user nkey or
`*` (`accounts.go:4016-4033`).

**Minting users on demand.** NATS documents the signer pattern itself, "Distributed User Creation": the process generates
its own user key and sends only the public key with its proof of identity; the signer returns a JWT
(`docs/running-a-nats-service/nats_admin/jwt.md:1340-1420`). The same page: "By generating users or accounts dynamically,
it becomes YOUR RESPONSIBILITY to properly authenticate incoming requests" and "For sign up service issued JWTs, ALWAYS set
the SHORTEST POSSIBLE EXPIRATION" (`jwt.md:1257-1259`).

**mTLS mapping.** `verify_and_map` maps a certificate to a configured user (`auth.go:933-960`, `auth.go:1366-1395`, URI SANs
included), and operator mode refuses configured users: "operators do not allow users to be configured directly"
(`jwt.go:97-98`). In operator mode a client certificate reaches an authorization decision only through a callout, which
receives it in `client_tls` (ADR-26:180-202).

**Kubernetes.** "The Kubernetes project recommends that you use the TokenReview API, because this method invalidates tokens
that are bound to API objects such as Secrets, ServiceAccounts, Pods or Nodes when those objects are deleted. For example,
if you delete the Pod that contains a projected ServiceAccount token, the cluster invalidates that token immediately and a
TokenReview immediately fails. If you use OIDC validation instead, your clients continue to treat the token as valid until
the token reaches its expiration timestamp." and "Your application should always define the audience that it accepts"
(kubernetes.io/docs/concepts/security/service-accounts, § Authenticating service account credentials). "The kubelet
proactively requests rotation for the token if it is older than 80% of its total time-to-live (TTL) … The application is
responsible for reloading the token when it rotates." (kubernetes.io/docs/tasks/configure-pod-container/configure-service-account,
§ ServiceAccount token volume projection).

**What else exists.** The NATS Helm chart configures a server (resolver, TLS, `merge`/`patch`) and issues no users
(`nats/values.yaml`, `config.resolver`, `tls`). NACK's `Account` CRD is NACK's own connection to the bus, from a creds or
seed Secret (`nack/deploy/crds.yml:1062-1110`), not an issuer. OpenBao 2.2.0 ships no NATS secrets engine (its builtin
registry lists `kubernetes`, `kv`, `ldap`, `pki`, `rabbitmq`, `ssh`, `totp`, `transit` and database plugins,
`registry.go:96-104`); its `transit` engine signs with ed25519, which a signer could use so that it never holds the signing
key (not measured). Synadia, the NATS maintainers, publish a callout service library, `synadia-io/callout.go`. No first-party
SPIFFE/SPIRE integration exists in nats-server or the NATS docs (searched both trees for `spiffe`/`spire`).

## 3. The mechanisms compared

| | (a) static user JWT + seed, no expiry (today) | (b) short `exp` user JWT from a platform signer, pod's nkey in memory | (c) auth callout, operator mode, SA token as `auth_token` (+ `default_sentinel`) | (d) mTLS client certs mapped to users (`verify_and_map`) | (e) per-service scoped signing key in the store, app mints its own user | (f) (c) with one scoped signing key per service |
|---|---|---|---|---|---|---|
| Identity proof | possession of a seed copied from the store | SA token to the signer (TokenReview); nonce signed by an in-memory key at connect | SA token checked by the callout at every connect (TokenReview) | a certificate's chain and subject | the store login (SA token) once; then the key itself | as (c) |
| Scope | the user's JWT, as minted | the signing key's scope (scoped key) or the signer's code | the minted user's permissions, or the key's scope | the configured user's permissions | the key's scope | the key's scope: the server enforces it whatever the callout writes |
| Lifetime | unlimited | minutes (`exp`) | minutes (`exp` of the minted user) | the certificate's | whatever the app writes | minutes |
| Revocation | account revocation pushed to the resolver | at `exp`; sooner only by revoking the key or the user nkey | next connect refused once TokenReview fails (pod deleted); live session by `exp`, account revocation, or removing the key | CRL/OCSP or expiry | remove the key: closes every session it signed (`accounts.go:4040-4056`) | as (c), and removing one service's key ends only that service's sessions |
| Secret the pod holds at rest | the seed (today in the store, read into memory) | none (projected token; seed generated in memory) | none (projected token; with `default_sentinel`, no NATS JWT either) | the private key on a volume | none at rest; the signing key in memory for its life | none |
| What the platform runs | operator mode, resolver | operator mode + a signer service (HTTP), with TokenReview rights and the signing keys | operator mode + a callout service on the bus (AUTH account), with TokenReview rights and the signing keys | config mode only, a CA, cert-manager | operator mode; the store holds a signing seed per service | as (c) |
| Rotation without restart | n/a (never rotates) | yes: refresh, or rebuild on close (§ 6) | yes: measured (§ 6) | rebuild the client with a new `SSLContext` (not measured) | yes, app-side | yes |
| Works in operator mode | yes | yes | yes (ADR-26; § 6) | no: operator mode refuses configured users (`jwt.go:97-98`); only through a callout | yes | yes |
| Works with nats-py 2.16.0 | yes | yes, measured | yes, measured (sentinel creds or `default_sentinel` + `token`) | TLS yes; mapping not in operator mode | yes | yes, measured |
| TLS requirement | recommended (JWT is public, seed never sent) | the signer's HTTPS is required (it carries the SA token); the bus connection carries only a JWT and a nonce signature | required: the SA token is a bearer credential inside `CONNECT` | intrinsic | the store's TLS | required |
| NATS's own production guidance | creds files are the documented default (`nsc` guide) | documented: "Distributed User Creation", shortest expiration (`jwt.md:1257-1420`) | documented: ADR-26 Implemented; config-mode guide; operator-mode prose "coming soon" | documented for config mode | signing keys are to be guarded; rotating one re-issues all its users (`signing_keys.md:11-13`) | scoped keys + callout are both documented; their combination is in the server code (`auth_callout.go:239-253`) |
| What fails | short-lived, scoped issuance and revocation (zero trust) | a bespoke signer API the platform must write; a deleted pod's JWT lives to `exp` | signer down: no new connection, and a refresh during an outage drops the session for the outage (§ 6.4) | operator mode | a long-lived signing secret inside every pod, minting any lifetime: fails "short-lived" | as (c) |
| Four facts | GitOps yes, cloud native yes, zero trust **no** | all yes | all yes | cloud native yes; zero trust yes; fails rask's operator-mode bus | zero trust **no** | all yes; the scope is declared, not coded |

(f) is the recommendation: (c) carrying (e)'s scope without (e)'s secret. Between (b) and (f), both meet the four facts. (f)
is chosen because the server, not the client, demands the platform's decision at every connect; the pod carries nothing but
its token; and a deleted pod's token stops working at the next connect. (b) keeps a live connection through a signer outage
until `exp`, which (f) does not when it refreshes early (§ 6.4). The rask client is the same for both, so the platform may
choose (b) without a rask change (§ 5).

## 4. What the platform runs and what rask ships

**The platform's (#787: the bus, its TLS and the issuing of users).**

- nats-server ≥ 2.15.2 in operator mode, TLS on the client port (a certificate chaining to a CA rask is given), an `AUTH`
  account whose external authorization names the callout service's user as its `auth_user` and the application account as
  its `allowed_account` (`nsc edit authcallout`), a bearer sentinel user in `AUTH` with every subject denied, set as the
  server's `default_sentinel`.
- In the application account, one **scoped signing key per service**, its scope the service's row of rask's permission
  table, plus the account's JetStream limits.
- The callout service, run with at least two replicas (a queue group on `$SYS.REQ.USER.AUTH`): it verifies the request's
  server signature, sends the presented token to the Kubernetes TokenReview API with the bus audience, maps the
  authenticated `system:serviceaccount:<ns>:<sa>` to the service's scoped key, and answers with a user whose subject is the
  request's `user_nkey`, whose `exp` is at most the table's lifetime, and which carries no permissions of its own. Its
  ServiceAccount needs `system:auth-delegator`. Optionally an xkey for the request and response (ADR-26:204-235).
- Reconciling the scoped keys from rask's rendered table on every change (removing a key closes that service's sessions,
  `accounts.go:4040-4056`).

**rask's (in git, in the product chart).**

- `nats.url` with the `tls://` scheme, and a value naming the CA (a ConfigMap or the platform's trust bundle).
- `nats.auth.audience`: the bus audience, projected per pod through `lance.identityTokens`
  (`chart/templates/_helpers.tpl:507-519`, today `expirationSeconds: 600`, mounted at
  `/var/run/secrets/rask/identity/<audience>/token`).
- The permission table `nats.auth.users`, rendered as one object the platform reads, for example a ConfigMap
  `<release>-nats-permissions`:

  ```json
  {
    "audience": "<nats.auth.audience>",
    "maxLifetimeSeconds": 600,
    "services": {
      "system:serviceaccount:<ns>:<release>-sa-catalog": {
        "role": "catalog",
        "publish": ["lineage.events.v1", "catalog.control.v1", "..."],
        "subscribe": ["_INBOX.catalog.>"]
      }
    }
  }
  ```

  Each row keyed by the ServiceAccount the projected token names (`lance.saName`), so the identity is the pod's, never a
  name it chose. The rows give each service its own inbox prefix (nats-py `inbox_prefix`): today every row but
  `maintenance-worker` subscribes `_INBOX.>` (`chart/values.yaml:100-139`), which lets any service read another's replies.
- What the chart refuses on a real deployment (the platform's store, `openbao.enabled` false): a `nats.url` that is not
  `tls://`; no CA; an empty `nats.auth.audience`; a table row with `>` or `$JS.API.>` in `publish`, or an empty list.
- **The dev stand-in in `deploy/fixtures/nats`**: operator mode on (`auth.server`), the `AUTH` account, the sentinel as
  `default_sentinel`, TLS from the dev CA, and a stand-in callout Deployment in the fixture with TokenReview rights, reading
  rask's rendered table; the nats-auth lane (`.dagger/nats_auth.go`) proves rask's client against it as § 6 does.

## 5. How the client is written

One `lakehouse` seam owns the connection. It is mechanism-agnostic: the same code serves (b) and (f).

- **Credentials as callables.** `token=` a function that re-reads the projected token file on every call (the kubelet
  rotates it at 80% of its TTL; the file is read at each connect, measured with five rotations in § 6.3). Under (b), also
  `user_jwt_cb` returning the JWT the signer last returned and `signature_cb` signing the nonce with a key generated in
  memory at start (`nkeys.encode_seed(os.urandom(32), PREFIX_BYTE_USER)`), never written anywhere.
- **Refresh before expiry.** The seam knows the user's lifetime (the table's `maxLifetimeSeconds`, or the JWT's `exp`
  under (b)) and calls `force_reconnect()` at a fixed fraction of it. Under (b) it fetches the new JWT first and reconnects
  only once it holds one, so a signer outage costs nothing until `exp`.
- **Rebuild on close.** `closed_cb` builds a new client, a new JetStream context and a new `pull_subscribe_bind` on the
  same durables, in-process, with backoff; `max_reconnect_attempts=-1` so a refused connect is retried. The process ends
  only on the rule ADR 0199 adopts (a background loop that ends ends the process), never on a closed connection.
- **In-flight work.** A fetch pending on the closed client raises `ConnectionClosedError`; an `ack` on a message from the
  closed client raises `ConnectionClosedError`. The server does not hand a new message to the dead connection's pull request:
  a message published after the rebuild reached the new subscription at once (§ 6.2). Messages held unacknowledged are
  redelivered after the durable's `ack_wait`, counted in `num_delivered` (§ 6.2). So a handler acks only through the client
  that is current, treats a failed ack as "will be redelivered", and stays idempotent per message id.
- **Publishing.** Every publish carries `Nats-Msg-Id` (#787 item 1) and is retried on timeout; a duplicate PubAck counts as
  delivered. Measured: publishes that timed out while the bus refused the client were retried with the same id, and the
  stream held each message once (§ 6.4).

## 6. Measurements, 2026-11-10

Versions: nats-server v2.15.2 and nsc 2.15.0 exported through Dagger from `nats:2.15.2-alpine` and
`natsio/nats-box:0.20.2` (the images `.dagger/test.go:12` and `.dagger/nats_auth.go:12` pin), on aarch64; nats-py 2.16.0,
nkeys 0.2.1, Python 3.13.15 from the repository's uv environment. One server, operator mode, MEMORY resolver, JetStream;
an `APP` account with one scoped signing key (`rask.svc.>`, `$JS.API.CONSUMER.MSG.NEXT.BUS.svc`, `$JS.ACK.BUS.svc.>` to
publish; `_INBOX.>` to subscribe); stream `BUS`, durable pull consumer `svc`, explicit ack. Users live 8 s. The workload
publishes a numbered message every 50 ms with `Nats-Msg-Id` and retries on error, and pull-fetches (batch 10) and
`ack_sync`s on the current client; a run counts PubAcks against messages acknowledged by the consumer. Scripts and outputs
are in `/tmp/claude-1001/-home-gabriel-Desktop-repos-rask/dc2f4b76-368c-47d6-925e-3be8f72de0c5/scratchpad/bus-creds/`
(`scripts/measure_expiry.py` → `run1.out`, `run1/results.json`; `scripts/measure_inflight.py` → `run2`;
`scripts/measure_default_sentinel.py` → `run3.out`).

### 6.1 A user JWT expiring under a live nats-py client (mechanism b)

- The server logs `User Authentication Expired - Account:…/APP - jwt:U…` then `Client connection closed: Authentication
  Expired` at the second the JWT's `exp` names (`run1/server/nats-server.log:47-48`).
- nats-py: `disconnected_cb` and `closed_cb` at the same instant, `last_error = Error('nats: user authentication expired')`,
  status closed, no `reconnected_cb`. **The same with callbacks that return a fresh JWT**: `user_jwt_cb` was called once,
  at connect, and never again. Fresh callbacks alone do not survive an expiry.
- A publish outside the key's scope (`rask.other.x`): `permissions violation for publish to "rask.other.x"`, connection kept.
  The scope came from the signing key; the minted user carried no permissions.

### 6.2 Rebuild and refresh (mechanism b)

| run | expiries / refreshes | PubAcks | acked by consumer | lost | redelivered | errors seen |
|---|---|---|---|---|---|---|
| rebuild on close | 3 closes, 3 rebuilds | 586 | 586 | 0 | 0 | 4 fetches raised `ConnectionClosedError` |
| `force_reconnect()` at 60% of lifetime | 6 refreshes, 0 closes | 586 | 586 | 0 | 0 | 1 fetch raised `ConnectionClosedError` |

Held across an expiry (durable `ack_wait` 20 s, three messages fetched and not acked, a pull request waiting): the waiting
fetch and an `ack_sync` on a held message both raised `ConnectionClosedError: nats: connection closed`; after the rebuild, a
newly published message arrived at once (`num_delivered` 1), and the three held messages arrived 12 s after the close,
which is 20 s after their delivery (`num_delivered` 2). Nothing was lost.

### 6.3 Auth callout in operator mode (mechanism c/f)

A Python stand-in callout service, connected as the `AUTH` account's `auth_user`, verified each request's server signature,
accepted tokens from a table, and answered with a user signed by the `APP` scoped key (8 s), the response signed by an
`AUTH` signing key with `issuer_account` set.

| run | client holds | result |
|---|---|---|
| refused token at connect (`allow_reconnect=False`) | sentinel creds + token | `nats.errors.Error: nats: 'Authorization Violation'` |
| no rebuild | sentinel creds + token | closed at expiry, `user authentication expired`, as § 6.1 |
| rebuild on close, token file rotated every 5 s | sentinel creds + token | 585 PubAcks, 585 acked, 0 lost, 0 redelivered, 3 rebuilds |
| `force_reconnect()` at 60%, token rotated | sentinel creds + token | 585 / 585, 0 lost, 6 refreshes, 0 closes |
| rebuild on close, `default_sentinel` set | **only the token** | 585 / 585, 0 lost, 3 rebuilds; the callout saw `tok-0` … `tok-5`, so each connect re-read the file |
| refresh, `default_sentinel` set | only the token | 585 / 585, 0 lost, 6 refreshes |
| callout stopped 12 s across an expiry, rebuild on close | sentinel creds + token | 391 / 391, 0 lost; closed at expiry, the server logged `Authorization callout response not received in time` every ~2.5 s while nats-py retried, rebuilt 1.5 s after the callout returned |

The scoped key's scope applied to callout users too (the same out-of-scope refusal as § 6.1).

### 6.4 A refresh while the signer is down (mechanism f)

`default_sentinel`, only the token, `force_reconnect()` at 60%, the callout stopped for 12 s: the refresh dropped the
session at once and nats-py stayed disconnected until 1.3 s after the callout returned (12.5 s); 5 publishes timed out and
were retried; 344 PubAcks, 344 acked, 0 lost, 0 duplicates. Under (f) an early refresh trades the rest of the user's
lifetime for a fresh decision; the seam therefore refreshes late (at most the lifetime minus the callout's timeout and a
margin), and the callout runs replicated.

## 7. Failure modes

- **Signer (callout) down.** Live sessions keep working until their `exp`; no new connection or refresh succeeds; nats-py
  retries for ever and resumes within seconds of the signer's return (§ 6.3, § 6.4). The lifetime bounds how long an outage
  is invisible. Under (b) a pod holding a JWT reconnects until `exp` without the signer.
- **Projected token rotation.** Read on every connect, so a rotated file is used at the next connect (§ 6.3). The token's
  `expirationSeconds` must exceed the bus user's lifetime plus the refresh margin, so a connect never presents a token about
  to lapse: today's 600 s (`_helpers.tpl:514`) with a 300 s user leaves the kubelet's 80% rotation inside the window.
- **Clock skew.** The server measures `exp` on its own clock (`client.go:1384`); the signer writes `exp` on its clock. A
  skew of seconds shortens or lengthens each session by that much; with lifetimes of minutes and NTP on both, it is harmless.
  A refresh scheduled by the client's clock uses the lifetime as a duration, not `exp`'s absolute time.
- **Account signing key rotation.** Add the new scoped key, switch the signer, wait one maximum lifetime, remove the old key:
  removing it closes every session it signed (`accounts.go:4040-4056`), which the client survives by rebuilding (§ 6.2), so a
  rotation done in one step costs one reconnect per pod, no lost message.
- **Revoking one service.** Remove that service's scoped key, or its row in rask's table and the platform's reconcile does:
  its sessions close and every new connect is refused. A deleted pod's token fails TokenReview at once (Kubernetes docs, § 2),
  so its next connect is refused; its live session lasts to `exp` unless the account also revokes its user nkey
  (`accounts.go:4016-4033`).
- **Plaintext bus.** Under (c)/(f) the SA token crosses in `CONNECT`; on `nats://` it is readable on the wire, and a bus
  audience token replayed by an observer would be accepted until it expires. TLS is a precondition, and the chart refuses a
  real deployment without it (§ 4).

## 8. Not confirmed

- **A three-node cluster.** Every measurement ran on one server; expiry and callout on a clustered, R3 bus (the estate's
  shape) were not run. Looked: the measurement scripts only.
- **TokenReview from the callout.** The stand-in accepted tokens from a table; a real TokenReview, its latency, and the
  `system:auth-delegator` binding were not exercised. Looked: the Kubernetes docs (§ 2) only.
- **TLS.** No run used TLS; nats-py's TLS against a platform CA, and whether that CA chains to Python's trust store, were not
  measured.
- **xkey encryption** of callout requests and responses (ADR-26:204-235) was not set up; nkeys 0.2.1 ships `nacl.public`,
  so a Python callout could, unmeasured.
- **Revocation and key rotation live.** Closing sessions on a removed scoped key and on an account revocation is read from
  `accounts.go:4016-4056`, not measured.
- **Mechanism (b) with a signer outage**, and its signer as an HTTP service: (b) was measured with an in-process minter; that
  a pre-fetched JWT keeps the session through an outage is read from `aio/client.py:1814-1848`, not measured.
- **(d) mTLS** was not measured; that operator mode cannot map certificates to users is read from `jwt.go:97-98` and
  `auth.go:933-960`.
- **The operator-mode callout's official prose** is "Coming soon!" (`auth_callout.md:157-159`); nothing in nats.docs states
  operator-mode callout as production guidance beyond ADR-26. nats-by-example's callout examples were not read (the tree
  listing through `gh api` returned nothing for `callout`).
- **The platform's NATS version, TLS and whether it will run a callout**: outside this repository; the owner's or the
  platform team's to state.
- **OpenBao transit as the signer's key holder**: transit's ed25519 signing is read from the builtin registry only; whether its
  signature format and the nkey public key line up was not tried.
- **How the nats Go client treats `User Authentication Expired`** (as a comparison) was not read.
