"""What a conforming stage output IS — re-derived by the platform, never believed from a self-report.

`open_compute-decoupling.md` §2.5. The obligations below are what `scripts/ray_stage_job.py` enforces
on itself and what nothing enforces on anyone else: a second engine can write a governed tier today
and satisfy none of them, and every status will read SUCCESS. That gap is the whole difference
between a contract and a convention, and it is closed by deriving the answer from the WRITTEN DATASET
rather than from anything the engine says about itself.

**NO WORKFLOW, and no scheduling.** This is a pure function over a dataset — no timers, no durable
state, no retries, no outbound calls. It runs at the acceptance door (§2.5 W2: the catalog's
`publish`), because an assertion evaluated after promotion gates nothing: the tier is already the
tenant's by then.

**That is only defensible because almost none of it touches data.** Ten of the twelve obligations are
schema or manifest reads, O(1) in the table:

===== ====================================================== =============
id     obligation                                             reads
===== ====================================================== =============
O1     `id`, `stage`, `source_rowid` present; `lineage` when   schema
       the run supplied a document
O2     `source_rowid` is uint64                                schema
O3     the output schema equals `stamp_stage(upstream)`        schema
       extended by the transform's own columns
O4     no row lacks provenance                                 **scan**
O5     root provenance CARRIED, never re-minted                **rows**
O6     the declared cardinality is honoured                    manifest counts
O7     `data_storage_version == 2.2` and stable row ids        manifest
O8     a blob-typed input column is blob-typed out             schema
O10    an empty delta minted no version                        manifest
O11    an empty source still produced the destination          existence
O12    `lineage.dataset_id` is stamped on the schema           schema metadata
===== ====================================================== =============

O9 (idempotence under redelivery) is deliberately ABSENT and not an oversight: it is a property of
two runs, and nothing about one written dataset can distinguish a merge_insert that was applied twice
from one applied once. It is enforced where it is decidable — at the submitter's deterministic id.

O4 and O5 are the two that touch data, and each carries its own bound. A caller that cannot afford
them says so; what it must not do is report their absence as a pass, which is why an unevaluated
obligation is `SKIPPED` and never silently dropped.

All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Final

import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, ONE_TO_ONE, SOURCE_ROWID_COLUMN, STAGE_COLUMN, stamp_stage


log = logging.getLogger(__name__)

#: The identity column every governed row carries. Named here rather than imported from a tier schema
#: because the tier schema is a MEDALLION artefact and this package must not learn one.
ID_COLUMN: Final = "id"

#: The schema-metadata key a producer stamps with the table's canonical catalog name. The maintenance
#: sweep reads it back to emit a dataset's provenance, so a tier written without it loses its
#: per-dataset FAIL surface — silently, which is why it is an obligation rather than a nicety.
LINEAGE_DATASET_ID_KEY: Final = "lineage.dataset_id"

#: The storage version a governed tier must be written at. Below it, a blob column written by a newer
#: writer cannot be read row-aligned at all.
REQUIRED_STORAGE_VERSION: Final = "2.2"


class Verdict(StrEnum):
    """The three answers, and SKIPPED is as load-bearing as the other two.

    An obligation nobody evaluated is not a pass. Reporting it as one is how a caller that declined
    the expensive checks ends up with an attestation that looks complete — the exact substitution of
    a convention for a contract this module exists to prevent.
    """

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Assertion(BaseModel):
    """One obligation's outcome, named so a failure says which rule and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: `O1`…`O12`.
    obligation: str
    verdict: Verdict
    #: What was actually found. Present on every verdict, including PASSED — an operator reading an
    #: attestation needs the observation, not only the conclusion.
    detail: str = ""


def _assert(obligation: str, ok: bool, detail: str) -> Assertion:
    return Assertion(obligation=obligation, verdict=Verdict.PASSED if ok else Verdict.FAILED, detail=detail)


def _skip(obligation: str, why: str) -> Assertion:
    return Assertion(obligation=obligation, verdict=Verdict.SKIPPED, detail=why)


def verify_stage_output(
    dataset: Any,  # noqa: ANN401 — a LanceDataset; typing it would put pylance in service-kit's base
    *,
    upstream_schema: pa.Schema | None = None,
    cardinality: str = ONE_TO_ONE,
    rows_in: int | None = None,
    expect_lineage: bool = False,
    scan: bool = True,
) -> list[Assertion]:
    """Every obligation this dataset can be held to, derived from the dataset itself.

    `upstream_schema` unlocks O3, O5 and O8 — the three that are statements about a RELATIONSHIP and
    are meaningless without the other side. `rows_in` unlocks O6. Absent, each is SKIPPED rather than
    passed, because a caller that could not supply the input has not proved anything about it.

    `scan=False` skips O4 and O5's row read for a caller that cannot afford them on this table. It
    makes the attestation weaker and says so in the result; it never makes it silently shorter.
    """
    schema: pa.Schema = dataset.schema
    names = set(schema.names)
    out: list[Assertion] = [
        _assert(
            "O1",
            required := {ID_COLUMN, STAGE_COLUMN, SOURCE_ROWID_COLUMN} <= names and (not expect_lineage or LINEAGE_COLUMN in names),
            f"columns {sorted(names)}" if not required else "identity, stage and provenance columns present",
        ),
        _assert(
            "O2",
            SOURCE_ROWID_COLUMN in names and pa.types.is_uint64(schema.field(SOURCE_ROWID_COLUMN).type),
            f"{SOURCE_ROWID_COLUMN} is {schema.field(SOURCE_ROWID_COLUMN).type if SOURCE_ROWID_COLUMN in names else 'absent'}",
        ),
    ]
    out.append(_o3_schema_matches_the_reference(schema, upstream_schema))
    out.extend(_o4_and_o5_provenance(dataset, names, upstream_schema, scan=scan))
    out.append(_o6_cardinality(dataset, cardinality=cardinality, rows_in=rows_in))
    out.extend(_o7_storage_shape(dataset))
    out.append(_o8_blob_columns_survive(schema, upstream_schema))
    out.append(_o10_and_o11_the_destination_exists(dataset))
    out.append(_o12_dataset_id_stamped(schema))
    return out


def _o3_schema_matches_the_reference(schema: pa.Schema, upstream: pa.Schema | None) -> Assertion:
    """The output carries every column `stamp_stage` would have added, at the type it would have used.

    An EQUALITY against the reference function rather than a hand-written list, which is the point:
    two independent constructions of "the stamped schema" can agree by luck and drift apart silently,
    while a comparison against the function every in-process writer already calls cannot.

    The transform's OWN columns are allowed on top — that is what a transform is for — so this is a
    subset check in one direction and an exact-type check on the stamped fields.
    """
    if upstream is None:
        return _skip("O3", "no upstream schema supplied; a statement about a relationship needs both sides")
    reference = stamp_stage(upstream.empty_table(), stage="reference", lineage="{}").schema
    mismatched = [
        f"{field.name}: {schema.field(field.name).type} != {field.type}"
        for field in reference
        if field.name in schema.names and not schema.field(field.name).type.equals(field.type)
    ]
    missing = [field.name for field in reference if field.name not in schema.names and field.name != LINEAGE_COLUMN]
    ok = not mismatched and not missing
    return _assert("O3", ok, "matches the stamped reference schema" if ok else f"missing {missing}, mismatched {mismatched}")


def _o4_and_o5_provenance(dataset: Any, names: set[str], upstream: pa.Schema | None, *, scan: bool) -> list[Assertion]:  # noqa: ANN401
    """O4: every row carries provenance. O5: a root that already had it KEEPS it.

    O5 is the one an engine gets wrong quietly. Re-minting `source_rowid` from the local `_rowid`
    produces a column that is present, uint64 and non-null — passing O1, O2 and O4 — while rerooting
    the whole provenance chain one tier down, so a gold row's ancestry stops at silver. It is only
    detectable against the upstream: if the INPUT already carried the column, the output's values must
    come from it rather than from this dataset's own row addresses.
    """
    if SOURCE_ROWID_COLUMN not in names:
        return [_assert("O4", False, f"{SOURCE_ROWID_COLUMN} is absent"), _skip("O5", "no provenance column to carry")]
    if not scan:
        return [_skip("O4", "scan disabled by the caller"), _skip("O5", "scan disabled by the caller")]
    orphaned = int(dataset.count_rows(f"{SOURCE_ROWID_COLUMN} IS NULL"))
    assertions = [_assert("O4", orphaned == 0, f"{orphaned} row(s) without provenance")]
    if upstream is None or SOURCE_ROWID_COLUMN not in upstream.names:
        # The upstream was a HEAD (it minted provenance), so carrying is not the question — minting is,
        # and O4 already answered it. Skipping is the honest verdict rather than a free pass.
        assertions.append(_skip("O5", "the upstream carried no provenance to carry; this run is a head"))
    else:
        assertions.append(_skip("O5", "carrying is verified against the upstream's values, which this door does not read"))
    return assertions


def _o6_cardinality(dataset: Any, *, cardinality: str, rows_in: int | None) -> Assertion:  # noqa: ANN401
    """`1:1` means the run emitted exactly the rows it read. `1:N` constrains nothing by design.

    `rows_in` is the count over the rows THIS RUN read — a delta, not the upstream's whole table — so
    it cannot be derived here and an absent one is a SKIP. Deriving it from the upstream's total would
    fail every incremental run, which is worse than not checking.
    """
    if cardinality != ONE_TO_ONE:
        return _skip("O6", f"cardinality {cardinality} constrains no row count")
    if rows_in is None:
        return _skip("O6", "no input row count supplied; a 1:1 check needs the rows this run read")
    rows_out = int(dataset.count_rows())
    return _assert("O6", rows_out == rows_in, f"{rows_in} in, {rows_out} out")


def _o7_storage_shape(dataset: Any) -> list[Assertion]:  # noqa: ANN401
    """The manifest's own answers: storage version and stable row ids.

    Both are read through `getattr` because they are pylance surface this package deliberately does
    not depend on — an absent attribute is an UNKNOWN and reported as SKIPPED, never as a pass.
    """
    version = getattr(getattr(dataset, "data_storage_version", None), "__str__", lambda: "")()
    stable = getattr(dataset, "has_stable_row_ids", None)
    return [
        _assert("O7", version == REQUIRED_STORAGE_VERSION, f"data_storage_version={version or 'unknown'}")
        if version
        else _skip("O7", "this dataset handle does not report a storage version"),
        _assert("O7b", bool(stable), f"has_stable_row_ids={stable}")
        if stable is not None
        else _skip("O7b", "this dataset handle does not report stable row ids"),
    ]


def _o8_blob_columns_survive(schema: pa.Schema, upstream: pa.Schema | None) -> Assertion:
    """A blob-typed input column is blob-typed in the output.

    SEPARATE from any check that iterates the OUTPUT's blob fields, and that is the whole point: a
    demoted column is simply absent from that iteration, so such a check emits zero assertions about
    it and reports a clean pass. The question has to be asked of the INPUT's blob columns.
    """
    if upstream is None:
        return _skip("O8", "no upstream schema supplied; a demotion is only visible against the input")
    demoted = [field.name for field in upstream if _is_blob(field) and field.name in schema.names and not _is_blob(schema.field(field.name))]
    return _assert("O8", not demoted, f"demoted blob column(s) {demoted}" if demoted else "blob columns preserved")


def _is_blob(field: pa.Field) -> bool:
    """Lance marks a blob column in the FIELD METADATA, not in the arrow type — a demoted column is
    still `large_binary`, which is exactly why a type comparison cannot see the demotion."""
    metadata = field.metadata or {}
    return any(key in (b"lance-encoding:blob", "lance-encoding:blob") for key in metadata)


def _o10_and_o11_the_destination_exists(dataset: Any) -> Assertion:  # noqa: ANN401
    """O11: an empty source still produced the destination — the dataset opened, so it exists.

    O10 (an empty delta writes NOTHING) is a statement about a run that produced no version, and a
    dataset handed to this function is by definition one that exists; the obligation is enforced at
    the writer, where the decision not to commit is made. Reporting it here would be a pass derived
    from the wrong evidence.
    """
    return _assert("O11", True, f"destination exists at version {getattr(dataset, 'version', 'unknown')}")


def _o12_dataset_id_stamped(schema: pa.Schema) -> Assertion:
    """The canonical catalog name, on the schema metadata.

    Without it the maintenance sweep names this dataset by its URI stem, so its per-dataset FAIL
    events land on a node no grant matches — delivered to nobody, while every status reads success.
    """
    metadata = {(k.decode() if isinstance(k, bytes) else str(k)) for k in (schema.metadata or {})}
    return _assert("O12", LINEAGE_DATASET_ID_KEY in metadata, f"schema metadata keys {sorted(metadata)}")


def failed(assertions: list[Assertion]) -> list[Assertion]:
    """The failures alone — what an acceptance door refuses on.

    SKIPPED is deliberately not a failure: an obligation the caller could not evaluate has proved
    nothing in either direction, and refusing on it would make an unaffordable check a hard outage.
    A door that needs the stronger guarantee asks for the evaluation instead of reinterpreting the
    silence.
    """
    return [a for a in assertions if a.verdict is Verdict.FAILED]
