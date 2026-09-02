# Verdict: continue the Lance-only lakehouse, DIY over Lakekeeper, and what is missing

Date: 2026-09-02. This closes the analysis series (`dapr-coupling-analysis.md`, `lakehouse-analysis.md`, `catalog-build-vs-buy.md`, `lance-conformance-and-build-rules.md`). It folds in the five-post digest you supplied, checked against pylance 10.0.0 and rask's code where the posts make a claim load-bearing.

## 1. The verdict in five sentences

Continue, and keep the catalog DIY. The five posts make the case stronger, not weaker: everything Lance is building (multi-base, branches tracked by root, blob v2 with four storage semantics, late materialization) lives **at the format layer**, and it only becomes governed if the catalog understands the format. No off-the-shelf catalog does: Lakekeeper's generic tables, Gravitino's 15-of-54 Lance REST, Unity and every `lance-namespace-impls` backend return `managed_versioning=false` and know nothing about bases, branches or blob lifecycle. That is the whole justification for DIY, and it is conditional: **a DIY catalog that only implements the registry floor is worse than Lakekeeper**, because Lakekeeper does the floor better. rask earns its existence by doing the format-aware governance nobody else does, and today it does about a third of it.

## 2. What is unique, stated as a product

rask is the only thing in the field that can plausibly become **a governed Lance lakehouse**: OpenFGA on every object, OpenLineage on every write, a modality-agnostic bronze→silver→gold, and a REST surface a stock Lance client can use. Concretely unique, none of which any candidate catalog has:

| Capability | Why it needs a format-aware catalog | rask today |
|---|---|---|
| **Governed commits** through the spec's managed-versioning ops (CreateTableVersion / BatchCommitTables) with FGA, protection, gate, lineage and replay marker on the commit itself | the external manifest store is a Lance-specific protocol | routes mounted and FGA-gated; no lineage/gate; `managed_versioning` never advertised; real door is non-spec `/commit` |
| **Base-aware credential vending**: vend per registered base, read on the clone's source base, write on `target_bases`, never on an archival base | the posts say vending "iterates the base list, not the file list"; a catalog that vends one prefix per table cannot serve a multi-base or cloned table | refuses to vend when any fragment carries a `base_id` (credentials.py:76-130, feature-flagged); no per-base grants |
| **Branch-level governance**: branches live under `tree/<branch>/`, so read-only main / write-only branch is a storage ACL and a vended prefix, plus per-branch audit and cost | tracked-by-root branches are Lance's departure from Iceberg; the governance win only exists if the catalog scopes ACLs, vending, protection and lineage to the branch prefix | `can_create_branch` on the table (owner only); update/delete ignore `branch`; vending, protection, trash and lineage are branch-blind |
| **Cross-dataset GC pins**: a shallow clone or branch inherits fragments by base; the source "can be garbage collected independently", which strands the clone | Lance's cleanup has no knowledge of clones; only a catalog that records the clone → (source, version) edge can pin the source version (tag) and refuse purge | tags-as-GC-pins exist for gates and publish; no clone/branch edge, no cross-dataset pin |
| **Blob lifecycle policy per base**: managed sidecars are reachability-GC'd; the blob post says the same GC reaches **external blobs under registered bases**; archival buckets must be reference-only | one identity running cleanup with delete on an archival base is a data-loss path | ingest picks reference vs managed implicitly; no per-base policy; cleanup identity holds the estate's root credentials |
| **Lineage that includes branch parentage and clone provenance** as OpenLineage facets | the branching post's "auto-traceability" is a structural fact the catalog must surface, not reconstruct | lineage on writes; no branch/clone facets |
| **Modality-agnostic tiers** with the blob descriptor as the late-materialization contract | the Spark post's whole point | tier contract is opaque payload; the cascade re-materializes managed blob bytes per tier |

Nothing in that table is a registry feature. It is the difference between "a catalog that lists Lance tables" and "a catalog that governs Lance". Lakekeeper is the best implementation of the first; rask is the only attempt at the second.

## 3. Does DIY still make sense over Lakekeeper

Yes, with the condition above. Restating the build-vs-buy case with what the posts add:

- **Lakekeeper** gives generic tables, OpenFGA, vending, soft-delete, protection, task queues, statistics. It does not do commits, versions, branches, bases or blob lifecycle for Lance, and its vending is per-table-prefix, which the multi-base post explicitly calls the model that "doesn't scale". Adopting it means building all of §2 as a sidecar service anyway, with two catalogs of record. Keep it as the design reference for the registry floor (idempotency records, task queues with heartbeats, per-warehouse storage profiles), which is what `catalog-build-vs-buy.md` already recommends.
- **Gravitino** ships a Lance REST facade with `managed_versioning=false` hard-coded and a two-level namespace limit. Its Filesets are the "governance layer over external references" the blob post argues against. Not a candidate.
- **Unity, DuckLake**: unchanged, disqualified.
- **lance-git / Nessie / lakeFS**: the branching post makes the layering argument for you. Versioning belongs in the format; the catalog governs it. Do not build catalog-level versioning; expose format branches and tags, governed.

The honest risk on the DIY side is not the architecture. It is capacity: §2 is a lot of format-specific engineering, the spec moves (three releases since the vendored copy), pylance's own bundled client and reference server disagree with the spec on two routes, and rask currently has 12 of 54 ops verbatim and no test with a stock Rust-backed client. If the team cannot commit to the format-aware roadmap in §5, the rational alternative is Lakekeeper for the registry plus a thin rask "governed commit and blob" service, and to accept two catalogs. I would not pick that, but it is the only other coherent option.

## 4. Missing features, ranked by what unblocks what

Beyond the eight spec blockers (B1–B8) and the two 0.12.0 ones (B9–B10) already listed in `lance-conformance-and-build-rules.md`:

1. **Per-base credential vending.** Vend the union of a table's `base_paths` with per-base rights (read for inherited/source bases, write for `target_bases`, never for archival bases). This unblocks multi-base tables, shallow clones, branches and external blobs at once. Today rask refuses instead of vending.
2. **Branch-scoped everything.** Honour `branch` on update/delete/insert/merge (the plumbing exists at `dataplane.py:1085`); scope vending to `tree/<branch>/` for branch writers; add a `branch` rung to the FGA model so a project member can write a branch without owning the table; protection and trash per branch; lineage facets for `parent_branch`/`parent_version`. Note the format has **no merge primitive** (pylance 10.0.0 has `create_branch`, `checkout_version`, `shallow_clone`, `add_bases`, no `merge_branch`), so write-audit-publish is branch → gate → *publish tag or copy*, never a fast-forward. Design the promotion flow on that fact.
3. **Cross-dataset GC pins.** Record clone/branch → (source, version) edges in the lineage graph; pin the source version with a tag while any clone references it; make the sweep and purge consult those pins. Lakekeeper has nothing here either; it is a genuine rask feature.
4. **External-base GC policy and cleanup identity.** Per base: `managed` (Lance may reclaim) or `reference-only` (never delete). Run cleanup with credentials that lack delete on reference-only bases. Before relying on the post's claim that Lance GC reaches external in-base blobs, pin it with a RED test against pylance 10.0.0; the installed `cleanup_old_versions` docstring does not mention it.
5. **Storage profiles as bases.** A warehouse in another bucket, account, region or a hot tier (Lustre, Weka) is a `DatasetBasePath` plus `base_<id>.<key>` storage options; `target_bases` is how writes are steered; selective failover is an edit of `base_paths`. This replaces the "storage profile per warehouse" item in `lakehouse-analysis.md` §11 E with the format's own vocabulary.
6. **Tiers as shallow clones plus columns, not copies.** The cascade already forwards *external* descriptors without re-persisting bytes, but re-materializes *managed* blobs per tier (`compute.py` blob path). A silver tier that is a shallow clone of bronze at version N with derived columns added (`add_columns`, which rask already uses in place) stores no blob bytes twice and gets provenance structurally. Requires item 3 first. Measure before committing; the gain is proportional to managed-blob volume.
7. **Descriptor-first reads.** Return the blob v2 descriptor struct on Arrow query responses by default, bytes only on `blob_handling="all_binary"`; that is the Spark connector's contract and what a BYO engine expects.
8. **In-flight blob-byte admission budget** on every blob door (503 + `Retry-After`), from the lance-context spec. rask streams but never bounds.
9. **Repack in the sweep.** Blob v2 packed sidecars accumulate holes; the post names repack as the consolidation that avoids data-file rewrites. The sweep does compaction and index optimize only.
10. **Runtime hygiene** already listed: shared `lance.Session`, conflict classification on every mutating door, `LANCE_CPU_THREADS`/`LANCE_IO_THREADS`/`LANCE_LOG` under Ray, unenforced primary key on the ingest id, name validation at the door, feature flags 32/64/128.

Not missing, and worth saying so: the create-time decisions are right (2.2, stable row ids, blob v2 with pinned thresholds, conditional-PUT commits, per-worker fragments with one commit, run-id replay marker); the medallion's opaque payload contract is the modality-agnostic shape the posts argue for; the annotator-as-client decision holds; the BYO-engine event plane and the Dapr retreat plan stand as written.

## 5. What to do, in order

1. **Make spec-verbatim true** (B1–B10) behind one test that drives the catalog with `lance.namespace.RestNamespace`, lancedb `namespace_client_impl="rest"` and lance-ray in namespace mode. Re-vendor `lance_docs` from lance-namespace v0.12.0 and lance main. File the upstream issue on pylance's GET routes.
2. **Move the governed commit onto the spec path** (advertise `managed_versioning`, lineage/gate/marker on CreateTableVersion, retire `/commit`), and carve the management API for everything in `lance-conformance-and-build-rules.md` §4.
3. **Per-base vending and branch scoping** (items 1–2 above). These are what turn multi-base, branches and blob v2 from format features into governed ones.
4. **Cross-dataset pins and external-base policy** (items 3–4), each pinned by a RED test.
5. **Then the Dapr retreat** per `dapr-coupling-analysis.md`: OpenBao for secrets, JetStream for events, no Dapr Workflow, BYO engines on the event plane.
6. Storage-profile-as-bases, tiers-as-clones, descriptor-first reads, blob budget, repack, hygiene.

## 6. Corrections the digest forced

- The blob thresholds in the posts are **64 KB / 4 MB**, matching what rask measured on pylance 10.0.0 and pinned; the current `guide/blob.md` still prints 16 KiB / 2 MiB. Trust the binary and the post; keep pinning per column.
- `blob_id` doubles as the base id for External blobs (`0` = absolute URI). External URIs outside registered bases are rejected by default; opting in transfers lifecycle responsibility to the user. rask's allowlist posture is exactly the default.
- The three marketing numbers in the link previews ("42% lower storage", "50%+", "68x faster") are not in the post bodies. Do not cite them.
