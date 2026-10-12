# 0156. The lakehouse audit's grilling amends ADR 0155: a tag gate, provenance in the spec Context, and one tier judge (2026-10-12)

**Status:** accepted. Source: owner, 2026-10-12, three grilling rounds over a read-only audit (lance_docs format and
namespace spec read in full, Lakekeeper as reference architecture, 459 open issues, tests and docs). The owner's
standing facts judged every option: zero trust, cloud native, Lance-idiomatic. Amends ADR 0155.

## Context

The audit found that parts of ADR 0155 rest on operations or channels that do not exist. Lance has no branch merge or
fast-forward (the spec's branch operations are List, Create and Delete; measured on pylance 12, copying a branch's
fragments onto main fails or leaves it unreadable). The spec has no field for Lance transaction properties, and the
catalog stamps none. `declare_table` never vends, `DescribeTable` vends only the read tier, and the spec's own governed
commit (`CreateTableVersion` with `managed_versioning`, the external manifest store lance_docs describe as the hook for
commit validation) is refused 406. The maintain tier vends Put/Delete over `_versions/` to any owner. The catalog
itself still compacts and rebuilds. The tier is guessed from a namespace name. The `/events` feed pages only backwards.

## Decision

- **C1, the promotion gate is a tag gate.** Every write lands on main; readers of a governed tier read the version
  tagged `published`; approval moves the tag (`UpdateTableTag`), rejection restores main to the tagged version
  (`RestoreTable`). This replaces ADR 0155's "fast-forward to the branch"; branches serve review reads only. Moving
  `published` notifies the project's watchers.
- **C2, provenance travels in the spec `Context`** (`header.*` request headers) on every governed write door: run id,
  job and inputs (table, branch, version). The catalog alone stamps them as reserved `rask.*` transaction properties,
  requires `can_read_data` on each named input, and emits the lineage edge, which is the durable record. Operations
  the SDK cannot stamp are refused on silver and gold unless a catalog door builds the transaction itself. Every ad hoc
  channel (`source`/`source_version`, `X-Lance-Run-Facets`, the `/commit` `run_id`) is replaced.
- **C3, bring-your-own commits.** The spec doors vend the tier FGA grants the caller. Measure whether stock clients
  commit through `CreateTableVersion` with `managed_versioning`; if they do, serve it (the catalog judges each staged
  manifest and does the put-if-not-exists itself, the CAS stays in the object store) and delete the bespoke `/commit`
  door; otherwise generalise `/commit`.
- **C4, the maintain tier** is vended to the maintenance service identity only (expiry and purge). A bring-your-own
  rewrite job gets the write tier over `data/` and `_indices/<uuid>/` and commits through the catalog's compaction and
  index commit doors, which judge the result.
- **C5, authentication.** Any token verified against the cluster's service-account issuer is a subject
  (`kubernetes~system:serviceaccount:<ns>:<sa>`); FGA alone authorizes. Dex client credentials follow for writers outside
  Kubernetes.
- **C6, rewrites in the catalog.** `CreateTableIndex`, a spec operation, stays as a bounded asynchronous build in the
  catalog; erasure stays as a named exception; `compact_now`, `rebuild_index_now` and the write-path index build leave
  for a bring-your-own job using `plan_compaction` / `commit_compaction`. ADR 0155's "Lance treats compaction as engine
  work" is withdrawn: this is the owner's decision, not a Lance property.
- **C7, the commit contract.** Lineage's `GET /events` gains a forward cursor (`since=<seq>`, oldest first, filterable by
  table or namespace) and a documented commit-event shape; the catalog's lineage emission is mandatory on governed tiers.
- **C8, one tier judge.** A table's tier is the reserved namespace property `rask.tier`, set only by a project admin.
  Every write door calls one `tier_policy.judge(op, table, caller, run)`; a contract test enumerates every write route.
- **C9, row columns.** `stage` and `lineage` are no longer required on governed rows (owner ruling D1 superseded);
  `source_rowid` is an opt-in a pipeline may write.
- **The Python gateway retires**: the cluster's Ingress / Gateway API routes each `/api/*` prefix straight to its
  service, each of which authenticates every request itself.
- rask-only routes under the spec's `/v1` move to `/management/v1`.
- Counted Phase 1 enablers: LH-225, LH-229, LH-063 (implied by C2, C3, C5), LH-215 (a rename keeps governance),
  XC-082 (secrets rotate in running pods), CTL-31 (the control bus admits only signed writers).

## Consequences

- LH-464 waits for C2 and C8 before adding its write-path rules; LH-466 is rewritten against the tag gate.
- The Lance-family upgrade (pylance 13, lance-namespace 0.13, lancedb 0.40) is researched before C3's measurement is
  taken, so the measurement is made on the versions that ship.
