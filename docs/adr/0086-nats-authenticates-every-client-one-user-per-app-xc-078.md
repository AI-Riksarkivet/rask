# 0086. NATS authenticates every client, one user per app (XC-078 slices 2 and 3, owner 2026-10-04/05)

Dapr 1.18.1's pubsub.jetstream authenticates only with a user JWT plus seed, a TLS file pair or one shared token, so
per-app rights need operator mode: an operator, a system account, one application account with JetStream, and one user
per Dapr app-id (plus `admin` for the stream Job, a read-only `monitor` for nats-box and `ingest` for its raw client),
with a MEMORY resolver so issuing or rotating a user never touches the server. Each user's rights are its row of
`nats.auth.users`, measured rather than derived: an operator-mode broker lane (`dagger call nats-auth`, a real
nats-server and one real daprd 1.18.1 per component shape, judged by the broker's log) refused the table derived from the
research in 35 places before it was right. Acks are the v1 layout the server sends and scoped per durable; a grant only a
flag's component uses sits under `nats.auth.flagged` and exists only while the flag is on, so with every flag at its
default no user holds a grant no call site uses. Credentials are referenced, never carried: every pub/sub Component names
its app's `nats-user-<app>` through `lance-secrets`, every app's Dapr Configuration denies every `nats-user-*` (ingest
reads only its own), and the Job and nats-box take theirs as ExternalSecret files. In production the platform runs NATS
and provisions the users (R1), attested by `nats.auth.provisioned`; the dev OpenBao mints the trust root and carries it
over a surge, and a total loss re-mints it, which the server refuses until its pods restart (durability is not a goal, R1).
Routes authorize with a credential delivered by ESO (R2).

A reload cannot switch the server's mode and a pod restarted alone into operator mode cannot join the others, so the
switch-over scales NATS to zero and lets the upgrade restore all three pods together; the streams start empty (test
data). Ingest's client signs the server nonce with lineage-kit's SigningKey over its own user (no new dependency).
The review round fixed the over-grant (flag-gated grants) and put a client restart after the store holds the users, so no
sidecar boots with an empty credential. Residuals named: `_INBOX.>` is shared within the account; the catalog's ack on
CATALOG_CONTROL is stream-wide (its broadcast consumer's name is the server's; a compromised catalog already signs every
control event, and `ackPolicy: none` does not stop daprd 1.18.1 acking, measured); port 8222 is unauthenticated by design;
OpenBao's dev root token reads the store (XC-079).

Deployed in two helm revisions and read back live. Rev D (269, credentials everywhere, the server still open): all seven
signing pairs carried over the OpenBao surge, the NATS root minted with 11 users, every component wired to its own user,
no sidecar secret error after the restart, every foreign credential refused at the secret API, and the cascade, a grant
notification, a publication and an ingest run unchanged; it held overnight. Rev E (270, 2026-10-05, owner-approved): NATS
scaled to zero, back in operator mode with route authorization, the stream Job recreated the nine streams as `admin`, every
connection authenticated and none anonymous; a credential-less publish and another app's credential were refused on every
cascade, control, lineage, ingest and maintenance subject with no stream moving; the cascade reached gold, a person was
told of a grant, and an ingest run drained eleven units through ingest's own user (seen in /connz?state=closed). Two
readback checks are corrected beside their as-written verdicts: the readback could no longer read stream messages through
nats-box, which now runs as `monitor` (the path was taken from the stage runners' logs instead), and the one DLQ entry
after the flip is the readback's own zero-byte prefix marker, parked by ingest. Slice 1 (the doors) is the entry above.
