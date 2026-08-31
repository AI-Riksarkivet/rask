"""Automated data-quality assertions — the gate every publication runs.

Lives in `service_kit.lakehouse` rather than in one service because § D-R1 makes the gate a property
of PUBLICATION, not of the medallion: a version becomes consumable only after it passes, whoever
wrote it. It started in `medallion/services/quality.py` and moved here the moment a second caller
(the catalog's `publish`) needed it — a gate that only one writer can run is a gate every other
writer has to reimplement, which is how the estate ends up with two definitions of "good enough".

A medallion stage that produced a real Lance dataset (compute on) can be VALIDATED before it promotes: the
mover runs cheap, exact assertions on the dataset it just wrote — does it have rows? is the key column free
of nulls? — and emits them as the standard OpenLineage ``dataQualityAssertions`` facet. When the quality
GATE is enabled, a failed assertion BLOCKS promotion: the failed run is still recorded (lineage keeps the
assertions, so the bad batch is auditable) but the next stage is never triggered, so bad data can't cascade.

This is the automated *validator* half of governance, and it composes with the FGA gate: FGA decides who
MAY promote (a registered identity holding the role); quality decides whether the DATA is good enough to.
Both gate movement. The checks use ``count_rows`` (with a filter) so they never materialise the table.
"""

from __future__ import annotations

from typing import Final

import lance
import pyarrow as pa
from pydantic import BaseModel

from service_kit.lakehouse import blobs
from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, SOURCE_ROWID_COLUMN, STAGE_COLUMN


#: Assertion names — stable identifiers the ``dataQualityAssertions`` facet carries (and the gate keys on).
ROW_COUNT_POSITIVE = "row_count_positive"
NOT_NULL = "not_null"
BLOB_RESOLVES = "blob_resolves"
COLUMN_DECLARED = "column_declared"

#: Findings NO approval can wave through: the data is wrong, not merely unusual. A null key means a
#: broken join or transform; an unresolvable blob pointer means the payload is gone. Neither becomes
#: correct because somebody signed off.
#:
#: SHARED because it is enforced twice, and both points must agree on what "structural" means: the
#: medallion's review refuses to ASK about these, and the catalog's publish door refuses to ACT on
#: them — so a caller that bypasses the workflow cannot publish corrupt data either. One definition,
#: two enforcement points, the same shape `blob_column_resolves` already has.
STRUCTURAL_ASSERTIONS: frozenset[str] = frozenset({NOT_NULL, BLOB_RESOLVES})


#: The provenance contract's verdict name. Reported as an assertion like every other gate answer, so a
#: refusal is auditable in the same list a caller already reads rather than arriving as a bare error.
PROVENANCE: Final = "provenance_complete"

#: The three columns that make a row traceable. Named here rather than imported from the medallion,
#: because the rule is the PLATFORM's and a service must not be the authority on what governs it.
_TIER_PROVENANCE_COLUMNS: frozenset[str] = frozenset({STAGE_COLUMN, LINEAGE_COLUMN, SOURCE_ROWID_COLUMN})


def tier_contract_violations(schema: pa.Schema, *, has_stable_row_ids: bool | None = None) -> list[str]:
    """Why this schema is not a conforming governed tier — empty when it is, or does not claim to be.

    A governed row carries `stage` (which tier), `lineage` (the run that produced it) and
    `source_rowid` (the bronze row it descends from). Owner ruling D1, 2026-08-31: honest provenance is
    MANDATORY, because the case it serves is impact analysis — one document is corrupted at ingest, and
    "which rows downstream are contaminated?" must not answer confidently and wrongly.

    OPT-IN BY CLAIM, and that is the whole reason this is safe to apply at a door every publish passes
    through. A table carrying NONE of the three is not a governed tier — a registered external dataset,
    a user's own table — and is left alone. A table carrying ANY of them is claiming to be one, and must
    then carry all three, correctly typed. That rule refuses exactly the shape that ships today
    (`source_rowid` present, `stage` and `lineage` absent) without touching a plain table.

    STRUCTURAL, not referential, and deliberately so: `publish` is handed a table id and a version and
    does not know the parent table, so "does every value name a real parent row?" cannot be asked here
    without new plumbing. It does not need to be — the referential half already runs in the stage job,
    where both datasets are open. What the job CANNOT do is notice an absent column: it counts
    parentless rows as `count_rows(filter="source_rowid IS NULL") if the column is in the schema else 0`,
    so a table that drops the column reports zero and passes. Structure is the half that was missing.
    """
    names = set(schema.names)
    claimed = names & _TIER_PROVENANCE_COLUMNS
    if not claimed:
        return []

    problems = [f"missing {column!r}" for column in sorted(_TIER_PROVENANCE_COLUMNS - names)]

    # A `source_rowid` of the wrong width is a DIFFERENT column wearing the right name, and it is the
    # failure a reader is least likely to see: present, non-null, and passing every count-based check.
    # `stamp_stage` mints uint64 because that is what Lance's own stable row id is.
    if SOURCE_ROWID_COLUMN in names:
        actual = schema.field(SOURCE_ROWID_COLUMN).type
        if not pa.types.is_uint64(actual):
            problems.append(f"{SOURCE_ROWID_COLUMN!r} is {actual}, not uint64 — the width Lance's stable row id uses")

    # THE DEEPER FAILURE THE COLUMNS CANNOT SHOW. `source_rowid` holds a Lance STABLE row id, and
    # `enable_stable_row_ids` is CREATE-TIME ONLY — set later it is a silent no-op. So a dataset
    # created without it carries a perfectly well-typed column of values that are not stable, and the
    # estate measured the cost: after compaction the ids moved from `0..5` to `4294967296..`, silently
    # naming rows that no longer exist. Every impact-analysis query over that tier answers confidently
    # and wrongly, which is the outcome ruling D1 exists to prevent.
    #
    # `None` means the caller could not read the property (a schema-only check, or a backend that does
    # not expose it) and is treated as "not asserted" rather than as a violation — refusing on an
    # unreadable property would fail closed against callers who cannot answer, which is a different
    # and worse failure than the one being prevented.
    if has_stable_row_ids is False:
        problems.append("the dataset was created without stable row ids, so `source_rowid` can never name a row that survives compaction")
    return problems


class Assertion(BaseModel):
    """One data-quality check on a produced dataset (the OpenLineage ``dataQualityAssertions`` shape)."""

    assertion: str
    success: bool
    column: str | None = None


def assert_quality(
    uri: str,
    storage_options: dict[str, str],
    *,
    key_column: str,
    required_columns: tuple[str, ...] | list[str] = (),
    version: int | None = None,
) -> list[Assertion]:
    """Run cheap, exact quality assertions on the Lance dataset at ``uri``.

    ``version`` names the version to scan; ``None`` means latest, which is right for a caller that
    just wrote. The publish gate is the caller that is NOT in that position — it tags a specific
    version while another writer may have committed since — and it used to pass only ``candidate.uri``,
    so the pin it had just taken was discarded here and the assertions ran against whatever was latest.
    Both directions were wrong, and the silent one is publishing a DIRTY version because a later clean
    one exists.

    - ``row_count_positive``: the dataset has at least one row (an empty promotion is a silent failure).
    - ``not_null`` on ``key_column``: the identity column has no nulls (a broken join/transform). Skipped
      (not failed) when the stage's data doesn't carry that column — different stages may key differently.
    - ``blob_resolves`` per blob-v2 column (§9 P2): the blob POINTERS actually dereference to bytes.
      A blob column can pass every tabular check while its payloads are gone — an external
      ``Blob.from_uri`` object deleted from under the table (the bucket-wipe case) fails only when
      someone finally reads it, far downstream of the promotion that let it through. Skipped (not
      failed) when the dataset has no blob column.

    The tabular checks use ``count_rows`` (with a filter for the null check) so the table is never
    materialised; the blob check reads ONE byte from the first and last rows' payloads per column.
    """
    ds = lance.dataset(uri, version=version, storage_options=storage_options)
    assertions = [Assertion(assertion=ROW_COUNT_POSITIVE, success=ds.count_rows() > 0)]
    if key_column and key_column in ds.schema.names:
        nulls = ds.count_rows(f"{key_column} IS NULL")
        assertions.append(Assertion(assertion=NOT_NULL, success=nulls == 0, column=key_column))
    # THE BREAKING-CHANGE DETECTOR (data-contract gap #1, 2026-07-12): each column a downstream
    # consumer DECLARED it reads must still exist in the just-written schema. Schema-on-write stays
    # completely free (additive evolution never blocked; the write itself always commits) — only the
    # PROMOTION of a version that dropped/renamed a declared column is stopped, turning what was a
    # runtime failure in the consumer's job into a pre-promotion contract violation here.
    for column in required_columns:
        assertions.append(Assertion(assertion=COLUMN_DECLARED, success=column in ds.schema.names, column=column))
    # The probe itself is SHARED with reconcile (service_kit.lakehouse.blobs.blob_column_resolves): the gate checks
    # pointers AT promotion; the reconcile sweep re-checks the already-promoted estate — one probe,
    # two enforcement points, so the two can never drift on what "resolves" means.
    for column in blobs.blob_field_names(ds.schema):
        assertions.append(Assertion(assertion=BLOB_RESOLVES, success=blobs.blob_column_resolves(ds, column), column=column))
    return assertions


def passed(assertions: list[Assertion]) -> bool:
    """Whether EVERY assertion succeeded — the gate promotes only when this is true."""
    return all(a.success for a in assertions)


def assert_quality_on_batch(
    table: pa.Table,
    *,
    key_column: str,
    required_columns: tuple[str, ...] | list[str] = (),
) -> list[Assertion]:
    """The SAME assertions, run on an uncommitted batch — D3's pre-commit gate.

    `assert_quality` above validates a dataset that already exists, which means the commit has
    already happened. `transform.py` calls it that way (:324-337, :462-468): a failed assertion
    leaves the commit in place, skips the next-stage trigger, and emits COMPLETE-with-failed-
    assertions — no FAIL run, no DLQ. So today a bad batch IS in the tier; it merely does not
    propagate. Anyone reading that table directly, or any consumer resolving `latest` rather than
    the published tag, sees data the gate rejected.

    D3 closes that window: where a hop's delta is a single transaction — every mover hop, and
    ingest's one commit — the assertions run on the batch BEFORE it is committed, so a rejected
    batch never becomes a version at all. Uncommitted fragments are invisible until commit
    (`lance_docs/guide.md:1533-1636`), which is what makes this possible rather than merely
    desirable.

    Deliberately the same assertion NAMES and the same `passed()`, so the pre-commit gate and the
    post-publish monitor cannot drift on what "good" means — the reason `blob_column_resolves` is
    already shared between the gate and the reconcile sweep.

    The blob check is absent here by necessity, not oversight: blob resolution is a property of
    stored payloads, and there are none until the commit. It remains a post-publish monitor
    (recorded via metadata-only commits, D3), which is the honest split — this gate catches
    structure, that one catches storage.
    """
    assertions = [Assertion(assertion=ROW_COUNT_POSITIVE, success=table.num_rows > 0)]
    if key_column and key_column in table.column_names:
        nulls = table.column(key_column).null_count
        assertions.append(Assertion(assertion=NOT_NULL, success=nulls == 0, column=key_column))
    for column in required_columns:
        assertions.append(Assertion(assertion=COLUMN_DECLARED, success=column in table.column_names, column=column))
    return assertions
