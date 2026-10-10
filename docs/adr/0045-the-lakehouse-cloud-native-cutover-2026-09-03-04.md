# 0045. The lakehouse cloud-native cutover (2026-09-03/04)

The cloud-native cutover plan asked one question: can the lakehouse stop doing unbounded work in request
handlers and stop signing writes with a root object-store key. It is closed, and the durable findings
are here because the plan doc is deleted — a finished plan left in the tree reads as outstanding work.

**The catalog exposes Lance's own distributed compaction protocol, not a hand-rolled Rewrite.** The
plan called for widening the commit door to accept `LanceOperation.Rewrite` built from
`write_fragments` output. Running it found that is not awkward but IMPOSSIBLE on this estate's tables:
Lance refuses with `All fragments must have row ids`, and every medallion table is written with stable
row ids. `Compaction.plan` → `CompactionTask.execute` → `Compaction.commit` are all `.json()`
serializable, so the split by credential is the protocol's own: plan and commit are metadata-only under
the catalog's key, execute moves every byte under a vended one.

**Maintenance discovers work by LISTING BUCKETS, and must.** Lakekeeper's catalog-directed task queue
is sound because every Iceberg commit goes through the catalog — the commit pointer lives in it. rask
deliberately does not have that (Lance puts the CAS in the object store, which is why this estate needs
no relational DB), and the medallion stage runners call `lance.write_dataset` directly, so a catalog-directed
decider would be blind to the highest-churn writer in the estate. Two supporting facts: the selection
function is whole-estate (`_protected_roots` must open every manifest in every bucket, because a shallow
clone in bucket B is the only thing that knows bucket A's dataset must not be rewritten), and datasets
carrying no policy have no record to poll. Note also that Lakekeeper performs zero compaction (no
`rewrite_data_files`, no `OPTIMIZE`) — so on the DATA-REWRITING half it is no reference and Lance's own
`Compaction.plan`/`.execute`/`.commit` is the guide. It is NOT, however, a catalog that only does cheap
work, and an earlier version of this sentence said so: its orphan-file removal "performs a full
recursive listing of the table's storage location, which can be expensive for tables with many files",
and its scheduling is adaptive rather than a timestamp comparison — the next run is timed to reclaim a
target number of bytes at the last run's observed rate, clamped to [1 day, ceiling]. Where that work
runs it answers explicitly: "we recommend running expire snapshots workers in dedicated pods to avoid
impacting REST API performance", with the API pod's worker count set to zero. See
docs/adr/0051-cascade-repair-detection-and-the-repair-verb-2026-09-04.md "Cascade repair" — checked against docs.lakekeeper.io 2026-09-04.

**A vended credential must use the `aws_`-prefixed storage-option spellings.** Every fleet pod exports
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, and with the bare spellings object_store BLENDS the two
sources and signs with a pair belonging to neither identity. Measured: identical options, identical
read — ALLOWED with no AWS_* env, `403 SignatureDoesNotMatch` with it, ALLOWED again under the prefixed
spellings with it still set. No test process has an ambient AWS_* environment, so the spelling that
fails in every pod passes every unit test. `test_vending.py` once forbade the prefix, for a real reason
(an e2e had read boto3's parameter names back out of the payload); the concern was right and the
conclusion was wrong — what matters is ONE vocabulary, and measurement chose it.

**Vending coverage is per warehouse, not estate-wide.** `warehouse:lance_catalog` is described as the
root whose grant cascades estate-wide; measured against the live store it parents 8 namespaces, among
~130 warehouses and 2810 tuples. A `can_write_data` check for the maintenance subject on a table under
another warehouse returns `allowed:false` with that grant in place. So vending hardens the granted
warehouses and falls back — correctly, and audibly per dataset — everywhere else. Closing that gap is
an authorization-model decision (a platform-subject rung, or a grant seeded at warehouse creation), not
a longer tuple list.

**The AMBIENT credential was demoted too, not just the vended path.** Vending covers the granted
warehouses; everywhere else a rewrite falls back to whatever key the service holds, so hardening only
the vended path would have left the fallback as the tenant root. `services/maintenance` now runs as a
scoped `rask-maintenance` RustFS user provisioned by `chart/templates/rustfs-scoped-users.yaml` —
which is the step that did not exist before: `values.yaml` carried `rayComputeAccessKey` with the
admission that "`scripts/` has no provisioning step yet", so the estate's one scoped-user precedent was
a knob only a hand-run `mc` session could turn, and a fresh install came up on the tenant root.

Six probes signed for real against the deployed store — the three ALLOWs matter as much as the denies,
because a credential that cannot do the service's job is an outage rather than hardening:

| Probe | scoped `rask-maintenance` | tenant root `rustfsadmin` |
| --- | --- | --- |
| LIST the warehouse (discovery) | ALLOWED | ALLOWED |
| GET a data object | ALLOWED | ALLOWED |
| PUT into a data prefix (compaction) | ALLOWED | ALLOWED |
| PUT into `_projects/` (the registry) | **AccessDenied** | ALLOWED |
| PUT into `_protection/` (the guard) | **403** | ALLOWED |
| LIST the observability store | **AccessDenied** | ALLOWED |

Maintenance can no longer rewrite the records that govern maintenance — including the protection guard
it consults before every compaction. Both halves had to move together: repointing the access key alone
gives `SignatureDoesNotMatch` on every operation, so the secret field moved to its own
`maintenance-s3-secret-key` rather than a second value on `rustfs-secret-key`, which other services
read and which overwriting would have repointed the whole estate at a maintenance-scoped credential.

**N5's premise was falsified: the sweep lock cannot simply be retired.** The row read "retire the
process-local locks and the `replicas: 1` pin once the ack is the lease". The ack IS the lease for the
EXECUTOR — `api/work.py::handle_unit` takes no lock, and redelivery is safe because compaction and GC
are convergent. It is not the lease for the PLANNER: `bindings.cron` fires on every replica with no
coordination anywhere in the path (Diagrid, verbatim: "No coordination – each replica runs the schedule
independently"), so two replicas would both plan and both enqueue the whole estate. Scaling the
executor therefore needs a SPLIT — a second Deployment with its own app-id, subscribed to the work
topic and not scoped to the cron binding — not a larger replica count. That is a pure chart change,
because Dapr component scoping decides which lanes a replica set receives. It is not urgent: the
estate is tens to hundreds of datasets, and a whole-estate tick planned and published 20 units in
1.76s.
