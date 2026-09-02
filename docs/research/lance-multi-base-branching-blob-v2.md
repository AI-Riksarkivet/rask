# Lance: multi-base layout, branching/shallow-clone, and Blob V2 — technical brief

Compiled 2026-09-02 from five sources. This is a **digest**, not a verbatim copy — every claim
below is traceable to one of the sources, and the code blocks are the API examples the sources
publish. Read the originals for prose and diagrams.

| # | Source | Author(s) | Date |
| - | ------ | --------- | ---- |
| 1 | [Rethinking Table File Paths with Uber: Lance's Multi-Base Layout](https://www.lancedb.com/blog/rethinking-table-file-paths-lance-multi-base-layout) | Jack Ye | 2026-01-20 |
| 2 | [Branching and Shallow Cloning in Lance: Towards a "Git for AI Data"](https://www.lancedb.com/blog/branching-and-shallow-clone) | Jack Ye | 2026-02-16 |
| 3 | [Lance Blob V2: Making Multimodal Data a First-Class Citizen in the Lakehouse](https://www.lancedb.com/blog/lance-blob-v2) | Xuanwo, Jack Ye | 2026-03-11 |
| 4 | [Lance Blob V2: Late Materialization for Large Binary Data in Spark](https://www.lancedb.com/blog/lance-blob-v2-late-materialization-for-large-binary-data-in-spark) | Drew Gallardo | 2026-06-17 |
| 5 | [Rollout artifact blobs: memory behavior and streaming roadmap](https://lance.org/integrations/context/specs/rollout-blob-streaming/) (lance.org spec — *not* a LanceDB blog post) | — | — |

**The through-line:** 1 → 2 → 3 → 4 is one architectural arc. Multi-base (1) is the primitive;
branching/tagging/shallow-clone (2) are built on it; Blob V2's External semantic (3) reuses it
again for blob URIs; the Spark connector (4) makes Blob V2 usable from SQL without moving bytes.
Source 5 is a different thing entirely — an operational spec for a service that *consumes* Lance
and explains why it can **not** yet use blob-v2.

---

## 1. Multi-base layout — the path model

### The problem

A table format is defined by how it *assembles* files: a top-level metadata object (Iceberg
metadata JSON, Delta log + checkpoint, Lance manifest) links every file that makes up the table.
The question "absolute or relative paths?" decides portability and operational cost at PB scale.

**Iceberg** started absolute (no ambiguity, files can live anywhere, resolution needs no context).
That broke in multi-cloud: relocating a table means rewriting every metadata/manifest file —
millions of absolute paths, often needing a distributed job. Discussion of relative paths started
2021; vendor band-aids appeared (S3 bucket aliases, access points); the community only aligned on a
**v4 relative-path design in late 2025** — paths without a URI scheme resolve against the table
location, which is either explicit or derived from the metadata JSON path, with data/metadata
locations separately configurable.

**Delta** went the other way. The Sept 2019 spec defined `path` as relative to table root → zero-rewrite
portability from day one. Aug 2021 the spec allowed absolute paths too ("a relative path … **or** an
absolute path"), which is what made **shallow clone** (Dec 2022) possible: the clone's files live
outside its own root, so they need absolute references. Delta became a hybrid — more flexible, less
portable.

### Jack Ye's three criticisms of both

1. **The false promise of flexibility.** Absolute paths let files live anywhere, but in practice they
   almost never do. Once they do, maintenance breaks: you can't safely GC a directory whose files may
   be referenced elsewhere, and credentials vending is built around a single narrow prefix — vending
   for scattered one-off absolute paths doesn't scale.
2. **"Zero rewrite, until you can't."** It holds for new, single-location, purely relative tables.
   Shallow clones, file imports, tiered storage and multi-bucket distribution all crack it. The real
   goal is **maximum portability**: relocate/restructure across any topology with the *fewest possible*
   metadata changes.
3. **Prefix reasoning is hard.** Hybrid relative+absolute means path resolution has edge cases, and a
   catalog that wants to vend credentials must discover and track distinct prefixes across hundreds of
   millions of manifest entries, with no format-level hint about what those prefixes *mean*.

### Lance's two pre-existing properties

**Predictability over flexibility** — every file lives in a spec-defined subdirectory:

```
data/            *.lance      -- column data
_versions/       *.manifest   -- one manifest per version
_transactions/   *.txn        -- commit coordination
_deletions/      *.arrow      -- deletion vectors (arrow)
                 *.bin        -- deletion vectors (bitmap)
_indices/
```

**Strict portability** — *every* path is relative, so `cp -r /local/dataset s3://bucket/dataset` just
works with no metadata update. Deliberate: AI engineers move data between local exploration, cloud
training and back on a near-daily basis; a format needing rewrites per move is a non-starter.

### The Uber requirement

Uber's AI infra team needed **horizontal bucket distribution**: one Lance dataset spread across N S3
buckets so writes round-robin (bucket-1, bucket-2, …, bucket-N) and reads fan out in parallel. At PB
scale with thousands of concurrent readers (training + agentic search) a single bucket is the
bottleneck; N buckets multiply aggregate throughput by N. Absolute paths would cost portability;
purely relative paths can't reference outside the root. They needed both.

### The design: a location **base**

Key observation: when a table has absolute paths, they share a **small number of common prefixes** —
whatever the source (file import, shallow clone, multi-bucket), the number of distinct bases stays
small even at millions of files. Make the base explicit in the format; let each file reference
`base + relative path`.

- **Controlled flexibility** — multiple locations, explicit structure; no scanning millions of paths to
  discover what locations exist.
- **Maximum portability** — relocating updates the *bases*, not the references. 10M files across 5
  buckets = change 5 strings, not 10M.
- **Composable portability** — choose which bases move together (keep the primary portable while
  external refs stay put, or migrate everything, or anything between).
- **Operational clarity** — GC and credentials vending iterate the base list, not the file list.

### Format spec (protobuf)

```protobuf
message Manifest {
  repeated BasePath base_paths = 18;
  // ...
}

message BasePath {
  uint32 id = 1;
  optional string name = 2;
  bool is_dataset_root = 3;
  string path = 4;
}

message DataFile {
  string path = 1;             // relative path within the base
  optional uint32 base_id = 7; // reference to BasePath entry
  // ...
}

message DeletionFile {
  optional uint32 base_id = 7;
  // ...
}
```

**`is_dataset_root` decides resolution:**
- `true` → the base is a Lance dataset root with the standard subdirs, so resolving a data file means
  `base + "data/" + relative_path`. Used for referencing complete datasets (shallow clone, branches).
- `false` → the base points directly at a flat file directory, no Lance subdirs. Used for raw storage
  locations (Uber's extra buckets).

**Storage efficiency:** each absolute base URI appears exactly once in `base_paths`; each file stores
only a small varint `base_id` (1 byte for up to 128 bases) instead of repeating the prefix. Manifests
stay small and cheap to load even in multi-base setups.

### Uber's layout

```
s3://bucket-1/dataset_root/      (primary dataset root; is_dataset_root: true)
├── data/
│   ├── fragment-0.lance
│   └── fragment-1.lance
├── _versions/
│   └── 1.manifest               (contains `base_paths`)
└── …

s3://bucket-2/                   (data bucket; base id: 1; is_dataset_root: false)
├── fragment-2.lance
└── fragment-3.lance

s3://bucket-3/                   (data bucket; base id: 2; is_dataset_root: false)
├── fragment-4.lance
└── fragment-5.lance
```

### Python API

```python
import lance
from lance import DatasetBasePath
import pandas as pd

data = pd.DataFrame({"id": range(1000), "value": range(1000)})

dataset = lance.write_dataset(
    data,
    "s3://bucket-1/my_dataset",
    mode="create",
    initial_bases=[
        DatasetBasePath("s3://bucket-2", name="bucket2"),
        DatasetBasePath("s3://bucket-3", name="bucket3"),
    ],
    target_bases=["bucket2"],          # write this batch to bucket2
)

more_data = pd.DataFrame({"id": range(1000, 2000), "value": range(1000, 2000)})
dataset = lance.write_dataset(
    more_data, dataset, mode="append",
    target_bases=["bucket2", "bucket3"],
)

dataset.add_bases([DatasetBasePath("s3://bucket-4", name="bucket4")])

new_data = pd.DataFrame({"id": range(2000, 3000), "value": range(2000, 3000)})
dataset = lance.write_dataset(new_data, dataset, mode="append", target_bases=["bucket4"])

# Reading is transparent
dataset = lance.dataset("s3://bucket-1/my_dataset")
print(dataset.to_table())   # all 3000 rows, from all buckets
```

`target_bases` controls where new data files go. With multiple bases and no target, **all bases are
used round-robin**. Reads automatically span every registered base.

### Use cases unlocked beyond throughput

**Hot-tiering onto high-performance AI storage** (Lustre, Weka, Nebius, CoreWeave): copy hot fragments
to the fast tier, register it as a base, training reads hot from fast / cold from cheap object storage.

```
base_paths:
[
  { id: 1, is_dataset_root: false, path: "/mnt/lustre/training-data" },
]
```

**Multi-region for localized training/serving** — one dataset spans regions natively; us-east training
reads us-east, eu-west inference reads eu-west; no cross-region transfer on the hot path.

```
base_paths:
[
  { id: 1, is_dataset_root: false, path: "s3://eu-west-bucket/dataset-data" },
  { id: 2, is_dataset_root: false, path: "s3://us-east-bucket/dataset-data" }
]
```

**Selective disaster-recovery failover** — fail over *one* base to its replica, leave the rest serving:

```
# Before
[
  { id: 1, is_dataset_root: false, path: "s3://us-east-bucket/data" },
  { id: 2, is_dataset_root: false, path: "s3://eu-west-bucket/data" }
]
# After us-east failure
[
  { id: 1, is_dataset_root: false, path: "s3://us-west-backup/data" },  // failed over
  { id: 2, is_dataset_root: false, path: "s3://eu-west-bucket/data" }   // unchanged
]
```

**Shallow clone** — the clone's manifest carries one base pointing at the source dataset:

```
# Clone at s3://experiments/test-variant references source dataset
base_paths:
[
  { id: 1, is_dataset_root: true, path: "s3://production/main-dataset", name: "source" }
]
```

*Credits: Uber AI Infrastructure; Jay Narale drove the Rust/Python/Java implementation plus concurrent-write, conflict-resolution and edge-case testing.*

---

## 2. Branching, tagging and shallow clone

Author context: Jack Ye **wrote the original 2021 Apache Iceberg Snapshot Lifecycle Management design**
that introduced branching and tagging to Iceberg. This post is his retrospective on it.

### Iceberg's model and its three failures

Structurally Git-like: the table metadata file holds a map of named refs → snapshot IDs. Branches point
at a branch head; tags pin a snapshot against cleanup. Every table has `main`. E.g. `main`→snapshot 10,
`staging`→snapshot 8, tag `v1.0`→snapshot 5.

Three original goals: (a) decouple time travel from cleanup — Iceberg metadata grows with commit count
so you want aggressive expiry, but need certain versions kept for audit/compliance/reproducibility, and
tags solved that; (b) make write-audit-publish useful — Iceberg had single-pending-change "staged"
commits, branches extended that to accumulate/review/test before merge ("QA teams back in the day, and
AI agents today"); (c) fast ML/AI experimentation.

Goals (a) and (b) succeeded. (c) was **half-solved**, for three reasons:

1. **Performance bottleneck.** Every operation on any branch — create, commit, delete — updates the
   *root table metadata file*. All branches share it. So experimental writes conflict with production
   commits, and every experimental commit invalidates production read-metadata caches. High-frequency
   experimentation degrades production.
2. **Weak governance isolation.** Branches share the main branch's directory and metadata. No physical
   isolation, so you cannot express "read-only on production, write-only on the branch"; a buggy
   experimental write can tamper with production data and storage has no mechanism to stop it.
3. **Poor observability and cost attribution.** Shared directory → audit logs can't distinguish
   production from experimental access; branch storage is charged to the production table's budget.
   Tooling would need branches as a first-class *sub-table* construct, but the ecosystem's unit of
   observability is the table.

**Result:** even with Iceberg dominant as an analytical table format, ecosystem tooling for branch-level
governance/observability is effectively unbuildable against a generic Iceberg table, and adoption of
branching/tagging for ML/AI experimentation stayed low.

### Delta's shallow clone

Instead of refs inside a table, create a whole new table referencing the source's files — enabled by
Delta's absolute-path support. New writes go to the clone's own location via relative paths.
The clone: has its own location and identity; is governed independently (permissions, quotas, auditing);
shares no metadata with the source after creation; can diverge arbitrarily (appends, updates, schema
changes). It sidesteps all three Iceberg problems — the clone is a first-class table that plugs into
existing governance; delete it if the experiment fails.

Jack Ye was convinced for years this was the better ML/AI model.

### The perspective shift

**ByteDance and Netflix independently asked for *branching* in Lance**, and had converged on the same
three reasons:

- **Auto-traceability** — a branch is structurally associated with its parent, so lineage comes out of
  the box; no extra lineage framework reconstructing the relationship after the fact.
- **Simpler data management** — all data stays within one table; cleanup runs off branch/tag expiration
  policies; no clones scattered across locations to lose track of (and no GDPR compliance break from a
  forgotten clone).
- **Intuitive DX** — ML/AI engineers are developers; create/experiment/merge-or-discard is immediately
  understood. Better than managing a web of shallow clones.

So: can you get both? Lance's answer, built on multi-base. (Full spec: the *Lance Branch and Tag
Specification*.)

### Building block 1 — shallow clone = multi-base

```
Source dataset: s3://production/main-dataset
Clone dataset:  s3://experiments/test-variant

Clone manifest base_paths:
[
  { id: 0, is_dataset_root: true, path: "s3://experiments/test-variant" },
  { id: 1, is_dataset_root: true, path: "s3://production/main-dataset", name: "source" }
]

Original fragments (inherited from source):
  DataFile { path: "fragment-0.lance", base_id: 1 }
  → resolves to: s3://production/main-dataset/data/fragment-0.lance

New fragments (clone-specific):
  DataFile { path: "fragment-new.lance", base_id: 0 }
  → resolves to: s3://experiments/test-variant/data/fragment-new.lance
```

Fully independent dataset: own location, metadata, version history. No data copied, only metadata
created. Same as Delta's shallow clone **but with one base entry instead of a full absolute path per
inherited file** — same isolation, much smaller metadata.

### Building block 2 — tags as immutable global refs

Tags are immutable named pointers to a version. Created once, never change. They are a **global**
concept living outside the version timeline: **invariant under time travel and rollback** — roll the
dataset back and every tag survives.

Tag record:

```json
{
  "branch": "feature-a",
  "version": 3,
  "manifest_size": 4096
}
```

Stored under `_refs/tags/` at the dataset root; can reference any version on any branch; tagged versions
are exempt from GC. Stronger than Iceberg, where tags live *inside* the table metadata file and are
therefore subject to metadata-replacing operations like rollback. Lance can spread information across
storage within a location instead of consolidating into one metadata file, so tags get their own files.

### Building block 3 — branching = shallow clone + tag

A branch is a shallow clone that lives *inside* the source dataset, plus a tag-like ref recording the
fork point. Branches and shallow clones are conceptually the same thing; the difference is only **where
they live** — a branch inside the source's directory structure, a clone at an independent location.

**The critical departure from Iceberg (and from Git): Lance tracks branches by ROOT, not by HEAD.** In
Iceberg the main metadata holds each branch's head snapshot ID, so every branch commit rewrites root
metadata — the root cause of both the perf coupling and branch data landing in the main data location.
Tracking by root is a worse fit for Git-style code but a much better fit for a table format holding
petabytes across millions of files.

Layout:

```
dataset_root/
    _refs/
        branches/
            feature-a.json        # branch metadata only
    tree/
        feature-a/                # branch directory
            _versions/
                1.manifest        # branch's own version history
                2.manifest
            data/
                *.lance           # branch's own data files
            _transactions/
                *.txn
            _deletions/
                *.arrow
```

Branch metadata records only the creation point:

```json
{
  "parent_branch": null,
  "parent_version": 5,
  "created_at": 1706547200,
  "manifest_size": 4096
}
```

`parent_branch` is `null` for branches off main, or the parent branch name — which enables **branching
from branches** (hierarchical experimentation).

Creating a branch does five things:
1. write `_refs/branches/{name}.json` recording parent branch + version;
2. create a new manifest in `tree/{name}/_versions/`;
3. that manifest carries a base path pointing at the parent dataset root;
4. all parent fragments are referenced through that base;
5. new writes create fragments with **no `base_id`**, stored locally in `tree/{name}/data/`.

```
Source dataset: s3://data/my-dataset

Branch manifest base_paths:
[
  { id: 0, is_dataset_root: true, path: "s3://data/my-dataset", name: "parent" }
]

Original fragments (inherited from parent):
  DataFile { path: "fragment-0.lance", base_id: 0 }
  → resolves to: s3://data/my-dataset/data/fragment-0.lance

New fragments (branch-specific):
  DataFile { path: "fragment-1.lance" }  // no base_id
  → resolves to: s3://data/my-dataset/tree/feature-a/data/fragment-1.lance
```

This kills all three Iceberg problems: **no perf bottleneck** (branch writes never touch main's
metadata → no cross-branch commit conflicts, no cache invalidation), **strong governance isolation**
(branch data physically under `tree/<branch>/`, so storage ACLs can be read-only on main and write-only
on the branch), **clear observability/cost attribution** (per-directory audit logs and storage
accounting). Plus a bonus Iceberg cannot offer: **time travel *within* a branch** — Iceberg branches are
pointers to a single snapshot with no lineage of their own; every Lance branch keeps a complete version
history.

### Python API

**Tags**

```python
import lance
import pyarrow as pa

data = pa.table({"id": range(1000), "feature": range(1000)})
ds = lance.write_dataset(data, "s3://bucket/my-dataset")

more_data = pa.table({"id": range(1000, 2000), "feature": range(1000, 2000)})
ds = lance.write_dataset(more_data, ds, mode="append")

ds.tags.create("baseline", 1)
ds.tags.create("training-v1", ds.version)

print(ds.tags.list())
# {'baseline': {'version': 1, 'branch': None, ...},
#  'training-v1': {'version': 2, 'branch': None, ...}}

ds_baseline = ds.checkout_version("baseline")
print(len(ds_baseline.to_table()))   # 1000 rows
```

**Branches**

```python
ds = lance.dataset("s3://bucket/my-dataset")

experiment = ds.create_branch("feature-experiment")
print(experiment.version)             # branch-local version (e.g. 1)
print(len(experiment.to_table()))     # 2000 rows (same as main)

experimental_data = pa.table({
    "id": range(2000, 3000),
    "feature": [x * 2 for x in range(1000)],
})
experiment = lance.write_dataset(experimental_data, experiment, mode="append")

print(len(experiment.to_table()))     # 3000
print(len(ds.to_table()))             # still 2000

# nested branches for A/B testing
variant_a = experiment.create_branch("variant-a")
variant_b = experiment.create_branch("variant-b")

print(ds.branches.list())
# {'feature-experiment': {'parent_branch': None, 'parent_version': 2, ...},
#  'variant-a': {'parent_branch': 'feature-experiment', 'parent_version': 2, ...},
#  'variant-b': {'parent_branch': 'feature-experiment', 'parent_version': 2, ...}}

# checkout takes a (branch, version) tuple; None = latest on that branch
ds_variant_a = ds.checkout_version(("variant-a", None))
ds_variant_a = lance.write_dataset(
    pa.table({"id": range(3000, 4000), "feature": [x * 3 for x in range(1000)]}),
    ds_variant_a, mode="append",
)
print(len(ds_variant_a.to_table()))   # 4000
print(len(ds.to_table()))             # still 2000 on main
```

**Shallow clone**

```python
ds = lance.dataset("s3://production/main-dataset")

clone = ds.shallow_clone("s3://experiments/clone-latest", ds.version)

clone_baseline = ds.shallow_clone("s3://experiments/clone-baseline", "training-v1")  # by tag

branch = ds.checkout_version(("feature-experiment", None))
clone_from_branch = branch.shallow_clone("s3://experiments/clone-experiment", branch.version)

# the clone is fully independent
clone.tags.create("clone-baseline", 1)
clone = lance.write_dataset(pa.table({"id": [4000], "feature": [12345]}), clone, mode="append")
```

### `lance-git` (proposed subproject, not built)

Jack Ye's framing: his Iceberg design in hindsight gave an **SVN**-like experience — centralized, one
metadata file, no isolation, no independent history. Lance's design is genuinely Git-like: distributed,
isolated branches with independent histories, working across local and remote storage.

The three primitives that make it possible: **multi-base** (data partly local, partly remote — like
local vs remote refs), **shallow clone** (a `git fetch`-alike: pull partial metadata without copying
data), **branches + tags** (the UX primitives that make Git intuitive).

| Git | Lance | Description |
| --- | ----- | ----------- |
| Repository | Dataset | The unit of version control |
| Remote | Base path to cloud storage | Where the dataset lives (e.g. `s3://production/dataset`) |
| Clone | Shallow clone to local | Copy metadata locally, reference cloud data via multi-base |
| Fetch | Shallow clone (partial) | Pull latest metadata from cloud without copying data files |
| Pull | Fetch + merge | Fetch latest metadata and integrate new versions locally |
| Commit | Version | Record a new point-in-time snapshot on the current branch |
| Branch | Branch | An isolated line of development, locally or in cloud |
| Tag | Tag | Immutable named reference to a version |
| Checkout | Checkout branch/version | Switch branch/version for reading and writing |
| Push | Write to remote base | Upload local branch data + metadata back to cloud |
| Log | Version history | View a branch's version history |

Sketched CLI:

```bash
lance-git init s3://bucket/my-data
lance-git clone s3://production/dataset ./local-copy
lance-git branch experiment
lance-git checkout experiment
lance-git log
lance-git tag v1.0.0
lance-git fetch origin
lance-git pull origin main
lance-git push origin experiment
```

Explicit position: the goal isn't to replicate Git (data has different semantics), and prior attempts
exist at other layers — **Nessie** (catalog-level versioning), **lakeFS** (storage-level Git layer),
**Bauplan** (versioned data pipelines) — but the **table format is the right layer**, because it owns
data layout, metadata structure and file lifecycle; versioning anywhere else works *around* the format.
Open call for collaborators on `lance-git`.

*Credits: Nathan Ma (ByteDance) co-designed and drove implementation; Pablo Delgado and Bryan Keller (Netflix) gave the feedback that shaped the design.*

---

## 3. Blob V2 — multimodal as a first-class citizen

Context: LanceDB works with Runway, Midjourney, WorldLabs, Harvey and others. Three pain points recur.

### The three problems

1. **Mixed blob access strategy.** Multimodal datasets are long-tailed: many small objects (tens of KB)
   plus a few large ones (MB → hundreds of GB). Small objects need locality and low overhead (must not
   slow full scans, must not detour on random read). Large objects need *operability* (not rewritten
   unnecessarily; cacheable, migratable, governable independently — so a table-level compaction isn't an
   I/O nightmare). One physical strategy for both is "fine most of the time, suddenly terrible on
   certain batches."
2. **Existing external references.** Many teams already have mature media asset libraries — path
   conventions, permissions, hot/cold tiering, lifecycle policies. Copying into the table is duplicated
   cost and fragmented governance. And reads are often **ranged**: "I only need this byte range" of a
   video/audio/large binary. Without native external-reference + range-read semantics, users stitch it
   together in the application layer.
3. **Lifecycle governance.** With data outside the table: which objects are still referenced by a
   dataset version? Which are orphans? How do you clean up safely under snapshots and version evolution?
   If users must maintain manifests and reconciliation scripts, blobs stay supporting characters.

### Why the two existing approaches fail

**Approach 1: everything as an external reference** (what most formats do). Two reasons: uniformity is
simple, and OLAP columnar formats are structurally bad at inline blobs because of **row groups** — a row
group is either too large (slow writes, wasteful reads for a few images) or too small (metadata
overhead, poor compression). Storing path strings plays to what those formats do well.

Modern lakehouses built abstractions on top — **Unity Catalog Volumes**, **Apache Gravitino Filesets** —
which *look* like first-class blob support but are still separate from tables, governed through different
APIs, managed as a segregated catalog component: a governance layer over the same approach, where blobs
live outside your data and you keep the two in sync.

This solves Problem 2 completely but breaks the other two:
- *Training inefficiency* (Problem 1): training typically constrains multimodal sizes to <1 MB images and
  <5 MB video clips — exactly the small-blob case external refs handle worst. **GPU utilization**: each
  blob is a separate HTTP request; connection overhead makes training I/O-bound and wastes GPU cycles.
  **Storage metadata bottleneck**: billions of references = billions of objects, which pressures object
  store and (especially) high-performance training file system metadata.
- *Lifecycle pain* (Problem 3): external refs don't follow row lifecycle. Delete the row, the image
  survives. You end up building pipelines and big cross-table joins to find live blobs, and the table
  format's ACID/time-travel/snapshot guarantees don't apply — which matters for e.g. GDPR.

**Approach 2: everything stored inline** = **Lance Blob V1**. Lance has no row groups, so it avoids that
constraint. Blob V1's specialized encoding stores blob **bytes out-of-band**: pages hold only blob
metadata (start position + size), actual bytes live outside the page structure — compact pages,
continuously addressable blobs.

```python
import lance
import pyarrow as pa

values = pa.array([b"image_bytes_1", b"image_bytes_2", b"image_bytes_3"], pa.large_binary())
table = pa.table(
    [values, pa.array([0, 1, 2])],
    schema=pa.schema([
        pa.field("image", pa.large_binary(), metadata={"lance-encoding:blob": "true"}),
        pa.field("id", pa.uint64()),
    ])
)
ds = lance.write_dataset(table, "/tmp/images.lance")

blobs = ds.take_blobs("image", indices=[0, 1, 2])
for blob in blobs:
    with blob as f:
        data = f.read()   # file-like interface with seek support
```

V1 wins on training performance (blobs co-located with metadata into GPU memory), minimizes file count,
and gives blobs row lifecycle (delete the row → blob cleaned atomically). But:

- **The big-file problem.** With Lance's data evolution (cheap column adds), users store very large raw
  media in Lance and iteratively add features/embeddings/downsized samples as columns. Data files get so
  large that size-based compaction becomes impractical — a few rows can be multiple GB — and **every
  compaction rewrites the source blobs**, expensive and never necessary since raw data rarely changes.
- **Migration blocker.** Teams whose ingestion pipelines already dump media into buckets can't rewrite
  that infrastructure overnight. They need a hybrid transition state — reference existing external blobs
  while incrementally moving into Lance. V1's all-or-nothing inline model doesn't allow it.
- **The self-reflection.** V1 treated blobs as an *access technique* (bytes out-of-band, located by
  offset), which presumes **one physical manifestation of a blob**. Under mixed size + mixed source +
  mixed access patterns the bottleneck moves from implementation to **semantics**: the system can't
  express *what this blob is and how it should be treated*, which caps optimization and pushes
  complexity onto users. So V2 elevates blobs from an ancillary technique to a **system-level data
  asset**.

### The V2 design principle

Decouple **user expression** from **system storage**.
- What users express is small: provide the **content** (bytes) or the **location** (external URI), and
  optionally "I only care about this range."
- What the system handles is large: pick the storage method, locate and read data, govern lifecycle
  across version evolution — and not leak any of it into the user interface.

Honest aside from the authors: they first tried to design **one universal layout** and found it
impossible. There is no single best layout for all blobs, so they moved to **multi-semantic storage**.

### The four storage semantics

Different objects **in the same column** pick different homes; users always see one unified blob type.

| Blob size | Semantic | Physical layout |
| --------- | -------- | --------------- |
| ≤ 64 KB | **Inline** | Stored directly within the main data file |
| 64 KB – 4 MB | **Packed** | Concatenated into shared `.blob` sidecar files (up to 1 GiB each) |
| > 4 MB | **Dedicated** | Individual `.blob` file per blob |
| user-provided URI | **External** | Reference to external storage |

Thresholds are **configurable via schema metadata**; defaults are tuned for typical multimodal AI.

- **Inline** — locality, low metadata overhead, simple read path; small blobs don't pay the large-object
  complexity tax. Thumbnails come back alongside row data with zero extra overhead.
- **Packed** — for numerous medium objects (mid-size images, audio clips). Keeps the main data file from
  being bloated and rewritten by blobs, without one-file-per-object management pressure. Lance
  aggregates into the same file per configured thresholds and actively splits at appropriate points so
  each pack stays reasonably sized — batch-read throughput preserved, random-access cost controlled.
- **Dedicated** — truly large objects (HD video, whole PDFs) as individual files, **isolated from the
  table-level rewrite/compaction path**, so migration, caching and lifecycle policy are controllable;
  range reads use the object store's native range capability instead of loading the whole file.
- **External** — the blob column stores only the URI; reads transparently redirect. Interop, no copying,
  natural range reads. **Integrated with multi-base**: when an external URI maps to a registered base
  path, Lance stores only the **relative path + base ID**, keeping descriptors compact while enabling
  full lifecycle management. **URIs outside registered bases are rejected by default** — users must
  explicitly opt in to absolute external URIs, accepting that lifecycle management is then their
  responsibility.

Mixed workloads within one column are the norm, not the exception. On write, the user supplies bytes or
a URI; the system picks the semantic from size + configuration.

### Format design

Why this is possible at all: **Lance controls both the file format and the table format** (unlike
Parquet + Iceberg, developed separately), so blob support extends from file level into table level and
routes across all four semantics without exposing complexity.

**Unified on-disk descriptor** — the same Arrow struct regardless of physical location:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `kind` | uint8 | Storage semantic: 0=Inline, 1=Packed, 2=Dedicated, 3=External |
| `position` | uint64 | Byte offset within the file (data file for Inline, pack file for Packed, external file for External range) |
| `size` | uint64 | Blob size in bytes |
| `blob_id` | uint32 | Packed/Dedicated: references `.blob` sidecar files. External: base ID (0 = absolute URI, >0 = relative to registered base) |
| `blob_uri` | string | External: URI or relative path. Empty for managed blobs |

The reader switches on `kind`, then uses the appropriate fields. That uniformity is what makes one read
API possible.

**Division of labour:**
- **Inline (≤64 KB)** — the *file format* handles everything: pages store position/size, actual bytes go
  to a dedicated buffer section within the same `.lance` file (compact pages, locality preserved).
- **Packed / Dedicated** — the *table format* takes over. Packed sidecars batch medium blobs into shared
  files up to 1 GiB each (reducing file count); Dedicated stores one blob per file, enabling direct
  object-store range reads and isolating large blobs from compaction.

Table-format lifecycle tracking:

| Mechanism | Description |
| --------- | ----------- |
| Fragment → blob mapping | Each fragment records which `blob_id` values it references |
| Blob file naming | `data/{data_file_key}/{reversed_blob_id}.blob` |
| Version awareness | Blob files are tied to fragments, enabling version-based GC |

This gives efficient path encoding (4-byte `blob_id` instead of full URIs), lifecycle tracking (safe GC),
and **repack** operations (consolidate sparse packs without rewriting data files).

> Note on naming: the `010101…` prefixes are throughput optimization — blob file names use the **reversed
> binary string of the blob ID**. (Lance's file-naming optimizations across manifests, data files and
> blob sidecars are promised their own post.)

### The unified read experience

`take_blobs()`, `BlobFile.read()`, `BlobFile.seek()` work **identically across all four semantics** —
same file-like interface to read bytes, seek, and stream ranges whether the blob is in the main data
file, a shared pack, a dedicated object, or an external URI. The system routes on `kind` behind the
scenes; new layouts in future leave user code unchanged.

```python
import lance
import pyarrow as pa
from lance import Blob

# 1) mixed blob values — each lands in a different storage semantic
small_bytes   = b"tiny-inline-data"             # → Inline (≤64 KB)
medium_bytes  = b"x" * 100_000                  # → Packed (64 KB – 4 MB)
large_bytes   = b"y" * 5_000_000                # → Dedicated (> 4 MB)
external_uri  = "/path/to/existing/video.mp4"   # → External (URI reference)

values = [
    small_bytes,
    medium_bytes,
    large_bytes,
    external_uri,
    Blob.from_uri(external_uri, position=1024, size=4096),  # External range slice
]

# 2) blob extension array
table = pa.table({"id": pa.array([1, 2, 3, 4, 5]), "blob": lance.blob_array(values)})

# 3) write with a blob-v2-enabled storage version
ds = lance.write_dataset(table, "./blob_v2_demo.lance", data_storage_version="2.2")

# 4) schema uses the lance.blob.v2 extension type
print(ds.schema)
# id: int64
# blob: extension<lance.blob.v2<...>>

# 5) inspect descriptors  (kind: 0=Inline 1=Packed 2=Dedicated 3=External)
descriptors = ds.to_table(columns=["blob"]).column("blob")
for i, desc in enumerate(descriptors):
    d = desc.as_py()
    kind_name = ["Inline", "Packed", "Dedicated", "External"][d["kind"]]
    print(f"Row {i}: {kind_name} (size={d['size']})")
# Row 0: Inline (size=16)
# Row 1: Packed (size=100000)
# Row 2: Dedicated (size=5000000)
# Row 3: External (size=...)
# Row 4: External (size=4096)

# 6) unified read API — same interface for ALL semantics
blobs = ds.take_blobs("blob", indices=[0, 1, 2])
for blob in blobs:
    with blob as f:
        data = f.read()
        f.seek(0)
```

### Governance

Multi-semantic storage makes governability mandatory: the system must reclaim data living outside the
table or it creates new operational minefields. Lance determines **reachability within the context of
dataset versions and snapshots**, identifying and reclaiming orphaned carriers with no user-maintained
manifests. On version cleanup it scans external blob objects and retains only those still referenced by
active versions — e.g. blobs referenced only by versions 1 and 2 become orphans once those versions are
cleaned, and are GC'd. The process is **automatic, incremental, and non-blocking for normal reads and
writes**.

This extends to **external** blobs: because external blob bases are tracked in the manifest, reachability
works the same as for managed sidecars — automatic GC, plus compute engines can apply **credentials
vending and column-level policies** exactly as for any other table/column. Explicitly: *no more separate
Volumes and Filesets.*

Closing claim: future Lance can manage multimodal data the way Git manages code — branches, tags,
snapshots, fine-grained permissions — on an open format.

---

## 4. Blob V2 in Spark — late materialization

### The mismatch

Product catalog table: `id`, `category`, `label`, `embedding`, plus a product image.

```sql
UPDATE products SET label = 'discontinued' WHERE id = 42;
```

Nothing there asks to decode, resize, classify or ship the image. But if the image is an inline `BINARY`
column (as in Parquet-/Iceberg-backed tables), Spark has exactly **one abstraction for the value: bytes
inside a row**. Row-level SQL rewrites rows, the blob column must survive the plan, and the default way
to survive is to move the payload with the row.

Multimodal tables are mostly metadata with a few very large assets. Spark is good at moving rows; large
assets want a different contract — **a small reference in the plan, bytes materialized only at a boundary
that actually needs bytes**.

### The tradeoff Spark forces today

| Model | What Spark moves | What the user owns | Practical consequence |
| ----- | ---------------- | ------------------ | --------------------- |
| Inline `BINARY` | Payload bytes | A simple SQL schema | Metadata-only jobs still pay blob-sized heap and network costs |
| Path / URI column | String references | Download, versioning, auth, layout conventions in app code | The table and the asset lifecycle drift apart |
| **Lance Blob V2** | Descriptor or copy reference | One logical blob column; Lance owns physical layout and materialization | Connector keeps SQL simple while moving references through the plan |

### What the connector does

On **scan**, a Blob V2 column is *not* exposed as `byte[]`. It's a descriptor:

```
struct<kind:short, position:long, size:long, blob_id:long, blob_uri:string>
```

Enough to answer "what is the blob size?" and enough for the connector to find bytes later. This fetches
no image bytes:

```sql
SELECT id, image.size, image.kind
FROM lance.db.products
WHERE category = 'shoes';
```

On **write**, users still hand Spark `BINARY`:

```sql
CREATE TABLE lance.db.products (
    id INT NOT NULL,
    category STRING,
    label STRING,
    image BINARY
) USING lance
TBLPROPERTIES (
    'image.lance.encoding' = 'blob',
    'file_format_version' = '2.2'
);

INSERT INTO lance.db.products VALUES
    (1, 'shoes', 'active', X'89504E47');
```

**Reads and writes need not use the same physical representation inside Spark.** Reads stay descriptors,
writes accept bytes, and Lance Core decides where bytes live inside the dataset's versioned storage
model. The deeper point: a large-asset column wants **multiple execution representations under one
logical schema** — metadata for reads, bytes for direct writes, references for Lance-to-Lance movement.

### Reference passthrough (the "copy-through" path)

For safe Lance-to-Lance writes the connector uses **reference passthrough, not byte passthrough**: the
optimizer replaces a direct Blob V2 column assignment with a **small copy token**, and the writer later
resolves that token to real bytes. The token carries the **source dataset context, source row address,
and blob column**. Spark can project, join, filter and shuffle the token like any small value; the Lance
writer materializes bytes only on the write path.

No separate blob system is created — the connector just preserves enough source context for Lance Core to
apply Blob V2's normal layout, materialization, versioning and write rules. *Spark keeps moving small
values; Lance owns asset materialization.*

### SQL that benefits

**INSERT / CTAS** — with inline `BINARY`, `image` is the bytes; with Blob V2 it's a reference until the
target is written. Real blob values still land in the target table; Spark just never shuffles payloads.

```sql
INSERT INTO lance.db.products_archive
SELECT id, category, label, image
FROM lance.db.products
WHERE category = 'seasonal';
```

**Joins** — metadata from one table, images from another, image stays a reference. One-to-many works too:
if one image fans out to several labels/crops, each output row carries its own reference and the writer
copies the blob into the rows it writes.

```sql
INSERT INTO lance.db.training_examples
SELECT p.id, p.category, i.image
FROM lance.db.product_metadata p
JOIN lance.db.product_images i
  ON p.id = i.id
WHERE p.split = 'train';
```

**MERGE / UPDATE** — the important case, where inline bytes hurt most. Spark still rewrites rows under
the hood, so the connector must carry the **untouched target blob** forward to make the rewritten row
complete; reference passthrough preserves it by reference and materializes on write.

```sql
MERGE INTO lance.db.products t
USING lance.db.label_fixes s
ON t.id = s.id
WHEN MATCHED THEN UPDATE SET t.label = s.new_label;

UPDATE lance.db.products
SET label = 'archived'
WHERE category = 'seasonal';
```

And when SQL genuinely replaces a blob, the same mechanism copies from the source:

```sql
MERGE INTO lance.db.products t
USING lance.db.corrected_images s
ON t.id = s.id
WHEN MATCHED THEN UPDATE SET t.image = s.image
WHEN NOT MATCHED THEN INSERT (id, category, label, image)
VALUES (s.id, s.category, 'new', s.image);
```

**Coverage:** `INSERT … SELECT`, CTAS, replace-table-as-select, and **Spark 3.5+ MERGE and UPDATE** can
preserve Blob V2 columns without turning descriptors into user-managed path columns or shuffling raw
payloads. No new copy API, no per-job layout decision. **If a query turns a descriptor into a new value,
Spark keeps descriptor semantics.** (The detailed support matrix lives in the Spark Blob V2 docs, not the
post.)

### Why it matters

Two wins: **performance** (metadata-only and Lance-to-Lance jobs move small references instead of
dragging payloads through scans, shuffles and row rewrites) and **ease of use** (no layout picking, no
path-column conventions, no remembering which jobs must download bytes). A blob column stays a column;
Lance Core owns Blob V2 layout at the format layer, Lance Spark owns the reference path through Spark.
*Late materialization in practice: references through the plan, bytes on write.*

---

## 5. lance.org spec — rollout artifact blobs: memory behavior and streaming roadmap

**Different genre, and worth flagging:** this is not a LanceDB blog post but an implementation/ops spec
for a service (`lance-context`, `crates/lance-context-core/src/rollout_store.rs`) that stores rollout
artifacts. It is a **counter-example to §3–4**: a real system that wants blob-v2 and cannot yet have it.
Items 1–3 are implemented; §4 is a proposal, not built.

### Background — where blob bytes live in memory

`binary_payload` is a plain **inline `LargeBinary` column, not a lance blob-v2 (`lance-encoding:blob`)
offloaded column**. Deliberate: rollout reads go through the **MemWAL LSM scanner, which has no
blob-materialization step**, so a blob-v2 column **reads back as `None`** there. Inline is currently the
only encoding that round-trips.

So every blob is fully materialized in RAM at each hop:
- **Upload** — the whole request body is buffered (JSON base64 ≈ +33% expansion via
  `axum::body::to_bytes`, or each multipart part via `field.bytes()`), then appended into a
  `LargeBinaryBuilder` whose `finish()` copies into a contiguous Arrow buffer.
- **Download** — `get_blob` locates the row, `take_rows` materializes the `binary_payload` Arrow buffer,
  and `.to_vec()` copies it into an owned `Vec<u8>` (**≈2× blob size** at that instant).

With a 1 GiB per-request ceiling (`MAX_ROLLOUT_UPLOAD_BYTES`) and no concurrency cap, N concurrent large
requests need ≈`2 × size × N` bytes and could OOM the worker.

### 1. Streaming downloads (implemented)

Both blob-serving handlers — worker `fetch_rollout_blob`, master `download_experiment_blob` — send the
payload as a **chunked body** (`blob_stream_body`, **256 KiB frames**) instead of one
`Body::from(Vec<u8>)`. Frames are refcounted `Bytes` slices of the single backing allocation (no
per-frame copy). Removes the extra full-blob copy the HTTP send path would hold, and lets a slow client
apply backpressure at frame granularity instead of after the whole payload is queued. `Content-Length`
is still set, so the wire transfer isn't chunked-transfer when length is known.

*Does not* remove the single in-RAM `Vec<u8>` from `get_blob` — bytes are still fully read from storage
first. True read-from-storage-in-frames requires §4.

### 2. Single-scan record + blob (implemented)

`download_experiment_blob` used to do `get_by_id` (full-row point scan) **then** `get_blob` (a second
point scan over the same shard) — two scans per download. `RolloutStore::get_record_with_blob` folds them
into one base-first scan returning `(record, payload)`, **halving scan work** on the master download path,
while keeping the base-table-first fast path and the NotFound-tolerant WAL fallback semantics.

### 3. In-flight blob-byte budget (implemented)

`BlobBudget` in the worker's `AppState`, sized by **`ROLLOUT_MAX_INFLIGHT_BLOB_BYTES`** (`0` = disabled),
is a process-wide admission budget bounding total blob payload held across concurrent uploads+downloads.

- **Uploads** reserve the declared `Content-Length` *before* the body is buffered; held for the whole
  handler.
- **Downloads** reserve payload size once known and hold the reservation **inside the streamed body**, so
  a slow client keeps bytes accounted until the last frame flushes.
- On refusal: **`503 OVERLOADED`** and `rollout_blob_budget_rejections_total` is incremented, instead of
  allocating. *Backpressure moves to the edge, not the allocator.*
- It bounds **concurrency, not maximum blob size** — a lone request larger than the whole budget is
  admitted when the instance is idle, so a single big blob never permanently 503s against its own limit.
- **Sizing guidance:** a fraction of the pod memory limit leaving headroom for the ≈2× transient copy per
  in-flight download plus base overhead — e.g. **4 GiB pod → ~1–1.5 GiB budget**.

### 4. True streaming storage (proposal — NOT implemented)

Items 1–3 bound and smooth memory, but every blob is still fully resident once per request. Eliminating
that needs ranged, chunked I/O: **blob-v2 offloaded columns + `BlobFile` range reads**.

**The blocker:** the rollout read path is the MemWAL LSM scanner, which unions base table with flushed
WAL generations and has no blob-materialization step — a `lance-encoding:blob` column reads back as
`None` through it. You cannot flip `binary_payload` to blob-v2 until the scanner can resolve blob
descriptors.

**Sub-step 1 — reader-side `BlobFile` range read for the base table.** Keep `binary_payload` inline in
the WAL (small, short-lived generations), but store artifact bytes as a blob-v2 column **in the base
table**. Change `get_blob`/`get_record_with_blob` so a base-table hit opens the row's blob descriptor and
returns a `BlobFile`/reader streaming ranges from object storage, wired into `blob_stream_body` (frames
pulled from storage on demand, not from an in-RAM `Vec`). WAL-fallback rows stay inline — they're the
un-merged tail, folded into base on merge. Covers the already-merged **99% path** without touching the
LSM union. *Medium effort (core `rollout_store.rs` change + a lance API dependency on `BlobFile` range
reads), low blast radius (reads only, base only).*

**Sub-step 2 — writer-side streamed ingest into the blob column.** Replace "buffer whole body →
`LargeBinaryBuilder`" with a streamed writer appending blob bytes to the blob-v2 column in frames as the
body arrives. Multipart parts stream naturally; **JSON base64 would need a streaming base64 decoder or be
deprecated in favour of multipart for large blobs.** Upload peak memory drops from **O(blob) to
O(frame)**. *Higher effort; needs care around the atomicity guarantees of a rollout append (currently one
`RolloutStore::add`).*

Also required: **merge/compaction must carry blob-v2 columns from WAL-inline to base-offloaded**, and the
LSM merge path must be verified to preserve descriptors.

**Recommendation:** ship §1–3 now; schedule sub-step 1 next as its own PR (bulk of the read-side memory
win); treat sub-step 2 as a follow-up once the read path proves out.

---

## Cross-cutting summary

**One dependency chain.** `BasePath` (multi-base) is the load-bearing primitive. Shallow clone = a base
pointing at a source dataset root. A branch = a shallow clone living inside the source, at
`tree/<branch>/`, tracked **by root, not head**. Blob V2's External semantic = a blob URI resolved
through a registered base (`blob_id` doubles as base ID; `0` means absolute URI). Everything reduces to
"small integer + relative path, resolved against a short explicit base list."

**Recurring design principle.** Predictability over flexibility; keep the *count of things that must
change* small. Relocating 10M files = editing 5 strings. Referencing a location N times = one URI + N
one-byte IDs. Four blob layouts = one descriptor struct + one read API.

**What's explicitly *not* built:** `lance-git` (proposed, open call for collaborators); the Lance
file-naming optimization post (teased); blob-v2 through a MemWAL LSM scanner (source 5's blocker).

**Numbers worth quoting.** Blob thresholds 64 KB / 4 MB, pack files ≤ 1 GiB, `blob_id` 4 bytes,
`base_id` 1 byte up to 128 bases, training-typical sizes <1 MB image / <5 MB clip, rollout streaming
frames 256 KiB, rollout ≈2× transient copy, 4 GiB pod → ~1–1.5 GiB blob budget.

**Two claims from the WebFetch-summary layer that are NOT in the article text and should not be
repeated:** "42% lower storage in robotics", "50%+ storage reduction", "68x faster blob reads". Those
appear in link-preview/marketing copy around the blog, not in the bodies of these four posts. Don't cite
them as if the posts substantiate them.
