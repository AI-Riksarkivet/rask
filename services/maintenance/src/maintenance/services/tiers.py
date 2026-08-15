"""#61 — per-tier compaction fragment sizing, because one row count cannot serve all three tiers.

TWO FORCES, PULLING OPPOSITE WAYS.

*Conflict detection is per-fragment.* Two writers touching different fragments commit cleanly; two
touching the same one conflict and retry. The annotator writes to SILVER concurrently with the
cascade, so smaller fragments there mean fewer collisions.

*Scan cost is per-file.* Smaller fragments mean more files, more metadata and more round trips. GOLD
is published and read-heavy, so it wants the opposite.

AND THE ROW WIDTH DIFFERS BY ~1000x, which is what makes a single number wrong everywhere rather than
merely suboptimal. A bronze page-image row is ~1.8 MB — the figure the chart's own `scanBatchSize`
comment is built on, and the reason Lance's 8192-row default was an OOM here — while silver and gold
rows are ordinary columnar records at ~2 KB. One `target_rows_per_fragment` therefore produces a
~1.8 GB fragment in bronze and a few MB in gold.

THE NUMBERS. Target is a fragment in the high-hundreds-of-MB range: large enough that per-file
overhead is noise, small enough to stay well inside the pod's memory when compaction reads it.

======  ==========  =========  ========  =====================================================
tier    rows        row size   fragment  why
======  ==========  =========  ========  =====================================================
bronze  512         ~1.8 MB    ~0.9 GB   append-only from ingest; no concurrent writer, so
                                         conflict pressure is nil and SIZE is the only axis
silver  262 144     ~2 KB      ~0.5 GB   the annotator writes here concurrently — the one tier
                                         where conflict pressure decides, so it takes the
                                         smallest fragment by BYTES of the three
gold    524 288     ~2 KB      ~1.0 GB   published, read-heavy, few writers; favour scan
                                         efficiency over write concurrency
======  ==========  =========  ========  =====================================================

**These are DEFAULTS, not a measurement of this estate.** The row widths are the chart's working
figures rather than a profile of production data, so the numbers are a defensible starting point and
an owner may well want different ones. A #50 policy record still wins over everything here, which
makes retuning a config change rather than a deploy — and is why shipping a default is better than
shipping nothing and letting Lance size a 1.8 MB-row tier by a row count meant for narrow data.
"""

from __future__ import annotations

from typing import Final


#: ~512 x 1.8 MB. Bronze rows are page images; a row count meant for narrow data produces fragments
#: measured in tens of GB here, which is the OOM `scanBatchSize` already had to be bounded for.
BRONZE_TARGET_ROWS: Final = 512

#: ~262k x 2 KB. The SMALLEST fragment by bytes, on purpose: this is the tier the annotator writes
#: to concurrently with the cascade, and conflict detection is per-fragment.
SILVER_TARGET_ROWS: Final = 262_144

#: ~524k x 2 KB. Published and read-heavy — larger fragments cut per-file overhead on scans, and the
#: write concurrency that argues for small fragments in silver is largely absent here.
GOLD_TARGET_ROWS: Final = 524_288

_BY_TIER: Final = {"bronze": BRONZE_TARGET_ROWS, "silver": SILVER_TARGET_ROWS, "gold": GOLD_TARGET_ROWS}


def tier_of(dataset_uri: str) -> str | None:
    """The medallion tier this dataset lives in, read from the NAMESPACE segment.

    `project_namespace` composes `<project>-<tier>` (`acme-bronze`), so the tier is a property of the
    namespace and never of the table name. Matching the table name instead would mis-tier a
    `gold_summary` table that legitimately lives in silver — and mis-sizing is silent.

    ``None`` when the URI does not name a tier, which is a real case (a control-plane dataset, or a
    single-tenant deployment with no tier suffix) and must not be guessed at.
    """
    parts = [segment for segment in dataset_uri.split("://")[-1].split("/") if segment]
    if len(parts) < 2:
        return None
    namespace = parts[-2]
    tier = namespace.rsplit("-", 1)[-1] if "-" in namespace else namespace
    return tier if tier in _BY_TIER else None


def target_rows_for(dataset_uri: str) -> int | None:
    """The default ``target_rows_per_fragment`` for this dataset's tier, or ``None`` to let Lance decide.

    ``None`` is deliberate rather than a fallback constant: for a URI whose tier cannot be read,
    inventing a number is worse than deferring. Lance's own sizing is a reasonable default, whereas a
    wrong explicit value is applied silently and forever.

    A #50 policy record overrides this — the caller applies the policy after asking here.
    """
    tier = tier_of(dataset_uri)
    return _BY_TIER.get(tier) if tier else None
