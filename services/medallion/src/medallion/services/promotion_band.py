"""Is this promotion UNUSUAL? — the quality gate's missing question.

The assertions answer "is it broken". They cannot answer "is it strange", and the cascade design record
(`docs/architecture/medallion-cascade.md`) is about the gap between those two: a promotion whose row count doubled passes every assertion there
is, so before this module it was promoted silently and the review machinery never ran.

§9.1 decided the policy (2026-08-15): a row-count delta outside ±25%, plus every first promotion of a
dataset. The width is assumed rather than measured — nobody has looked at what a normal silver->gold
delta is on a real corpus — which is why it is a settings knob and why re-opening it needs a
measurement, not an opinion.

THE MEASUREMENT WAS ATTEMPTED (2026-08-23) AND CANNOT SETTLE IT, which is worth recording so the next
person does not repeat it. 81 consecutive medallion writes were read out of the live estate's lineage
(`outputStatistics.rowCount`, six datasets). The distribution is degenerate: 64 of 81 transitions are
EXACTLY 0.0% — the same 8-row fixture re-seeded — and the other 17 are all >=87%, the deliberate
re-seeds (8 -> 200, 200 -> 1000, 1000 -> 8, 8 -> 900). Nothing at all falls between 10% and 87%.

So on this data a band of 10%, 25% or 50% asks on the IDENTICAL 17 transitions; only a width above
100% changes the answer (to 10). The estate cannot discriminate the current value from a half or
double of it, because its row counts are re-seeded fixtures rather than a corpus that grows.

What would settle it is incremental growth over a real corpus — ingest that appends rather than
overwrites — where deltas actually populate the 0-50% range the band is trying to cut. Until such a
corpus exists, 0.25 stands as an assumption, and this note is the evidence that it is still one.

PURE, and deliberately so. The caller does the IO (reading the previous version's row count) and hands
the two numbers in; everything here is arithmetic on them. That keeps the policy testable without a
Lance dataset, and — the part that matters for the workflow — keeps it resolvable inside an ACTIVITY,
because a threshold read inside a workflow body replays against whatever the value is now instead of
what it was when the promotion was held.
"""

from __future__ import annotations

import logging
from typing import Final


#: The dataset had no previous version to compare against. §9.1's load-bearing clause: it fires once
#: per dataset, so the band's exact width never decides whether anyone looks at a NEW table.
FIRST_PROMOTION: Final = "first_promotion"

#: The row count moved further than the configured band allows.
ROW_DELTA: Final = "row_count_delta"


log = logging.getLogger(__name__)


def review_reasons(*, row_count: int, previous_row_count: int | None, band: float) -> list[str]:
    """Why a person should look at this promotion — empty when it is unremarkable.

    These are REASONS TO ASK, never reasons to block. `resolve_review_policy` keeps a separate
    structural set for corruption, where nobody is asked because no approval makes broken data right;
    routing a band breach into that set would turn "unusual" back into "dropped forever", which is the
    behaviour §4 says is wrong.

    ``previous_row_count`` is ``None`` when there is no prior version and ``0`` when the prior version
    was empty. Both are first promotions: an empty predecessor gives nothing to compare against, and
    dividing by it would either raise or manufacture an infinite delta.

    A band of ZERO asks about every change, which is the safe direction for a misconfigured value:
    asking too often is visible and annoying, while asking never is invisible, and invisible is the
    failure this module exists to end. Note "every CHANGE" is literal — with a band of zero an
    UNCHANGED row count still does not ask, because `abs(0) > 0` is false.

    This used to read "zero or less", and negative is not reachable: `MedallionSettings` declares
    `promotion_review_band` with `ge=0`, so `MEDALLION_PROMOTION_REVIEW_BAND=-1` fails validation and
    CRASH-LOOPS the mover — measured 2026-08-23, after that sentence invited exactly that value. The
    constraint is right (a band that quietly promotes everything is the failure this exists to end);
    the sentence describing a value it forbids was not.
    """
    if not previous_row_count:
        return [FIRST_PROMOTION]
    if abs(row_count - previous_row_count) > band * previous_row_count:
        return [ROW_DELTA]
    return []


def resolve_previous_row_count(*, observed: int | None, carried: int | None) -> int | None:
    """Which predecessor count the band should use — the WRITER's, else the one pass 1 carried.

    Two sources exist because two lanes measure at different moments. An in-process write observes the
    destination immediately before overwriting it, and is authoritative. The RAY lane cannot: its job
    writes out-of-process, so by the time the mover measures, the predecessor is gone — which made
    `observed` None on every Ray run and turned the band into a review that fired every time. A review
    that always fires carries no signal, so the band was effectively off for that whole lane while
    looking configured.

    `carried` is pass 1's reading of the same number, taken in the last moment it existed
    (`StageTrigger.pre_row_count`). It loses to `observed` because it is older: when the two disagree,
    the writer measured the version it actually replaced.

    Both None stays None, and the band reads that as FIRST_PROMOTION and asks — the safe direction for
    a destination whose history genuinely cannot be read.

    PURE, and extracted for the reason `gate_decision` was: this is policy, and a policy reachable only
    by standing up a mover, a Ray cluster and an object store is a policy nobody re-checks.
    """
    return observed if observed is not None else carried


def previous_row_count(uri: str, storage_options: dict[str, str], *, version: int) -> int | None:
    """Rows in the version BEFORE ``version``, or ``None`` when there is no comparable predecessor.

    The IO half, kept beside the policy it feeds but separate from it: everything above is arithmetic
    and testable without a dataset, and this is the one blocking read. Callers run it in a threadpool.

    ``None`` on any failure, and that is a decision rather than laziness. ``None`` means "first
    promotion", which means ASK — so a dataset we cannot read the history of gets a person's attention
    instead of a silent promote. The opposite default would let an unreadable predecessor look exactly
    like an unremarkable one.
    """
    if version <= 1:
        return None
    import lance

    try:
        return int(lance.dataset(uri, storage_options=storage_options, version=version - 1).count_rows())
    except Exception as exc:  # noqa: BLE001 — an unreadable history is "no predecessor", which ASKS
        # Same reason as the sibling in `compute.py`: the caller is about to read `None` as
        # FIRST_PROMOTION and raise a review, so a read that FAILED must be distinguishable in the log
        # from a version that genuinely has nothing before it. `version <= 1` returns above without
        # coming here, so this only ever fires on a real failure.
        log.warning("medallion_predecessor_unreadable", extra={"uri": uri, "version": version, "error": str(exc)[:200]})
        return None
