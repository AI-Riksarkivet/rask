# Verify: lineage / OpenLineage

Evidence log for the lineage track. Each section states a claim, the command that tested it, and the
verdict. Live checks run against the kind cluster `lance` (helm release `lance-ns`).

## Gold: lineage as JSON in the Lance file

**The claim under test** (from `docs/LINEAGE.md`, `services/lineage/seed.py`, and the medallion demo
header): *gold embeds its whole upstream provenance as a JSONB `lineage` column inside the Lance file.*

### Verdict

| Question | Answer |
| --- | --- |
| Does the **product** gold write embed lineage today? | **No.** The cascade's silver→gold mover writes `id, payload, source_rowid, stage` and nothing else. |
| Where does the JSONB embedding actually live? | Only in `scripts/medallion_demo.py::write_gold` (the demo driver) and in `services/lineage/seed.py`'s *synthetic* schema facet. Its reader, `GET /demo/datasets`, is **off** on the cluster (`LINEAGE_DEMO_DATA_ENABLED=false`). |
| Is the JSONB-in-Lance representation still what Lance recommends? | **Yes** — `pa.json_()` is the current recommendation and it is a *stronger* choice than we knew: it is indexable. Our demo code already writes `pa.json_()`, so there is no migration debt. |
| Does gold lie about its provenance? | **No.** It says nothing, and what the lineage graph says about it matches storage exactly (below). |

So the claim is **stale, not false in a dangerous way**: nobody is reading a lineage column that does not
exist, because the only reader is disabled. The honest statement is "the demo embeds provenance in gold;
the governed cascade does not".

### (a) What Lance recommends now — `lance.org/guide/json`

Fetched 2026-07-26; the vendored copy at `lance_docs/guide.md` (`FILE: docs/src/guide/json.md`) matches.

> Lance stores JSON data internally as JSONB (binary JSON) using the `lance.json` extension type. This
> provides: efficient storage through binary encoding; fast query performance for nested field access;
> compatibility with Apache Arrow's JSON type.

The write recipe is `pa.array([json.dumps(doc)], type=pa.json_())`. Query functions: `json_extract`,
`json_get`, `json_get_string/int/float/bool`, `json_exists`, `json_array_contains`, `json_array_length`.
Indexing (the part that is new relative to "store a JSON string"):

> For `pa.json_()` columns, create a scalar index with `IndexConfig` and specify the JSON path to index.
> The query should use the same path literal that was indexed.

and, for text search over the whole document, an `INVERTED` index on the JSON column.

The guide is explicit that a Utf8 column is not equivalent: *"For `pa.json_()` columns, use the JSON index
shown above and query with `json_get_*` or `json_extract`."*

### (b) What the gold write path actually writes today

The gold writer is `medallion.services.compute.transform_stage`, called from
`medallion.services.transform.handle_stage`. It carries upstream columns forward, mints/carries
`source_rowid`, stamps `stage`, and writes `mode="overwrite"`, `data_storage_version="2.2"`,
`enable_stable_row_ids=True`. There is **no** lineage column anywhere in that path — provenance leaves the
mover as an OpenLineage `RunEvent` (`medallion.schemas.events.build_run_event`), not as data.

Live proof. The terminal mover's own settings name the tenant gold table
(`MEDALLION_TO_DATASET=gold$catalog`, `MEDALLION_GOLD_WAREHOUSE_ENABLED=true`), which resolves to the
project's gold serving warehouse — bucket `acme-gold`:

```
$ kubectl exec deploy/lance-ns-silver-to-gold -c mover -- python -c '<open the dataset with the
  mover's own settings + OpenBao secret>'
=== s3://acme-gold/medallion/gold v 1 rows 8
id: int64
payload: string
source_rowid: uint64
stage: string
 ROW0 id = 0
 ROW0 payload = event-0
 ROW0 source_rowid = 24
 ROW0 stage = gold
```

The default (non-tenant) root is the same shape, minus `source_rowid` on that older incarnation:

```
URI s3://lance-catalog/medallion/gold
version 61 rows 8
id: int64
payload: string
stage: string
ROW0 id = 0 | payload = event-0 | stage = gold
```

A sweep of every Lance table in the `lance-catalog` bucket found no JSON column at all:

```
scanned 124 tables; json columns found: 0
```

### (c) Judgement — representation and queryability

Measured on the deployed runtime (`pylance 8.0.0`, `pyarrow 24.0.0`), writing a realistic gold provenance
document into a `lineage` column:

```
arrow type written: extension<arrow.json>
lance schema: id: int64
lineage: extension<arrow.json>
--- json_get_string filter        rows matched: 8
--- json_extract filter           rows matched: 8
--- json scalar index
indices: [{'name': 'lineage_idx', 'type': 'Json', ... 'fields': ['lineage'] ...}]
rows matched with index: 8
LanceRead: uri=..., full_filter=json_get_string(lineage, Utf8("dataset")) = Utf8("gold$catalog"), refine_filter=--
  ScalarIndexQuery: query=[Json(lineage = gold$catalog->dataset)]@lineage_idx(BTree)
```

So: **yes, a lineage field can be filtered without a full scan.** The plan resolves the predicate through
`ScalarIndexQuery ... @lineage_idx(BTree)` with an empty `refine_filter` — the JSON column is not scanned.

The same experiment against a plain `pa.string()` column fails on both counts:

```
STRING FILTER ERROR: ValueError Invalid user input: Error during planning: Failed to coerce arguments to
  satisfy a call to 'json_get_string' function: coercion from Utf8, Utf8 to the signature
  Exact([LargeBinary, Utf8]) failed
STRING INDEX ERROR: ValueError Invalid user input: A JSON index can only be created on a Binary or
  LargeBinary field.
```

**No migration cost.** We are not on an old pattern: the demo already writes `pa.json_()`, which is exactly
the current recommendation, and Lance reads it back as `extension<arrow.json>` (value returned as JSON
text; JSONB canonicalises key order). What is *unused* is the indexing half — if gold ever embeds lineage
for real, add `create_scalar_index(..., IndexConfig(index_type="json", parameters={"target_index_type":
"btree", "path": "<field>"}))` and query with the same path literal, or the column is a full scan.

Two follow-ups fall out of this (both outside the medallion partition, filed here so they are not lost):

1. `scripts/medallion_demo.py::write_gold` catches `AttributeError / ArrowNotImplementedError / TypeError`
   around `pa.json_()` and silently falls back to `pa.string()`. Per the errors above, that fallback
   produces a `lineage` column on which **every** JSON function and the JSON index fail — a silently
   unqueryable column. pyarrow is pinned to 24.0.0 in `uv.lock` and `pa.json_()` works there, so the
   fallback is currently dead code; it should either fail loudly or be dropped.
2. `services/lineage/seed.py` declares gold's column as type `"json"` in its schema facet. Until this pass
   the real renderer disagreed — see below.

### Fix landed: a JSON column is labelled `json`, not `extension<arrow.json>`

`common.schema.type_label` exists to keep raw pyarrow reprs out of the lineage graph (blob → `"blob"`,
vector → `"array<float>"`, binary → `"binary"`). It had no JSON branch, so a JSON column reached the
`SchemaDatasetFacet` — and the frontend column list — as:

```
type_label: extension<arrow.json>
facet_fields: [{'name': 'id', 'type': 'int64'}, {'name': 'lineage', 'type': 'extension<arrow.json>'}]
```

which contradicts the `("lineage", "json")` label `services/lineage/seed.py` already emits for the same
column. This is not hypothetical for the merged media path: `packages/ratch/model/schema.py` writes
`pa.field("alignments_json", pa.json_())` and `packages/ratch/features/topic_tree.py` writes `hierarchy` the
same way, and those tables are emitted through the vendored mirror
`common.lancekit.openlineage._type_label`, which had the same gap.

Both labellers now return `"json"`, matched by
`tests/unit/test_lineage_schema_facet.py::test_type_label_renders_a_json_column_as_json` and
`::test_lancekit_mirror_labels_json_the_same_way` (the mirror test asserts both modules produce the
identical facet, so they cannot drift apart again). Detection is on the extension name
(`arrow.json` / `lance.json`) because pyarrow 24 ships no `pa.types.is_json`.

### (d) Is gold's provenance consistent with the lineage service?

Nothing is embedded in gold, so there is no embedded copy to diverge. The available check is the graph's
record of the run that wrote gold versus the bytes on storage — and it is exact.

AGE (`lance-ns-age-0`, graph `lineage`), newest `aggregate_gold` run for `acme-gold$catalog`:

```
$ kubectl exec lance-ns-age-0 -- psql -U lance -d lineage -c "... MATCH (r:Run)-[w:WROTE]->
    (d:Dataset {name:'acme-gold$catalog'}) RETURN r, w ..."
Run   {"job": "lance-medallion/aggregate_gold", "author": "analyst",
       "run_id": "9e5d933a-3a8e-5ce8-9cd1-e263afd55d2b", "operation": "aggregate_gold",
       "event_type": "COMPLETE", "event_time": "2026-07-24T17:00:02.648458+00:00"}
WROTE {"version": "1", "row_count": 8, "size_bytes": 284,
       "schema": "[{\"name\":\"id\",\"type\":\"int64\"},{\"name\":\"payload\",\"type\":\"string\"},
                   {\"name\":\"source_rowid\",\"type\":\"uint64\"},{\"name\":\"stage\",\"type\":\"string\"}]"}
```

Inputs and the dataset-level edge:

```
MATCH (r:Run {run_id:'9e5d933a-...'})-[:READ]->(i:Dataset)      -> "acme-silver$features"
MATCH (d:Dataset {name:'acme-gold$catalog'})-[:DERIVED_FROM]->(u) -> "acme-silver$features"
```

Storage, measured with the mover's own `compute.measure`:

```
version 1 rows 8 bytes 284
fields: id:int64, payload:string, source_rowid:uint64, stage:string
```

Version, row count, byte count and the full schema all match the `WROTE` edge; the single input matches
the mover's configured `MEDALLION_FROM_DATASET` chain (`acme-silver$features` → `acme-gold$catalog`).
**No divergence.**

One structural caveat for whoever revives the embedding: in `scripts/medallion_demo.py` the Lance write
(`_perform`) runs *before* the terminal `COMPLETE` is emitted (`_emit_step`), and `_gold_provenance`
builds `produced_by` as a hand-written `{"job", "author"}` dict. The embedded document therefore carries
**no `run_id` for gold's own run** and cannot be joined back to the lineage service by run id — the very
correlation key (d) asks about. If gold is to embed provenance for real, the run id must be computed
first (it is deterministic — `common.openlineage.run_id_for(f"{project}-{operation}-{token}")`) and
written into the document. The DAG direction the demo appends is correct:
`GraphEdge` is documented as "`source` is derived from `target`", and it appends
`{"from": "gold$catalog", "to": "silver$features"}`.

### Reproduce

```bash
export PATH="$PATH:$PWD/.localbin"
POD=$(kubectl get pod -l app.kubernetes.io/component=silver-to-gold -o jsonpath='{.items[0].metadata.name}')

# gold's real schema + one row (uses the mover's own settings + OpenBao secret)
kubectl exec "$POD" -c mover -- python -c "
from medallion.core.config import MedallionSettings, apply_dapr_secrets
import lance
s = MedallionSettings(); apply_dapr_secrets(s)
ds = lance.dataset('s3://acme-gold/medallion/gold', storage_options=s.storage_options())
print(ds.version, ds.count_rows()); print(ds.schema); print(ds.to_table(limit=1).to_pylist()[0])"

# what the graph holds for the same dataset
kubectl exec lance-ns-age-0 -- psql -U lance -d lineage -t -A -c \
  "LOAD 'age'; SET search_path=ag_catalog,\"\$user\",public;
   SELECT * FROM cypher('lineage', \$\$ MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:'acme-gold\$catalog'})
   RETURN r, w \$\$) as (r agtype, w agtype);"
```
