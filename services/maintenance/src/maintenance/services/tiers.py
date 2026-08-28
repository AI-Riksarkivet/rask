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

from service_kit.lakehouse.naming import CATALOG_DELIMITER


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


#: The one parent directory whose CHILD is the namespace rather than the table. The medallion cascade
#: writes `s3://<bucket>/medallion/<tier>` — the dataset IS the tier — so `medallion` promotes its child
#: instead of being read as the namespace itself. Kept to a single literal on purpose: every other
#: parent segment must stay untrusted, or a table named `gold` would silently size itself as gold.
_CASCADE_PARENT: Final = "medallion"


def _tier_from_namespace(namespace: str) -> str | None:
    """The tier encoded in a CATALOG namespace segment, or ``None``.

    `project_namespace` composes `<project>-<tier>` (`acme-bronze`), and a bare `silver` is the
    single-project form, so both shapes reduce from the RIGHT.
    """
    tier = namespace.rsplit("-", 1)[-1] if "-" in namespace else namespace
    return tier if tier in _BY_TIER else None


def _tier_from_cascade_namespace(namespace: str) -> str | None:
    """The tier encoded in a MEDALLION namespace segment, or ``None``.

    THE OPPOSITE END from `_tier_from_namespace`, and the distinction is load-bearing rather than
    cosmetic. The cascade runs several lanes side by side and names each `<tier>-<lane>`:
    `bronze-media` -> `silver-media` (chart/values.yaml:957), `bronze` -> `gold-htr` (:966), plus the
    plain `bronze` -> `silver` -> `gold` (:949-950) and the live `bronze-pages`. So the tier LEADS here,
    while the catalog's `<project>-<tier>` trails.

    Reducing a cascade namespace from the right yields the LANE (`bronze-media` -> `media`), which is no
    tier, so the dataset silently falls back to Lance's default sizing — the exact failure this module
    exists to prevent, and the one `bronze-pages` hit.
    """
    tier = namespace.split("-", 1)[0]
    return tier if tier in _BY_TIER else None


def tier_of(dataset_uri: str) -> str | None:
    """The medallion tier this dataset lives in, read from the NAMESPACE — in whichever of the three
    layouts the estate actually writes.

    The tier is always a property of the namespace and never of the table name: matching the table
    would mis-tier a `gold_summary` that legitimately lives in silver, and mis-sizing is silent. What
    changed 2026-08-16 is WHERE the namespace sits, because reading only `parts[-2]` turned out to
    resolve almost nothing in production:

      1. ``<bucket>/<project>-<tier>/<table>``      — nested; the namespace is `parts[-2]` (original).
      2. ``<bucket>/medallion/<tier>``              — the CASCADE. `parts[-2]` is the literal
         `medallion`, so every governed tier read as untiered and the #61 defaults never once applied
         to a medallion dataset. Measured live: bronze, silver and gold all returned None.
      3. ``<bucket>/<uuid8>_<namespace>$<table>``   — the `dir` backend's FLAT layout, which does not
         nest a table under its namespace at all but encodes both in ONE directory name. Here
         `parts[-2]` is the BUCKET, so catalog-governed tables read as untiered too.

    Layout 3 is matched on the delimiter rather than the uuid prefix, and the prefix is stripped with a
    single `split("_", 1)` so a namespace that itself contains an underscore (`transcripts_v2`) survives
    intact — that one must stay untiered, which is what keeps this from degenerating into "find a tier
    word anywhere in the path".

    ``None`` when the URI names no tier. That is a real case (a control-plane dataset, an untiered
    namespace) and must not be guessed at — see `target_rows_for`.
    """
    parts = [segment for segment in dataset_uri.split("://")[-1].split("/") if segment]
    if len(parts) < 2:
        return None

    leaf = parts[-1]
    if CATALOG_DELIMITER in leaf:  # layout 3 — flat: the namespace is the leaf's own prefix, not a parent directory
        namespace = leaf.split(CATALOG_DELIMITER, 1)[0]
        # Strip the dir backend's uuid8 prefix, once: `aa3bed10_silver` -> `silver`.
        return _tier_from_namespace(namespace.split("_", 1)[1] if "_" in namespace else namespace)

    if parts[-2] == _CASCADE_PARENT:  # layout 2 — the cascade: the child IS the namespace
        return _tier_from_cascade_namespace(leaf)

    return _tier_from_namespace(parts[-2])  # layout 1 — nested


def target_rows_for(dataset_uri: str) -> int | None:
    """The default ``target_rows_per_fragment`` for this dataset's tier, or ``None`` to let Lance decide.

    ``None`` is deliberate rather than a fallback constant: for a URI whose tier cannot be read,
    inventing a number is worse than deferring. Lance's own sizing is a reasonable default, whereas a
    wrong explicit value is applied silently and forever.

    A #50 policy record overrides this — the caller applies the policy after asking here.
    """
    tier = tier_of(dataset_uri)
    return _BY_TIER.get(tier) if tier else None
