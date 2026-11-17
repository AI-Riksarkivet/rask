# 0220. A bearer names the same caller in Rust as in Python (2026-11-17)

**Status:** accepted; the third feature of the port (ADR 0217), and its first zero-trust code. It decides who a
verified token names, and fetches the keys it is verified with from each issuer over HTTP. Source: the
owner, 2026-11-17 ("try to port the code to rust following these principles, keep ADR of features").

## Context

Every door asks one question before anything else: who sent this bearer. In Python the answer is spread over
`lakehouse.governed` (`principal`, `settings`, `oidc`, `idp_identity`, `machine_identity`, `door_caller`). A port
that names one caller differently, or takes one token Python refuses, is a hole in every door at once.

## Decision

1. **`lakehouse_core::auth` is the auth plane**, six modules:

   | Rust | Decides | Python it answers for |
   | --- | --- | --- |
   | `principal` | what a principal is (an idp id and a subject), its key and its OpenFGA user | `governed.principal` |
   | `settings` | the `RASK_OIDC_*` and `RASK_SA_*` settings, and which combinations are refused | `governed.settings` |
   | `claims` | what a token's claims must say at a given time | PyJWT's claim checks and `oidc.IDToken` |
   | `identity` | whether verified claims name a person, a client or a service account, and which | `idp_identity`, `machine_identity` |
   | `verifier` | which issuer a token is checked against, its signature, and the door's answer | `oidc.OIDCVerifier.verify`, `door_caller.caller_of` |
   | `issuer` | an issuer's keys: discovery, the key set, when either is fetched, and with which credential | `oidc.OIDCVerifier` (`_fetch`, `_resolve`, `refresh`, `_signing_key_for`), `auth_lifespan.attach_auth` |

2. **The claims are judged by one function of the claims and the time** (`claims::judge`). The JWT library checks
   the signature and that the algorithm is one the issuer signs with, and nothing else. What a token must say is in
   this repository, not in a library's defaults.
3. **Which verifier a token meets is decided by the issuer it claims**, as in Python: a token claiming the
   service-account issuer is only ever verified as a service account's, and any other only as a person's or a
   client's. A verifier that is not up answers 503, never "no such caller".
4. **A verifier reads keys through one seam** (`KeySource`): the issuer's key for a key id, no such key, or the issuer
   could not be asked. The first is `Ok(Some)`, the second `Ok(None)` and a 401, the third `Err(Unavailable)` and a
   503: a token is never told it is bad because its issuer did not answer.
5. **Keys come from the issuer over HTTP** (`issuer::IssuerKeys`, a `KeySource`). Its decisions are functions of
   values: `safe_algorithms` (RS256/384/512 and ES256/384, intersected with what discovery advertises, the whole list
   where it advertises none, and the issuer unavailable where nothing is left), `discovered` (a document naming
   another issuer is refused; a `jwks_uri` under the issuer moves under the discovery override), `require_https`
   (discovery and the key set are HTTPS unless `RASK_OIDC_ALLOW_INSECURE`; the service-account issuer always),
   `signing_keys` (keys marked for signing or for nothing, with a key id, each verifying only with its own
   algorithm), and `Pace` (one fetch per 30 seconds per issuer that a request may cause, counted from when it began).
   The shell fetches with `reqwest` over rustls, follows no redirect and gives each fetch 15 seconds. A request reads
   the keys held: an issuer nothing holds yet is fetched by a request at most once per 30 seconds, and an unknown key
   id re-reads the held issuer's key set at most once per 30 seconds, each pace its own. `refresh` fetches every
   issuer and holds each that arrived whole, so an outage serves the last good keys; `issuer::start` runs it at boot
   (a failure is logged, never fatal) and then every `RASK_OIDC_CACHE_TTL` seconds. The service-account issuer is
   fetched with the bearer in `RASK_SA_FETCH_TOKEN_FILE`, read on every fetch and marked sensitive, and trusts only
   `RASK_SA_CA_FILE`. `issuer::authenticator` builds the `Authenticator` from `AuthSettings` with this source for
   both verifiers.
6. **Parity is shown through the real Python path** (`scripts/_port_auth_vectors.py` → `rust/vectors/auth.json`):
   57 callers are put to `door_caller.caller_of` with the real verifiers and settings, and 37 environments to the
   real settings class. Only the key source is replaced, by one recorded key, so the vectors need no issuer and the
   repository holds no private key. A time claim is recorded as an offset from the moment of the check. The Rust
   door answers every caller the same: the same principal key and OpenFGA user, or a refusal with the same name,
   code and message. Five planted defects (an expiry that ignores the leeway, an unchecked audience, an unchecked
   service-account lifetime, account tokens routed to the people's verifier, an idp id shared by two issuers) each
   failed between 1 and 8 cases.
7. **The key source is shown at its seam** (`rust/crates/lakehouse-core/tests/issuer.rs`): an in-process issuer on a
   loopback port serves discovery and a key set of ES256 and RS256 keys generated when the test runs, and every
   assertion goes through `Authenticator::caller_of`. Seven tests: a token signed by the issuer's key names its
   caller, one signed by another key or for another audience is a 401; a rotated key is found by one paced re-read
   and a second unknown key id inside the pace costs no fetch; an issuer that is down is a 503 that costs one fetch
   per pace and recovers, and its held keys outlive its next outage; a document naming another issuer, or no allowed
   algorithm, is a 503; an http issuer is fetched only where insecure issuers are allowed; the discovery override is
   where discovery and the key set are fetched; the service-account issuer is reached over TLS through its own CA with
   the bearer read afresh after it rotates, and a public issuer is sent none. The pace is crossed on an injected
   clock. Three planted defects (no pace, no `jwks_uri` rewrite, no issuer check) each failed 1 or 2 of them.

## What differs from the Python, on purpose

Measured on both implementations 2026-11-17:

- **A time claim is a JSON number.** Python also took `exp` and `nbf` written as numeric text (`"1700000000"`).
  Both refuse an `exp` or `iat` with a fraction and take a whole one written as a float.
- **The settings are read from the process's environment only**, and their refusal messages are the port's own.
  Which environments are refused is the same.
- **ES512 is not accepted**: the JWT library verifies P-256 and P-384 only, so it is absent from the allowlist and a
  P-521 key is not held.
- **An issuer is held only whole**, its discovery document with a key set that holds a signing key, and both are
  fetched under the one pace. Python holds the document as soon as it arrives, and its key set is then fetched by
  each request that finds none, without a pace.
- **A public issuer is trusted through the platform's trust store** (rustls-platform-verifier), where Python's HTTP
  client trusts the certifi bundle. A private issuer's CA file replaces either.
- **A key verifies one algorithm**: the one its `alg` names, or by its type where it names none (RS256 for an RSA
  key, ES256 or ES384 by its curve). Python tries every allowed algorithm the key's type can do, so it takes an RS512
  token under an RSA key that names no `alg`. The issuers in use name it.
- **A key the library cannot read is skipped where PyJWT infers more**: an EC key with no `crv`, which PyJWT reads as
  P-256. A key whose `alg` contradicts its type is held and verifies nothing, where PyJWT skips it; both refuse the
  token with a 401. The 503 messages are the port's own; which answers are 401 and which 503 is the same.

## Not ported yet

- **The audit trail's export.** An authentication is logged under the `lance.audit` target; routing it to the
  governance trail arrives with telemetry.

## Consequences

- `lakehouse-core` is 1,637 of its 11,234 budgeted lines; the key source is 334 of them.
- A service authenticates real tokens once it builds its authenticator with `issuer::authenticator` and calls
  `issuer::start` at startup.
- A change to who the Python door names changes a vector, and `make rust-check` fails until the Rust door agrees.
