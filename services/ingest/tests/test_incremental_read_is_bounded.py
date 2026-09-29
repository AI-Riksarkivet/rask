"""The anti-join read every existing bronze row, per tick, with no ceiling.

`docs/architecture/ingest-and-tier-movement.md` §1c chose the anti-join against bronze itself precisely so incremental
ingest needs no second store — and named its cost in the same breath: **"O(existing rows) per tick,
not O(new rows)"**. It then said what to do about it: *"Bound it explicitly.
`RASK_INGEST_INCREMENTAL_MAX_ROWS`, default `0` = unbounded, in the identical shape and with the
identical reasoning as `MAX_UNITS`."* The mechanism shipped; the bound did not.

`enumerate_chunks` does `dataset.to_table(columns=["id"])` and materialises EVERY id into a Python
set. On a table the plane's own docstrings advertise — million-unit harvests — that is the whole
table in memory on every cron tick, and the activity retries, so a run that cannot fit it does not
fail once.

**THE CEILING MUST REFUSE, NEVER SAMPLE, and that is the part worth getting right.** Truncating an
anti-join does not degrade it, it inverts it: a partial "already have" set makes the run conclude
that rows bronze holds are new, and re-land every one of them. Silent duplication is the exact
outcome §1c's whole design exists to prevent, so a ceiling that trimmed the read would be worse than
no ceiling at all. It is refused for the same reason `AntiJoinUnavailable` refuses an unreadable id
column: ingesting anyway re-lands everything.

Zero means unbounded and is the default IN CODE, matching `max_units` — this plane advertises long
harvests, so a live default would kill the legitimate run the ceiling exists to protect.
"""

from __future__ import annotations

import pytest


class TestTheDecisionItself:
    """The predicate, in isolation: given a ceiling and a row count, may this run proceed?"""

    @pytest.mark.parametrize(("rows", "ceiling"), [(10, 0), (10, 10)])
    def test_it_allows_what_fits(self, rows: int, ceiling: int) -> None:
        from ingest.workflow import anti_join_within_ceiling

        assert anti_join_within_ceiling(rows, ceiling) is True
