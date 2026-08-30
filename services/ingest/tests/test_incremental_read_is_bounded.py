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

from ingest.workflow import RunLimits


class TestTheCeilingExists:
    def test_run_limits_carries_it(self) -> None:
        assert "incremental_max_rows" in RunLimits.model_fields, (
            "the anti-join's O(existing rows) cost has no ceiling — §1c named this bound and it was never built"
        )

    def test_zero_is_the_default_and_means_unbounded(self) -> None:
        assert RunLimits().incremental_max_rows == 0

    def test_it_is_resolved_from_the_documented_env_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RASK_INGEST_INCREMENTAL_MAX_ROWS", "5000")
        assert RunLimits.from_env().incremental_max_rows == 5000

    def test_an_empty_value_is_unbounded_not_a_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`kubectl set env FOO=` leaves an empty string; `int("")` raises. The other two ceilings
        already survive this and a third that did not would take its activity down for a typo."""
        monkeypatch.setenv("RASK_INGEST_INCREMENTAL_MAX_ROWS", "")
        assert RunLimits.from_env().incremental_max_rows == 0


class TestTheDecisionItself:
    """The predicate, in isolation: given a ceiling and a row count, may this run proceed?"""

    @pytest.mark.parametrize(("rows", "ceiling"), [(10, 0), (10, 10), (0, 1), (9, 10), (1_000_000, 0)])
    def test_it_allows_what_fits(self, rows: int, ceiling: int) -> None:
        from ingest.workflow import anti_join_within_ceiling

        assert anti_join_within_ceiling(rows, ceiling) is True

    @pytest.mark.parametrize(("rows", "ceiling"), [(11, 10), (1_000_001, 1_000_000), (2, 1)])
    def test_it_refuses_what_does_not(self, rows: int, ceiling: int) -> None:
        from ingest.workflow import anti_join_within_ceiling

        assert anti_join_within_ceiling(rows, ceiling) is False

    def test_the_boundary_is_inclusive(self) -> None:
        """A ceiling of N means N rows are allowed. Off-by-one here refuses a run the operator
        deliberately sized to fit."""
        from ingest.workflow import anti_join_within_ceiling

        assert anti_join_within_ceiling(10, 10) is True
        assert anti_join_within_ceiling(11, 10) is False

    def test_zero_never_refuses_anything(self) -> None:
        from ingest.workflow import anti_join_within_ceiling

        assert anti_join_within_ceiling(10**12, 0) is True


class TestRefusalIsNotSampling:
    def test_the_read_site_does_not_LIMIT_the_scan(self) -> None:
        """The tempting "fix" — `to_table(columns=['id'], limit=N)` — inverts the anti-join: a
        partial `existing` set makes the run treat rows bronze already holds as new and re-land them.
        Bounded memory bought with silent duplication is a worse trade than the unbounded read."""
        import inspect

        from ingest import workflow

        source = inspect.getsource(workflow.enumerate_chunks)
        assert "limit=" not in source, "an anti-join read must never be truncated — it must refuse"

    def test_the_refusal_is_the_existing_unavailable_error(self) -> None:
        """Same failure class as an unreadable id column, and for the same reason: in both cases the
        run cannot tell what bronze already holds, and ingesting anyway re-lands everything. A new
        exception type would split one meaning across two handlers."""
        import inspect

        from ingest import workflow

        source = inspect.getsource(workflow.enumerate_chunks)
        assert "anti_join_within_ceiling" in source, "the ceiling is defined and never consulted"
        assert "AntiJoinUnavailable" in source
