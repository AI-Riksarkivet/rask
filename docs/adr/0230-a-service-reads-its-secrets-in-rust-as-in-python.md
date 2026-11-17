# 0230. A service reads its secrets in Rust as in Python, and a verifier the keys its signers publish (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), and its third zero-trust code after ADR 0220 and ADR 0223. It
logs a Rust service in to the secret store as itself, keeps that login, reads secrets under it, and reads each signer's
published public keys for the verifier of ADR 0223. No Rust service opens a session yet. Source: the owner, 2026-11-17
("try to port the code to rust following these principles, keep ADR of features").

## Context

A service's only secret source is the secret store (OpenBao, a Vault-compatible API), reached as the pod's own
identity: the process presents its projected ServiceAccount token at the store's Kubernetes auth method, as the role
bound to its account, and reads KV v2 secrets under the store token the login returns. In Python this is
`lakehouse.governed.secrets` (`SecretStore`, `read_required_secret`, `secret_store_session`), the readers' fetch in
`lakehouse.governed.signing_key` (`signing_public_secret`, `published_key_fetch`) and the key cache in
`lakehouse.lineage.keys.PublishedKeys`, wired by lineage in `services.signature.store_published_keys` and `main`. ADR
0223 left the reading of published keys to this feature: until it lands, no Rust door can verify a signature.

## Decision

1. **`lakehouse_core::secrets` is the session**, three modules:

   | Rust | Decides | Python it answers for |
   | --- | --- | --- |
   | `decide` | the `RASK_SECRET_STORE_*` settings and their refusal, a login's lease (renewed at half, used to nine tenths), whether a login's answer names a token this process takes, whether a read's answer is a KV v2 read of string fields, the secret-name rule, whether a credential fits a header, the backoff, and the loop's row of unanswered logins with what it says once | `SecretStoreSettings`, `_Issued`, `_ReadAnswer`, `_Login`, `_SECRET_NAME`, `fits_a_header`, tenacity's `wait_exponential_jitter`, the row in `keep_logged_in` |
   | `store` | the login, the loop's pass, a read and its retries, the readiness report, a required secret, and the session a service keeps | `SecretStore`, `read_required_secret`, `secret_store_session` |
   | `published` | which secret a signer's keys are under, the per-identity cache, and when an unknown key id may cause a read | `signing_key_name`, `signing_public_secret`, `published_key_fetch`, `PublishedKeys` |

2. **The decisions are functions of values; the session is a thin shell around them.** The shell reads the projected
   token from its file at every login and keeps it nowhere, never takes it from the environment, and never logs it.
   The store token is held as a `StoreToken` that prints nothing of itself and leaves only as an `X-Vault-Token` header
   marked sensitive. The client follows no redirect and uses no proxy, so the token reaches only the declared address;
   a private store is trusted through `RASK_SECRET_STORE_CA_FILE` alone. A login is held only with a lease of 60 to
   3600 s and a token that is visible ASCII, so no credential this process holds outlives an hour.
3. **The fail-closed distinction is kept exactly.** `read` answers an empty map, logged, when the store did not serve a
   secret within its budget; `read_required` and `read_required_secret` refuse it (and a secret whose field is empty),
   so a start never runs on an empty key; a name outside `[a-z0-9][a-z0-9-]{0,252}` is refused before anything is
   asked. A 403 on a read is the store's answer and logs in no further; only the store's answer about the token itself
   (`auth/token/lookup-self`) tells a dropped token from a secret the policy does not name.
4. **The loop is the Python loop.** `keep_logged_in` asks the store about the held token each pass until half the lease
   is used, logs in again on a refusal, and sits out 1, 3, 7, then 15 passes after each further login the store refuses
   the question, a login the lease asks for counting in the row. While a pass has been made within a minute, a read
   that finds no usable login waits for the loop and never logs in itself. One login is in flight at a time
   (`tokio::sync::Mutex`), each wait bounded by what is left of its attempt. `Session::open` logs in with the boot budget
   (ten attempts of five seconds from a backoff of three) and keeps the loop, ended when the session is dropped.
5. **`PublishedKeys` implements `signing::PublicKeySource` as it stands**: `for_event(kid)` stamps one verification with
   when it began and the key id it names; `published` answers from a cache younger than 60 s, `refresh` only from a read
   begun after the key id was first met, and otherwise declines with `KeysUnavailable` while the last read ended less
   than 30 s ago. A failed read answers every verification that began before it ended. Each identity has its own lock
   (single-flight) and its own bounded memory of 8192 key ids. The trait is synchronous, so `store_fetch` runs the read
   on the runtime and the verifier blocks on it from a blocking thread, as Python verifies in its threadpool. No change
   to the trait was needed.
6. **Parity is shown through the real Python** (`scripts/_port_secrets_vectors.py` → `rust/vectors/secrets.json`). The
   real `SecretStore` is driven with an injected clock and an `httpx.MockTransport` answering from a script, and the
   real `PublishedKeys` with a scripted fetch; every step records what was asked (method, path, the token header, the
   login's body) and what the object did. Recorded: 21 session scenarios of 147 steps and 157 requests (a first login;
   renewal at half the lease; leases out of bounds; tokens a header cannot carry; a login refused the question once and
   at every attempt; a dropped token; the backoff row to its cap; a lease-asked login inside a row; an outage with the
   login usable and then lapsed; a read waiting for the passing loop; a rotated and an unreadable projected token;
   eleven reads the store does not serve; retries; twelve bad names; required secrets; the readiness report with and
   without a login; mounts and an address with a path), 5 published-key scenarios of 64 steps (a cache hit and expiry,
   an unknown key id inside and outside the pace, a redelivery judged on a read begun since its first sighting, failed
   reads, identities paced apart), 13 environments, 29 login answers, 16 read answers, 11 leases, 17 names, 11 header
   texts, 72 backoff waits with the jitter drawn from a fixed value, and 9 signers' secret names. The tokens are
   fixture strings. The Rust tests replay every scenario through a local server that gives the same answers in order
   and records the same requests, and assert the same requests, outcomes and said events. The generator writes the
   same file on every run.
7. **End to end** (`tests/secrets.rs`), against an in-process store speaking the login, `lookup-self` and KV v2 read:
   a read is served under the login the role made, presenting the projected token; a projected token rotated between
   logins is read again; a dropped token is replaced by the loop's pass and never by a read; a store that is down fails
   a required secret closed and serves again once up; eight readers that find no login share one; and a run event
   signed with an Ed25519 key generated at test time verifies through `signing::verify_event` against the keys read
   from the store, a rotated key is found by one paced re-read, and a signature under the wrong key is refused.
8. **Against a real OpenBao** (`tests/secrets_openbao.rs`, ignored unless `.localbin/bao` is fetched): OpenBao 2.2.0,
   the version the development fixture runs, pinned in `scripts/toolchain.sha256` (linux x86_64), in dev mode with a
   JWT auth method mounted at `kubernetes`, a role bound to one subject and audience with only the rendered policy's
   grants, and a projected token signed by a P-256 key generated at test time. The session logs in, reads a secret the
   policy names, is refused one it does not name, and the readiness report passes.
9. **Five planted defects each failed tests**: reading the projected token once and caching it (the rotation e2e test
   and the session vectors), renewing at the full lease (the lease vectors and the session vectors), using a login past
   its lease (the rotation e2e test and the session vectors), re-reading published keys on every unknown key id (the
   signed-event e2e test and the published-key vectors), and letting every reader log in (the shared-login e2e test).

## What differs from the Python, on purpose

Each is outside the corpus:

- **A lease is a JSON number**, whole or a float with no fraction. Python also took numeric text (`"900"`, `"9_00"`,
  `" +900.0 "`); OpenBao writes a number. As in ADR 0220.
- **The session is a value the service holds** (`Session`, an `Arc<SecretStore>`), not module state; a process with no
  store has none (`Session::from_settings` answers `None`), and `read_required_secret` takes the session it reads.
- **A signer whose key name is no secret name reads as unavailable.** Python raised `ValueError` out of the verifier;
  the signers are configured, so neither happens on the estate.
- **A verifier waits at most 7 s for the read of a published list**, three of its bounded waits; Python's wait had no
  bound beyond theirs.
- **Messages are the port's own and quote neither the token file's path nor a transport error's URL.** Which outcome
  is a refusal, an empty read or a 503 is the same.
- **A public store is trusted through the platform's trust store**, where Python's client trusts the certifi bundle;
  `RASK_SECRET_STORE_CA_FILE` replaces either. As in ADR 0220.
- **Closing the session ends a retry's wait with the attempt's last error**; Python made one more attempt that found
  the session closed. Either way nothing more is asked.

## The four facts

- **GitOps:** the store, role, mounts and token path come from the chart's `lance.secretStoreEnv`; nothing is set by hand.
- **Cloud native:** the store is the platform's, named by address; the projected token is the workload's own identity.
- **Lance-idiomatic:** not applicable; no table is read or written.
- **Zero trust:** no secret through the environment; the projected token is read per login and the store token is held
  in memory for at most an hour, never logged, never sent elsewhere; a lease the loop cannot keep is no login.

## Not ported yet

- **The signer's side** (`SigningKeyHolder`: resolving the private seed, ready only while its key is listed).
- **The readiness dependency** (`secret_store_dependency`): `SecretStore::answers` is what it will call.
- **A Rust service opening a session**, and lineage's doors verifying with `PublishedKeys`.

## Consequences

- `lakehouse-core` holds 781 counted lines of this feature (decide 174, store 427, published 174, mod 6), within its
  11,234 budget.
- A change to the lease, the loop, the read rules or the key cache changes a vector, and `make rust-check` fails until
  the Rust agrees.
