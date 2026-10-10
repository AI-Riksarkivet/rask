# 0066. Four rulings on the lakehouse's unblocked rows (owner, 2026-09-19)

Four rows had stood blocked on an owner call, each with its evidence complete and re-measured
immediately before the question. All four were answered in one sitting; they are recorded together
because that is how they were decided, not because they interact.

### The 32 residual orphan files are reclaimed by a NO-OP COMMIT, not by rask deleting bytes ([[LH-094]], [[LH-102]])

**Decision.** Write one empty commit to each of the two frozen datasets. That raises the dataset's
listing floor above the re-uploaded object mtimes, so Lance's own `cleanup_old_versions` enumerates and
deletes the residue on the next ordinary sweep. rask does not delete those bytes itself.

**Why the residue cannot clear itself.** `cleanup_old_versions` clamps its unreferenced-file listing to
the commit timestamp of the EARLIEST RETAINED MANIFEST (`rust/lance/src/dataset/cleanup.rs:332-341`,
applied to `_versions/`, `_transactions/`, `data/` and `_deletions/` at `:721-731`) BEFORE the 7-day
unverified rule at `:345` filters what was listed. `older_than` never enters that cutoff. Re-measured
against pylance 11.0.0 with `delete_unverified=True` bypassing the age filter: the flip is at the
manifest timestamp to the second — -3600/-60/-1/0 s REMOVED, +1/+60/+3600 s KEPT. A dataset collapsed to
one live version therefore holds every file it wrote after that surviving commit permanently.

**Why these two datasets are above their floor.** The MinIO PVCs were created `2026-09-11T10:50:09Z`
while `m2proof_silver$m2-proof-1788537252` embeds epoch `2026-09-04T15:54:12Z` — the objects were
re-uploaded a week after the commits that reference them, so every mtime is newer than every manifest's
commit timestamp. This is not specific to the incident: any restore, re-upload or lifecycle rehydrate
rewrites object mtimes and reproduces it.

**Why not a one-off reclaimer.** Both datasets are LIVE (80 and 300 rows, one live version each), so the
32 files are superseded-version residue INSIDE governed tables rather than junk. Deleting them directly
means rask deleting bytes on a live estate against a floor rule it derived itself, and the asymmetry
`base_refs` already states applies: a wrong refusal costs disk, a wrong permit costs data. The no-op
commit keeps the deletion inside the format's own reclaimer, where the rule is upstream's.

**The cost, accepted explicitly.** Each frozen dataset gains a version representing no data change,
written for a GC side effect. That is the trade: a phantom commit in the history against rask never
holding the delete.

### An unset app token REFUSES at `authorize_produce`, like every sibling door ([[LH-175]])

**Decision.** `authorize_produce` treats an unset app token as a refusal. `RASK_ALLOW_UNAUTHENTICATED_DAPR`
stays the one sanctioned hatch for a deployment that means to run open.

**Rationale.** One rule across every door is the point: `require_dapr_token` already states it as "the
door cannot authenticate anybody, so it admits nobody", and a single door that opens instead is an
asymmetry nobody can hold in their head. An operator wanting an open deployment now says so with a flag
that is explicit and greppable, rather than by leaving a setting empty. The accessor half of this row
was a live authorization hole — the gate read `settings.app_api_token` (`''` here) instead of
`expected_app_token()`, so `/stage-runners` and `/cascade/stalled` answered 200 with real data to a
caller with no credential — which is why the policy half is being closed rather than left as a note.

### Vending's off-cluster question is a SEPARATE row from the conformance gap ([[LH-020]])

**Decision.** Run the stock-client conformance suite from INSIDE the cluster, where the vended endpoint
resolves, so lancedb and lance-ray become real passes. Whether vending is supposed to serve off-cluster
clients at all is filed as its own row.

**Rationale.** The catalog vends its own in-cluster address (`http://rask-minio:9000`), so an external
client gets a valid 900 s credential for a host it cannot resolve. Measured from the host against
release 186: 1 passed, 2 SKIPPED, both naming that cause. The skips are honest but the row's closing
condition says PASSES, and a skip reads as green — that is a test-coverage defect and it is fixable
today. "Who is vending for" is a product decision that reaches into per-deployment external addressing
and into what an endpoint discloses about internal topology; carrying it on a coverage row is what kept
both halves unfinished.

### Reconcile becomes WRITE-CAPABLE, dry-run first ([[LH-061]])

**Decision.** The standing deferral ("No — not yet") is overturned. Maintenance gains a repair pass that
acts on the drift report at every tier, **dry-run by default with drift deletion opt-in**, plus an
**additive** tuple rebuild driven from the `_projects/`, `_warehouses/` and bindings registries. The
report/reclaim module split stays, so the read-only gate on the detection half stays enforceable.

**Rationale.** Resilience is one of the five conditions that finish the lakehouse, and the estate
currently cannot reconstruct its FGA tuple estate after a loss: the registries hold everything needed
and nothing reads them back. The additive rebuild is the half that carries that property and it only
ever writes tuples the registries already justify — it never deletes, so it cannot widen access beyond
what the control plane already recorded. Deletion is the part that can destroy, so it is opt-in and
previewed, never the default.

### Lance general compression is OPT-IN PER TABLE, and off by default ([[LH-034]])

**Decision.** The catalog's create path sets no `lance-encoding:compression` scheme. It instead honours
any `lance-encoding:*` key supplied as a create **property**, stamping it onto the schema's
variable-width fields (`catalog.services.dataplane._apply_encoding`), so a workload that knows its own
value sizes opts in per table. Fixed-width fields are deliberately left alone: `lance-encoding:bss`
engages byte-stream-split on floats only where general compression is also applied, so a blanket stamp
would change float encoding as a side effect of asking for string compression.

**Rationale.** "Compression on = smaller" is false at this estate's value sizes, measured rather than
assumed. Lance already applies FSST to variable-width data, bitpacking to ints and RLE to
low-cardinality columns; `lance-encoding:compression` adds a classical compressor *after* those, and
below ~1 KiB per value the per-block frame costs more than it saves. Measured 2026-09-19 on the governed
tier shape (`{id, payload, stage}`, 8 MiB of payload held constant while value size varies):

| value B | rows | none | lz4 | zstd | lz4 vs none | zstd vs none |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 128 | 65,536 | 3,055,712 | 4,699,767 | 3,112,063 | +53.8% | +1.8% |
| 256 | 32,768 | 3,001,060 | 5,346,747 | 5,346,627 | +78.2% | +78.2% |
| 512 | 16,384 | 2,995,490 | 3,900,282 | 3,900,226 | +30.2% | +30.2% |
| 1,024 | 8,192 | 3,090,642 | 3,019,177 | 3,019,185 | −2.3% | −2.3% |
| 2,048 | 4,096 | 3,051,730 | 2,419,690 | 2,419,698 | −20.7% | −20.7% |
| 4,096 | 2,048 | 2,943,121 | 2,124,584 | 2,124,592 | −27.8% | −27.8% |

The crossover is ~1 KiB, the worst regression is +78% at 256 B, and above 32 KiB the setting is inert
because Lance already applies general compression automatically in a full-zip context at that size
(`lance_docs/file_format.md` — Compression Configuration; measured as a +0.0% delta at 256 KiB values,
which is what confirms the harness was reading real behaviour rather than a no-op).

**The corpus this was taken at.** The five governed tier datasets measured live on 2026-09-19 hold
**121 rows** with a `payload` median of **8 B** (min 7, max 14) and `lineage` values of 1–32 KiB — the
backlog row's standing figure for the governed plane is ~50 MB, against an object-store total dominated
by `rask-observability`. Writing that real corpus under each scheme made it **+14.6% (lz4) / +14.7%
(zstd) LARGER** than the default. So a blanket scheme today would cost bytes, not save them.

**Why not decide it once for the whole estate.** The tier payload is opaque by construction —
`medallion/schemas/tier.py` fixes `{id, payload, stage, lineage, source_rowid}` and lets the transform
declare the payload's shape — so there is no value size the catalog is entitled to assume. A modality
that lands 4 KiB JSON documents wants zstd; one that lands 8 B tokens is made worse by it. Pinning a
single scheme in the create path would be the catalog deciding a workload's physical layout on evidence
it does not have. Pinned by `services/catalog/tests/test_compression_is_opt_in_per_table.py`.
