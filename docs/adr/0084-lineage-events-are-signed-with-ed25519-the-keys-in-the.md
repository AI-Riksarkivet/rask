# 0084. Lineage events are signed with Ed25519, the keys in the store (LH-064, owner 2026-10-02)

This supersedes the 2026-09-24 entry above that accepted symmetric event signatures, and LH-064's earlier OpenBao
Transit wording. XC-078 makes the cascade heads and notifications verify events too, and an HMAC verifier holds the
very key that forges what it verifies, so each new verifier would have been a new forger. Transit keeps the private key
inside OpenBao, but it needs a per-service OpenBao login, an HTTP client and a durable store that no row owns, and the
dev OpenBao is in memory. Keys in the store reuse the delivery path that works live, through each sidecar's secret API,
and the seed's carry-over across rollouts. Verifiers read public keys only.

Two scope rulings came with it. Only the signature facet takes the `rask_` prefix now (`rask_signature`), because its
shape changes anyway and an unknown facet is order-independent during a roll. The author, lance and model facets keep
their keys until a separate flag day (LH-360). The OpenLineage spec requires the prefix of every custom facet, but
renaming the facets a cascade head reads, across separately tagged images, makes it skip triggers mid-roll. Key custody
(C6) is the Dapr-scope claim: no app's sidecar can read another identity's private key, and lineage reads none. A pod
holding OpenBao's root token, or reaching :8200 directly, still reads the store until XC-079.

Deployed in three helm revisions and read back live on 2026-10-04. Rev A (263) minted the seven pairs under the old
images: each of the 13 lance-secrets sidecars reads exactly its own signing key, and every kid survived an OpenBao
restart through the seed's carry-over. Rev B (264) rolled the signing images with enforcement off: seven canary events,
one per hop of a cascade to gold plus the catalog's, verified offline from the raw bytes in the LINEAGE stream, among
them an integral float of 1e16 that crossed a real daprd, so the sidecar keeps a number's text and canon-1's float rule
stands. Rev C (265) deleted the service-token-* family and turned enforcement on. A forged unsigned event published from
the notifications sidecar was refused within seconds, acknowledged and never recorded. Every signer had a verified event
recorded under enforcement, ingest's through its staged outbox path while lineage was scaled to zero (owner-approved).
service-maintenance's key was rotated live (owner-approved): an event signed with the previous key was recorded, and so
was the next one under the new kid. A lineage restart left the DLQ unchanged.

Kept from the readback. A Dapr Configuration is named by its content (`lance-config-<app>-<hash>`): Helm applies custom
resources after every Deployment, so a pod rolled by a checksum could boot against the previous deny list, while a pod
naming a Configuration Helm has not applied yet fails to start until it exists. A rotation takes
`bao kv metadata delete`: after a soft `bao kv delete` the CLI prints "No data found", which the seed refuses as an
unreadable store, and the replacement OpenBao never becomes Ready. From Rev A until Rev C the catalog's DROP events had
no admitting signature (the rev-262 catalog signed under an identity env commit 1 no longer renders, and the contract
kept the shortcut off at Rev B); no table was dropped in that window: the LINEAGE stream holds only canary events and
two table creates between Rev A and Rev C (seq 16188 to 16197).
