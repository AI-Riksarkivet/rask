# 0220. A bearer names the same caller in Rust as in Python (2026-11-17)

**Status:** accepted; the third feature of the port (ADR 0217), and its first zero-trust code. It decides who a
verified token names. Fetching an issuer's keys is not ported yet, so no door authenticates with it. Source: the
owner, 2026-11-17 ("try to port the code to rust following these principles, keep ADR of features").

## Context

Every door asks one question before anything else: who sent this bearer. In Python the answer is spread over
`lakehouse.governed` (`principal`, `settings`, `oidc`, `idp_identity`, `machine_identity`, `door_caller`). A port
that names one caller differently, or takes one token Python refuses, is a hole in every door at once.

## Decision

1. **`lakehouse_core::auth` is the auth plane**, five modules:

   | Rust | Decides | Python it answers for |
   | --- | --- | --- |
   | `principal` | what a principal is (an idp id and a subject), its key and its OpenFGA user | `governed.principal` |
   | `settings` | the `RASK_OIDC_*` and `RASK_SA_*` settings, and which combinations are refused | `governed.settings` |
   | `claims` | what a token's claims must say at a given time | PyJWT's claim checks and `oidc.IDToken` |
   | `identity` | whether verified claims name a person, a client or a service account, and which | `idp_identity`, `machine_identity` |
   | `verifier` | which issuer a token is checked against, its signature, and the door's answer | `oidc.OIDCVerifier.verify`, `door_caller.caller_of` |

2. **The claims are judged by one function of the claims and the time** (`claims::judge`). The JWT library checks
   the signature and that the algorithm is one the issuer signs with, and nothing else. What a token must say is in
   this repository, not in a library's defaults.
3. **Which verifier a token meets is decided by the issuer it claims**, as in Python: a token claiming the
   service-account issuer is only ever verified as a service account's, and any other only as a person's or a
   client's. A verifier that is not up answers 503, never "no such caller".
4. **A verifier reads keys through one seam** (`KeySource`): the issuer's key for a key id, no such key, or the issuer
   could not be asked. The source that fetches them over HTTP is the next slice.
5. **Parity is shown through the real Python path** (`scripts/_port_auth_vectors.py` → `rust/vectors/auth.json`):
   57 callers are put to `door_caller.caller_of` with the real verifiers and settings, and 37 environments to the
   real settings class. Only the key source is replaced, by one recorded key, so the vectors need no issuer and the
   repository holds no private key. A time claim is recorded as an offset from the moment of the check. The Rust
   door answers every caller the same: the same principal key and OpenFGA user, or a refusal with the same name,
   code and message. Five planted defects (an expiry that ignores the leeway, an unchecked audience, an unchecked
   service-account lifetime, account tokens routed to the people's verifier, an idp id shared by two issuers) each
   failed between 1 and 8 cases.

## What differs from the Python, on purpose

Measured on both implementations 2026-11-17:

- **A time claim is a JSON number.** Python also took `exp` and `nbf` written as numeric text (`"1700000000"`).
  Both refuse an `exp` or `iat` with a fraction and take a whole one written as a float.
- **The settings are read from the process's environment only**, and their refusal messages are the port's own.
  Which environments are refused is the same.

## Not ported yet

- **Keys from the issuer:** discovery, the key set and its refresh, the rule that an issuer is HTTPS, the discovery
  override, and the credential a private issuer is fetched with. ES512 will not be accepted: the JWT library signs
  and verifies P-256 and P-384 only.
- **The audit trail's export.** An authentication is logged under the `lance.audit` target; routing it to the
  governance trail arrives with telemetry.

## Consequences

- `lakehouse-core` is 1,302 of its 11,234 budgeted lines.
- A change to who the Python door names changes a vector, and `make rust-check` fails until the Rust door agrees.
