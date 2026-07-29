"""Automated data-quality assertions for the medallion quality gate.

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

import lance
from pydantic import BaseModel

from service_kit.lakehouse import blobs


#: Assertion names — stable identifiers the ``dataQualityAssertions`` facet carries (and the gate keys on).
ROW_COUNT_POSITIVE = "row_count_positive"
NOT_NULL = "not_null"
BLOB_RESOLVES = "blob_resolves"
COLUMN_DECLARED = "column_declared"


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
) -> list[Assertion]:
    """Run cheap, exact quality assertions on the just-written Lance dataset at ``uri``.

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
    ds = lance.dataset(uri, storage_options=storage_options)
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
