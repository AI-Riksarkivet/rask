"""The per-stage provenance stamp — ONE implementation, imported by both cascade drivers.

The medallion runs its bronze→silver transform two ways: in-process (`medallion/services/compute.py`)
and distributed on Ray (`scripts/ray_stage_job.py`). Both must stamp the same provenance columns, and
until this module existed both did it with their own copy. The Ray copy's docstring said so outright —
"Mirrors compute._carry_source_rowid + _stamp_stage" — and a mirror maintained by hand is a mirror that
drifts.

IT HAD DRIFTED. Given one table already carrying `stage`, the two produced different SCHEMAS: the
in-process copy replaced the column in place, the Ray copy dropped it and appended it at the end. Since
`lance.write_dataset(mode="overwrite")` takes the table's schema as the dataset's, a silver table's
column order depended on which compute path wrote it — so two runs of one lane over one dataset left
schemas that are not equal, for no data reason.

PURE AT IMPORT, and pure per function but one. The stamp is a table in, a table out — no storage
options, no lance, no Ray — which is what lets one function serve a driver that holds a
`LanceDataset` and one that holds a Ray batch, and it is the `writing-python` § "Mixed I/O and
business logic" rule applied. The single exception, `ensure_declared_dataset_id`, takes a dataset
because the thing it repairs only exists on one; it imports lance LAZILY so this module stays
importable wherever the stamp is wanted.

IT LIVES IN service-kit rather than in the medallion because the Ray job CANNOT import the service: it
is baked into `.docker/ray-cluster.dockerfile`, which builds `--package ray-cluster-env` (the deps-only
platform-environment member) from the root lock, and that environment carries `service-kit`. Both
images therefore already have this package. A shared module in the medallion would be unreachable from
exactly one of its two callers.
"""

from __future__ import annotations

from typing import Final

import pyarrow as pa


#: The tier that produced a row. Re-stamped every stage, never appended twice.
STAGE_COLUMN: Final = "stage"

#: The BRONZE row a row descends from — root provenance (R23: bronze is the first governed tier).
SOURCE_ROWID_COLUMN: Final = "source_rowid"

#: The consume-layer provenance document (R26), a column of the table so a governed row is never
#: readable without it.
LINEAGE_COLUMN: Final = "lineage"

#: The canonical catalog name of the dataset a row was written INTO — schema metadata rather than a
#: column, because it describes the dataset and not the row. Read by
#: `maintenance.core.lineage_emit.declared_table_id` to name the dataset a maintenance run is about,
#: and asserted present by `attestation` O12.
LINEAGE_DATASET_ID_KEY: Final = "lineage.dataset_id"

#: Lance's reserved row-identity metacolumn. Read from, never written: the name is reserved and the
#: value advances on the next overwrite, so persisting it records an id that will not be true.
_ROWID: Final = "_rowid"

#: A lane that maps each input row to exactly one output row — every default stage runner's shape.
ONE_TO_ONE: Final = "1:1"

#: A lane that may emit MANY output rows per input row: a video into frames, a recording into speaker
#: turns, a document into chunks. `source_rowid` is what keeps such a child attached to its parent,
#: which is why the cardinality vocabulary lives beside the stamp that threads it rather than in the
#: declaration module — the two are one contract, and a stage driver needs it without importing a
#: spec registry.
ONE_TO_MANY: Final = "1:N"

#: Every cardinality a lane may declare. An unknown one is REFUSED rather than defaulted, at both the
#: declaration door and the job — a typo must not buy the loosest contract by falling through.
CARDINALITIES: Final = frozenset({ONE_TO_ONE, ONE_TO_MANY})


def carry_source_rowid(table: pa.Table) -> pa.Table:
    """Ensure `source_rowid` holds the stable `_rowid` of the BRONZE row this output descends from.

    An upstream that already carries it (a later stage) KEEPS it — re-minting from the immediate parent
    would silently reroot the provenance chain one tier down, so a gold row would name a silver row
    rather than the bronze one it actually descends from. The first derive off bronze mints it from the
    reserved metacolumn of the row just read, which requires the caller to have read `with_row_id=True`.

    HEAD DETECTION IS HEURISTIC — the absence of `source_rowid`, not a position. In the steady state only
    bronze lacks it, so this is exact. During a mixed-version rollout a mid-cascade dataset written by
    older code also lacks it, and a stage reading such an upstream mints from the IMMEDIATE parent for
    one cycle; it self-heals on the next full run from bronze. Acceptable only because the cascade is
    overwrite-only and re-runs.
    """
    if SOURCE_ROWID_COLUMN in table.column_names:
        return table.drop_columns([_ROWID]) if _ROWID in table.column_names else table
    if _ROWID not in table.column_names:
        return table
    minted = table.column(_ROWID).cast(pa.uint64())
    return table.drop_columns([_ROWID]).append_column(pa.field(SOURCE_ROWID_COLUMN, pa.uint64()), minted)


def _set_or_append(table: pa.Table, field: pa.Field, values: pa.Array) -> pa.Table:
    """Replace the column IN PLACE when it exists, else append.

    In place, and this is the half that had drifted: dropping and re-appending moves the column to the
    end, and the dataset's schema is the table's schema, so the same lane written by two different
    drivers produced datasets whose schemas differ for no data reason.
    """
    if field.name in table.column_names:
        return table.set_column(table.schema.get_field_index(field.name), field, values)
    return table.append_column(field, values)


def declare_dataset_id(table: pa.Table, dataset_id: str | None) -> pa.Table:
    """Set the destination's canonical name on the schema, or DROP an inherited one when unwired.

    Schema metadata survives every column operation `set_column`/`append_column`/`drop_columns`
    performs, so without this a child tier publishes its PARENT's name — the same false claim the
    `lineage` document rule below refuses, one level up. Merges the rest: Lance keeps other producers'
    schema metadata (the #21 self-describing coordinates among them) and a replace would destroy it.
    """
    metadata = dict(table.schema.metadata or {})
    key = LINEAGE_DATASET_ID_KEY.encode()
    if dataset_id:
        metadata[key] = dataset_id.encode()
    elif key not in metadata:
        return table
    else:
        del metadata[key]
    return table.replace_schema_metadata(metadata)


def stamp_stage(table: pa.Table, *, stage: str, lineage: str = "", dataset_id: str = "") -> pa.Table:
    """Stamp this stage's provenance onto `table` and return the result.

    Threads root provenance (`source_rowid`), (re)stamps `stage`, re-stamps the consume-layer
    `lineage` document, and re-declares the destination's canonical name.

    THE INHERITANCE RULE IS THE SAME FOR ALL THREE, and it is the reason they are one function: an
    absent value DROPS what the upstream carried rather than passing it on. The parent's document
    describes the parent's run and the parent's id names the parent's dataset, so leaving either on a
    child is a claim about the wrong object — and the child's readers cannot tell an inherited value
    from a declared one.
    """
    out = carry_source_rowid(table)
    out = _set_or_append(out, pa.field(STAGE_COLUMN, pa.string()), pa.array([stage] * out.num_rows, pa.string()))
    if lineage:
        document = pa.array([lineage] * out.num_rows, pa.string())
        out = _set_or_append(out, pa.field(LINEAGE_COLUMN, pa.json_()), document.cast(pa.json_()))
    elif LINEAGE_COLUMN in out.column_names:
        out = out.drop_columns([LINEAGE_COLUMN])
    return declare_dataset_id(out, dataset_id)


def ensure_declared_dataset_id(uri: str, dataset_id: str, storage_options: dict[str, str] | None = None) -> bool:
    """Correct an EXISTING dataset's declared name in place. Returns whether it wrote.

    THE STAMP ALONE IS NOT ENOUGH, and measuring the destination rather than the call is what showed
    it: `merge_insert` does not carry the source table's schema metadata onto the dataset (pylance
    10.0.0 — a dataset created declaring `acme$bronze` still declared `acme$bronze` after a full-sync
    merge of a table declaring `acme$silver`; only an `overwrite` moved it). The cascade's steady-state
    write is deliberately that merge, because an overwrite re-mints every `_rowid` and the tier above
    resolves `source_rowid` against them. So without this, a corrected stamp would land only on tiers
    created after it and every tier already on disk would keep its parent's name forever, repaired by
    no re-run.

    `update_schema_metadata` is a metadata-only commit: it touches one key, preserves the rest, rewrites
    no data and re-mints no row id. Idempotent by the read below, so it is safe on every cascade tick
    and the estate self-heals rather than needing a backfill pass.
    """
    import lance

    if not dataset_id:
        return False
    dataset = lance.dataset(uri, storage_options=storage_options)
    current = (dataset.schema.metadata or {}).get(LINEAGE_DATASET_ID_KEY.encode())
    if current == dataset_id.encode():
        return False
    dataset.update_schema_metadata({LINEAGE_DATASET_ID_KEY: dataset_id})
    return True


__all__ = [
    "CARDINALITIES",
    "LINEAGE_COLUMN",
    "LINEAGE_DATASET_ID_KEY",
    "ONE_TO_MANY",
    "ONE_TO_ONE",
    "SOURCE_ROWID_COLUMN",
    "STAGE_COLUMN",
    "carry_source_rowid",
    "declare_dataset_id",
    "ensure_declared_dataset_id",
    "stamp_stage",
]
