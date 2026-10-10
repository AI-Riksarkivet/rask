# 0089. A warehouse in another object store is refused until its credential is consumed (LH-205, 2026-10-05)

The catalog signs every connection with the estate's one key pair. A warehouse record could name its own
`endpoint` (LH-067) and the opens followed it, but no request sets `credential_ref` and nothing reads it, so
an open under such a record signed toward a host a project admin chose with the estate's access-key id, its
bucket was provisioned on the estate's store, and its vend ran against the estate's STS.

"Resolvable" is what the doors that sign would consume, and none consumes a warehouse reference today:
`warehouse_credentials.resolve` can fetch a bundle from the Dapr secret store, but only the multibase write
path uses it. So every endpoint other than the estate's is refused at create (400, `InvalidInput`), and an
existing record naming one is refused (406, `UnsupportedOperation`) at the connection builder, which every
open, vend, cascade, undrop and unbind reaches, and at the provision re-POST, the purge and the scope probe.
`Settings.namespace_properties` no longer takes an endpoint, so no code path can pair the estate's key with
another store. "The estate's endpoint" is `LANCE_S3_ENDPOINT` compared after normalisation
(`catalog.core.store_endpoint`): scheme and host case, a default port and a trailing slash or dot are
spelling, while userinfo, a query, a fragment or a bad port make it another store. A plain delete touches no
store and stays open, and a re-POST with `"endpoint": ""` returns a record to the estate's store. Lakekeeper
makes the same call at the same door: it validates a warehouse's storage credential at create
(`crates/lakekeeper/src/api/management/v1/warehouse/mod.rs:97-110`). Consuming a reference end to end
(per-base `base_store_params` or `base_<id>.<key>`, `lance_docs/guide.md:2354-2377`) is the work that lifts the refusal.
