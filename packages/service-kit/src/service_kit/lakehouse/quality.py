"""Automated data-quality assertions — the gate every publication runs.

Lives in `service_kit.lakehouse` rather than in one service because § D-R1 makes the gate a property
of PUBLICATION, not of the medallion: a version becomes consumable only after it passes, whoever
wrote it — a gate that only one writer can run is a gate every other writer has to reimplement, which
is how the estate ends up with two definitions of "good enough".

The catalog's `publish` door runs cheap, exact assertions on the version being published — does it have
rows? is the key column free of nulls? — in the shape of the standard OpenLineage
``dataQualityAssertions`` facet, and a failed assertion BLOCKS the publication, so bad data never becomes
consumable.

This is the automated *validator* half of governance, and it composes with the FGA gate: FGA decides who
MAY promote (a registered identity holding the role); quality decides whether the DATA is good enough to.
Both gate movement. The checks use ``count_rows`` (with a filter) so they never materialise the table.
"""

from __future__ import annotations

from typing import Final

import lance
import pyarrow as pa
from pydantic import BaseModel, model_validator

from service_kit.lakehouse import blobs
from service_kit.lakehouse.stage_stamp import SOURCE_ROWID_COLUMN


#: Assertion names — stable identifiers the ``dataQualityAssertions`` facet carries (and the gate keys on).
ROW_COUNT_POSITIVE = "row_count_positive"
NOT_NULL = "not_null"
BLOB_RESOLVES = "blob_resolves"
COLUMN_DECLARED = "column_declared"

#: Findings NO approval can wave through: the data is wrong, not merely unusual. A null key means a
#: broken join or transform; an unresolvable blob pointer means the payload is gone. Neither becomes
#: correct because somebody signed off.
#:
#: The catalog's publish door refuses to ACT on these however an approval names them, so no caller can
#: publish corrupt data by signing off on it.
STRUCTURAL_ASSERTIONS: frozenset[str] = frozenset({NOT_NULL, BLOB_RESOLVES})

#: The spec's two severity values (`DataQualityAssertionsDatasetFacet`). `error` means the failure
#: blocks the pipeline, `warn` means it does not — the distinction `STRUCTURAL_ASSERTIONS` already
#: draws, named in the vocabulary a standard consumer reads.
ERROR: Final = "error"
WARN: Final = "warn"


#: The provenance contract's verdict name. Reported as an assertion like every other gate answer, so a
#: refusal is auditable in the same list a caller already reads rather than arriving as a bare error.
PROVENANCE: Final = "provenance_complete"


def tier_contract_violations(schema: pa.Schema, *, has_stable_row_ids: bool | None = None) -> list[str]:
    """Why this schema's `source_rowid` cannot name the row it descends from — empty when it can, or is absent.

    A governed row needs no column (ADR 0156 C9): its run and inputs are the commit's transaction properties
    ([[LH-225]]), and `stage` and `lineage` are the writer's own columns. `source_rowid` is the one column the
    platform names, an opt-in a pipeline writes so impact analysis can trace a row to the bronze row it came
    from; a table that carries it must be able to back it.
    """
    if SOURCE_ROWID_COLUMN not in schema.names:
        return []
    problems: list[str] = []
    # A `source_rowid` of the wrong width is a DIFFERENT column wearing the right name, and it is the failure a
    # reader is least likely to see: present, non-null, and passing every count-based check. It is uint64
    # because that is what Lance's own stable row id is.
    actual = schema.field(SOURCE_ROWID_COLUMN).type
    if not pa.types.is_uint64(actual):
        problems.append(f"{SOURCE_ROWID_COLUMN!r} is {actual}, not uint64 — the width Lance's stable row id uses")
    # `source_rowid` holds a Lance STABLE row id, and `enable_stable_row_ids` is CREATE-TIME ONLY — set later it is
    # a silent no-op. A dataset created without it carries a well-typed column of values that are not stable:
    # measured on the estate, after compaction the ids moved from `0..5` to `4294967296..`, naming rows that no
    # longer exist. `None` means the caller could not read the property and is not a violation: refusing on an
    # unreadable property would fail closed against callers who cannot answer.
    if has_stable_row_ids is False:
        problems.append("the dataset was created without stable row ids, so `source_rowid` can never name a row that survives compaction")
    return problems


class Assertion(BaseModel):
    """One data-quality check on a produced dataset (the OpenLineage ``dataQualityAssertions`` shape).

    ``severity`` is DERIVED, never passed. The spec's values are ``error`` (the failure blocks the
    pipeline) and ``warn`` (it does not), and this estate already draws that line once, in
    :data:`STRUCTURAL_ASSERTIONS` — findings no approval can wave through. Deriving it from that set
    rather than taking it as an argument is what keeps the wire and the gate from disagreeing about
    which failures are blocking: a caller cannot mark a structural finding as a warning, and a new
    structural assertion becomes ``error`` on the wire by being added to the set.

    Without it a standard consumer reads a list of `success: false` with no way to tell a broken join
    from an unusual-but-accepted one — the two outcomes this estate treats most differently. A caller may
    label a non-structural check; nothing may downgrade a structural one.
    """

    assertion: str
    success: bool
    column: str | None = None
    severity: str | None = None

    @model_validator(mode="after")
    def _derive_severity(self) -> Assertion:
        """STRUCTURAL ALWAYS WINS. A caller may label a non-structural check, but nothing may downgrade
        one the gate refuses to waive — an `error` that arrives as `warn` tells a consumer the pipeline
        continued past a broken join."""
        if self.assertion in STRUCTURAL_ASSERTIONS:
            object.__setattr__(self, "severity", ERROR)
        elif self.severity is None:
            object.__setattr__(self, "severity", WARN)
        return self


def assert_quality(
    uri: str,
    storage_options: dict[str, str],
    *,
    key_column: str,
    required_columns: tuple[str, ...] | list[str] = (),
    version: int | None = None,
    session: lance.Session | None = None,
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
    ds = lance.dataset(uri, version=version, storage_options=storage_options, session=session)
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
