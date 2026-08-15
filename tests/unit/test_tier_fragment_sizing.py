"""#61 — one fragment size cannot be right for all three tiers, because they differ in both axes.

THE TWO FORCES, and they pull opposite ways:

  * **Conflict detection is PER-FRAGMENT.** Two writers touching different fragments commit cleanly;
    two touching the same one conflict and retry. The annotator writes to silver CONCURRENTLY with
    the cascade, so smaller fragments there mean fewer collisions.
  * **Scan cost is per-FILE.** Smaller fragments mean more files, more metadata, and more round trips
    on a read-heavy tier. Gold is published and mostly read, so it wants the opposite.

And the row WIDTH differs by an order of magnitude, which is what makes a single row-count wrong
everywhere rather than merely suboptimal: a bronze page-image row is ~1.8 MB (the figure the chart's
own `scanBatchSize` comment is built on), while silver/gold rows are ordinary columnar records. The
same `target_rows_per_fragment` produces a ~1.8 GB fragment in bronze and a few MB in gold.

THE NUMBERS, and the arithmetic behind each. Target is a fragment in the high-hundreds-of-MB range —
big enough that per-file overhead is noise, small enough to stay well inside the pod's memory when
compaction reads it:

    bronze     512 rows   x ~1.8 MB  =  ~0.9 GB   append-only from ingest; no concurrent writer, so
                                                  conflict pressure is nil and size is the only axis
    silver  262144 rows   x ~2 KB    =  ~0.5 GB   the annotator writes here concurrently — the one
                                                  tier where conflict pressure decides, so it takes
                                                  the SMALLEST fragment of the three by byte size
    gold    524288 rows   x ~2 KB    =  ~1.0 GB   published and read-heavy, few writers; favour
                                                  scan efficiency

These are DEFAULTS and a starting point, not a measurement of this estate — the row widths above are
the chart's own working figures, not a profile of production data. A #50 policy record still wins, so
a tier that turns out to want something else is a config change. Flagged for the owner rather than
presented as tuned.
"""

from __future__ import annotations

import pytest
from maintenance.services.tiers import GOLD_TARGET_ROWS, SILVER_TARGET_ROWS, target_rows_for


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("s3://wh/acme-bronze/pages.lance", 512),
        ("s3://wh/acme-silver/lines.lance", SILVER_TARGET_ROWS),
        ("s3://wh/acme-gold/alto.lance", GOLD_TARGET_ROWS),
    ],
)
def test_each_tier_gets_its_own_target(uri: str, expected: int) -> None:
    """The whole point: one number cannot serve a 1.8 MB row and a 2 KB row at once."""
    assert target_rows_for(uri) == expected


def test_bronze_gets_the_SMALLEST_row_count_because_its_rows_are_HUGE() -> None:
    """The relation, asserted rather than the literals alone.

    If someone raises bronze toward the silver/gold range, the fragment goes to hundreds of GB and
    compaction OOMs the pod — the same class of failure the `scanBatchSize` default exists to prevent.
    """
    bronze = target_rows_for("s3://wh/p-bronze/pages.lance")

    assert bronze is not None, "bronze resolved to no target at all — the tier is not being recognised"
    assert bronze < SILVER_TARGET_ROWS < GOLD_TARGET_ROWS
    assert bronze * 1.8 < 2048, "a bronze fragment should stay near a GB, not tens of GB"


def test_silver_is_SMALLER_than_gold_because_the_annotator_writes_there() -> None:
    """Conflict detection is per-fragment, so the tier with a concurrent writer takes smaller ones.

    Inverting this would make every annotator write contend with the cascade on the same fragment.
    """
    assert SILVER_TARGET_ROWS < GOLD_TARGET_ROWS


def test_an_UNRECOGNISED_tier_gets_None_so_LANCE_decides() -> None:
    """Not a guess. A URI whose tier we cannot read is exactly the case where inventing a number is
    worse than deferring: Lance's own sizing is a reasonable default, and a wrong explicit value is
    silently applied forever."""
    assert target_rows_for("s3://wh/something/else.lance") is None
    assert target_rows_for("") is None


def test_the_tier_is_read_from_the_NAMESPACE_not_the_table_name() -> None:
    """`project_namespace` puts the tier in the namespace segment (`acme-bronze`), and a table may
    legitimately be called anything. Matching on the table name would mis-tier a `gold_summary` table
    that lives in silver."""
    assert target_rows_for("s3://wh/acme-silver/gold_summary.lance") == SILVER_TARGET_ROWS
    assert target_rows_for("s3://wh/acme-gold/bronze_audit.lance") == GOLD_TARGET_ROWS
