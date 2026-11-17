# 0234. The outbox and its store session are spoken in Rust as in Python (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217), the producer's and the relay's object-store side of the event
path ADR 0221 models, and the fourth zero-trust code after ADR 0220, ADR 0223 and ADR 0230. It stages, lists, reads and
drops outbox events on the object store, under a session the pod's projected ServiceAccount token is exchanged for. No
Rust service opens a store session or runs a relay tick yet. Source: the owner, 2026-11-17 ("try to port the code to rust
following these principles, keep ADR of features").

## Context

A producer stages an event's envelope under `<outbox>/<its id>.json` before it publishes it and drops it once the bus
took it; lineage's relay lists what is still staged, oldest first, reads it, records and publishes it again, then drops
it (`specs/lineage/EventPath.tla`: Stage, Publish, Drop for the producer; Pick, the Relay steps and RelayDrop for the
relay). In Python this is `lakehouse.outbox` (`stage_event`, `drop_event`, `read_event`, `list_events`, `backlog`,
`publish_with_outbox`, `UnpublishedEventError`, `left_staged`), read by `lineage.api.relay._drain_outbox`, on
`lakehouse.storage` (the boto3 S3 client, the STS client, `s3_errors`) and `lakehouse.store_session` (the exchange,
`SessionCache`, `StoreSession`, `StoreIdentitySettings`). ADR 0231 ported what the relay does with one staged event and
left listing and reading the outbox to this feature.

## Decision

1. **Two modules of `lakehouse-core`, neither touching Lance or Arrow:**

   | Rust | Decides or does | Python it answers for |
   | --- | --- | --- |
   | `storage::session` | the `<PREFIX>_S3_*` settings and their refusals; the unsigned `AssumeRoleWithWebIdentity` exchange with the token read from its file at each exchange; the session held for the duration asked, on this process's clock; renewal at half; nothing handed out within 60 s of the end; the boot's 20 attempts 3 s apart; the loop's 15 s pass and what it says once | `StoreIdentitySettings`, `require_one_store_identity`, `exchange`, `SessionCache`, `held_session`, `StoreSession` |
   | `storage::sigv4` | SigV4 for S3: the canonical request, the payload's SHA-256, the signing key | botocore's `S3SigV4Auth`, which the boto3 client signs with |
   | `storage::s3` | put (plain, or `If-None-Match: *`), get, delete, a one-level listing read to its last page; a refusal's code read from its XML | `lakehouse.storage.s3_client` and the calls `outbox` and `records.create_json` make on it, and pyarrow's listing |
   | `storage` (mod) | the `StorageError` family and `classify`, the `KeyPair` that prints none of its secret | `lakehouse.storage.errors` (`s3_errors`), `records.create_json`'s 412, `S3KeyPair` |
   | `outbox` | the object an event is staged at; the staged events of a listing, oldest first and bounded; the backlog; stage, drop, read, read the staged; `publish_with_outbox` over a `Producing` trait in the model's order | `lakehouse.outbox` |

   What a staged text parses as is `lakehouse_bus::CloudEvent::parse` and `staged_text` (ADR 0225, 98 and 81 vectors):
   the outbox hands bytes over and reads none of them.
2. **The relay's half is what its tick will call:** `Outbox::listing` (metadata only), `outbox::backlog(listing, now)`,
   `Outbox::read_staged(listing, limit)` (oldest first, an event dropped since the listing passed over), and
   `Outbox::drop_staged` for `relay::Effects::drop_staged`. The producer's half is `publish_with_outbox(configured,
   effects)`: a stage that fails degrades to a plain publish, a publish that fails answers `Unpublished` with whether
   the event stays staged, and a drop that fails is logged and leaves a copy for the relay.
3. **Zero trust.** The only store identity is the projected token, read from its file at every exchange and kept
   nowhere; no key is read from the environment, and a key pair beside the token is refused at start. The exchange is
   unsigned and goes only to the STS the settings name (`<PREFIX>_S3_STS_ENDPOINT`, required, never defaulted to AWS's
   public one). Every client follows no redirect and uses no proxy, so a token or a signed request reaches only the
   declared address; a private store is trusted through `RASK_S3_CA_BUNDLE` alone. A session lasts 900 to 43,200 s and
   is renewed at half. The secret key and session token are held in a `KeyPair` whose `Debug` shows the key id alone,
   and leave only inside the signature and the `x-amz-security-token` header, marked sensitive. No message quotes the
   token, the token file's path, a credential or a transport error's URL.
4. **The dependency is two small crates, chosen by measurement** (2026-11-17, a 4-core, 16 GB machine, cold debug
   builds of a scratch crate with reqwest 0.13 and tokio as the workspace configures them):

   | Choice | New crates in `rust/Cargo.lock` | Cold build | Lines to write |
   | --- | --- | --- | --- |
   | SigV4 by hand over reqwest, with `hmac` 0.12.1 and `quick-xml` 0.38.4 (taken) | 2 | 25 s | 78 (`sigv4`) + 158 (`s3`) |
   | `object_store` 0.14.2 (`aws`), on reqwest 0.13 | 21 (async-trait, futures-io/macro, h2, humantime, itertools, md-5, parking_lot and its three, quick-xml 0.41, serde_urlencoded, spin, wasm-streams, crc-fast, …) | 33 s (+237 MB target) | ~60, plus a `CredentialProvider` |
   | `object_store` 0.13.2, the one lance 13 pins | 21, a second reqwest (0.12) among them | not built | as above |
   | `aws-config` 1 + `aws-sdk-s3` 1 + `aws-sdk-sts` 1 | 45, a second TLS stack among them (rustls 0.21, hyper 0.14) | not built | ~40, and a web-identity provider that reads its token path from `AWS_*` |

   `chrono` (std only) is already in the lock through the Arrow crates. `hmac` 0.13 is in the lock too (through
   jsonwebtoken) but needs sha2 0.11 where the workspace uses 0.10, so 0.12.1 is a second version of a 200-line crate.
   The hand-written signer is checked against botocore's on seven requests and verified by the real store on every call
   of the black-box test.
5. **A conditional create is in the client**: `put(.., only_if_absent: true)` sends `If-None-Match: *` and answers
   `StorageError::Exists` on the store's 412. The outbox's own stage is an unconditional put, as in Python (staging an
   event again writes the same object); the conditional create is what the relay tick's window claim will call
   (`lakehouse.timers` through `records.create_json`), and the black-box test exercises it on the real store.

## How parity was shown

- **Vectors through the real Python** (`scripts/_port_outbox_vectors.py` → `rust/vectors/outbox.json`, 99 cases in
  nine groups; the generator writes the same file on every run): 12 object keys (the real `stage_event` and
  `drop_event` through a recording client, six outbox URIs), 6 request URLs (the real boto3 client's path encoding), 7
  listings (the real `list_events` and `backlog` over a stand-in filesystem: order, ties, the limit, what is no staged
  object), 16 publishes (the real `publish_with_outbox` with each of stage, publish and drop succeeding or failing,
  configured or not: the effects in order, what it raised and `left_staged`, and what it counted), 24 refusals (the
  real `s3_errors` and `create_json` over twelve status-and-code pairs, with and without a key), 7 signatures
  (botocore's `S3SigV4Auth` at a fixed time: a put, a conditional put, a get with an encoded key, a delete in another
  region over TLS, two listings with encoded query values, and an endpoint with a path signing with no session
  token), 13 settings environments (the real `LineageSettings`), 6 exchanges (the real `exchange` through the real
  boto3 STS client, its request recorded and answered by a script: a session, a token file's whitespace, two refusals,
  an empty and a missing token file), and 8 session scenarios of 48 steps and 38 STS requests (the real `StoreSession`
  on an injected clock against scripted STS answers: held to a minute before its end, renewed at half, a rotated token
  read again, a failed renewal
  kept then said expired once, a missing token file, a boot waiting out a store still provisioning, a boot refused
  twenty times, the longest session). `tests/outbox_vectors.rs` asserts the same answers; the exchange and session
  scenarios run through a local STS that gives the recorded answers in order and records each request's form, which
  must equal Python's.
- **Against a real store** (`tests/outbox_rustfs.rs`, run by `scripts/port_outbox_check.sh`): RustFS 1.0.3, the
  version the fixture runs, its linux and macOS binaries pinned in `scripts/toolchain.sha256` from the release's own
  `SHA256SUMS` and fetched by `scripts/rustfs_server.sh`; a root pair made for the run and read from files; a stand-in
  issuer whose discovery and keys the store reads (`scripts/port_outbox_fixture.py issuer`); the bucket and two canned
  policies made by the provisioning hook's own tool (`scripts/rustfs_admin.py`): the producer's (put on the outbox) and
  the relay's, copied from `lance.objectStorePolicy.lineage`. Five tests: the producer stages, the relay lists oldest
  first, reads bounded, drops (twice) and finds nothing; the store refuses the relay a stage and another key, and the
  producer a listing; a conditional create is refused 412 once the key exists and the loser wrote nothing; a listing of
  1,005 objects is read across its pages; and a session is refused a forged token while the held one still signs, is
  renewed from the token read again, and signs nothing past its end.
- **Python and Rust on one outbox** (`scripts/port_outbox_fixture.py cross`): each exchanges its own token at the same
  STS; three events the Python stages are listed by the Rust exactly as the Python lists them and dropped by it; three
  the Rust stages are listed, read byte for byte and dropped by the Python.
- **Ten planted defects each failed a test**: the query left unsorted in the signature (signature vectors); renewal at
  the session's end (session vectors); a session handed out to its last second (session vectors); newest first (listing
  vectors); a failed stage failing the publish (publish vectors); a 412 read as a refusal (refusal vectors); the listing
  stopped at its first page (the 1,005-object test); a staged event that vanished failing the read (the stage-and-drain
  test); the session token left out of the signed headers (every real-store test); and the token file read once and
  cached (exchange and session vectors).

## What differs from the Python, on purpose

Each is outside the corpus:

- **The outbox is on the object store only.** A `file://` or local outbox (Python's development and unit-test path
  through pyarrow) is refused at construction.
- **No static key.** Python signs with the environment's pair where no token is projected (the offline and Dagger
  lanes, under `RASK_INSECURE_ALLOW_STATIC_STORE_KEY`); the Rust refuses to start without a token. An empty
  `<PREFIX>_S3_STS_ENDPOINT` is refused where Python took it.
- **The payload is signed.** botocore's S3 client sends `UNSIGNED-PAYLOAD` (measured on botocore 1.43.58); the Rust
  signs the body's SHA-256, as botocore's own signer does given the request (the signature vectors).
- **A staged object is handed over as bytes.** Python decodes each as UTF-8 inside the listing, so one object that is
  not UTF-8 fails the whole listing and every tick after it; the Rust hands it to `CloudEvent::parse`, which refuses it
  as poison, and the relay drops it.
- **A retry waits without jitter, and STS's exchange is made once.** An S3 request the store answers 429, 500, 502,
  503 or 504, or does not answer, is made again, three attempts in all, as botocore's adaptive mode makes it, waiting
  0.2 s and then 0.4 s where botocore draws its waits at random; a timer's conditional write is made again alike. The
  STS exchange, which botocore's standard mode also retries, is made once, and the next pass or boot attempt is its
  retry. A 404 on a delete is taken as done, as `drop_event`'s contract says; S3 and RustFS answer 204 there.
- **The endpoint is `<PREFIX>_S3_ENDPOINT`, then `RASK_S3_ENDPOINT_URL`, then the region's own.** The `S3_*` and
  `HCP_*` aliases are not read, nor `RASK_S3_INSECURE`: a store's certificate is always checked.
- **A session is held from STS's answer whole or not at all.** Python required `Expiration` and read it for its log
  line; the Rust takes the key id, secret and token and counts the session's end on its own clock, as Python does.
- **What Python counts is answered as a value.** `publish_with_outbox` answers `Published` (how it was staged, whether
  the copy was dropped) or `Unpublished`; the metrics that count them are not ported yet.

## The four facts

- **GitOps:** the store, STS, token path, role, region and session length come from the chart's
  `lance.objectStoreSessionEnv`; the identities' policies are the chart's own, provisioned by its hook.
- **Cloud native:** the store and its STS are the platform's, named by address; the projected token is the workload's
  own identity, and the session is renewed by the process itself.
- **Lance-idiomatic:** the outbox is not a table; it puts nothing in a Lance namespace and reads none, and the store's
  conditional create is the primitive Lance's own commits stand on.
- **Zero trust:** no key in the environment, the token read at each exchange, a session of at most twelve hours renewed
  at half and refused a minute before its end, every request signed, the store refusing what each identity's policy
  does not grant (shown on the real store), and no credential in a log line or an error.

## Not ported yet

- **The relay tick** and its window claim (`lakehouse.timers`), which call this feature; nothing wires them.
- **The outbox's metrics** (`lakehouse.outbox_metrics`), the readiness of a store session, and the boot of one in a service.
- **`resolve_event`** (the DLQ replay's lookup by run id) and the local-filesystem outbox.
- **The records' read-modify-write** (`If-Match`) and the rest of `lakehouse.storage` (`iter_keys`, retention pruning).

## Consequences

- `lakehouse-core` holds 749 counted lines of this feature (outbox 145, storage: session 295, s3 158, sigv4 78, mod 71;
  two in `lib.rs`), 4,723 of its 11,234 budget.
- A change to the stage, drop or listing rules, the error mapping, the signer, the settings, the exchange or the
  session's timing changes a vector, and `make rust-check` fails until the Rust agrees.
