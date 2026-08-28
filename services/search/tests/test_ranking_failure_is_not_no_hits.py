"""A search plane that has stopped ranking must not answer 200 with an empty list.

open_python-audit (E9's sharpest claim, an E4 fail-open in substance) — "`frames.py`'s
`_ranked_or_fallback` is `try: return rank(scoped=True) / except: pass` → fall through, so a search
plane that has stopped ranking anything returns an empty 200 no test can tell from 'no hits'".

THE SWALLOW'S OWN JUSTIFICATION IS EMPIRICALLY FALSE, which is why this is a deletion and not a
tuning exercise. Both docstrings defended returning `[]` as the "no vector/FTS index yet" case —
measured against lancedb at HEAD (pylance/lancedb as pinned):

  * FTS query with NO FTS index      -> NO RAISE (flat scan)
  * vector search with NO ANN index  -> NO RAISE (flat search, rows returned)
  * prefilter naming a missing column -> RAISES `RuntimeError: Invalid user input: Schema error:
    No field named ...`

So absence does not raise here at all — it is already handled one layer up by the guard
(`vec is None or frame_tbl is None or column not in frame_tbl.schema.names -> []`). The ONLY thing
the unscoped `except` ever caught was a genuine failure: a store outage, a timeout, a malformed
query. Every one of those was rendered as "no hits".

The SCOPED->unscoped retry stays, because that one is real and verified above: a scope prefilter may
legitimately name a column the frame table lacks (a metadata filter belongs on the row-table join),
and retrying unscoped is the documented recovery.
"""

from __future__ import annotations

from typing import Any

import pytest
from search.services import frames

from service_kit.exceptions import ServiceUnavailableError


def _rank_that(*, scoped_exc: Exception | None, unscoped_exc: Exception | None, rows: list[dict[str, Any]] | None = None):
    calls: list[bool] = []

    def rank(*, scoped: bool) -> list[dict[str, Any]]:
        calls.append(scoped)
        exc = scoped_exc if scoped else unscoped_exc
        if exc is not None:
            raise exc
        return rows if rows is not None else [{"id": 1}]

    return rank, calls


def test_a_failed_UNSCOPED_rank_is_reported_not_rendered_as_no_hits() -> None:
    """The finding. A ranker that cannot run is an OUTAGE; `[]` says 'this corpus has nothing to
    show you', which is a different and wrong answer."""
    rank, _ = _rank_that(scoped_exc=None, unscoped_exc=RuntimeError("object store connection reset"))

    with pytest.raises(ServiceUnavailableError) as caught:
        frames._ranked_or_fallback(rank, scoped=False)
    assert "connection reset" in str(caught.value) or "rank" in str(caught.value).lower()


def test_the_scoped_to_unscoped_RETRY_still_works() -> None:
    """The half that is real and must survive: a prefilter naming a non-frame column raises, and the
    unscoped retry is the documented recovery — verified against lancedb, which raises
    `Invalid user input: Schema error: No field named ...` for exactly that."""
    rank, calls = _rank_that(
        scoped_exc=RuntimeError("Invalid user input: Schema error: No field named meta_year"),
        unscoped_exc=None,
        rows=[{"id": 7}],
    )

    assert frames._ranked_or_fallback(rank, scoped=True) == [{"id": 7}]
    assert calls == [True, False], "the unscoped retry did not run"


def test_BOTH_failing_still_reports_rather_than_returning_empty() -> None:
    """A scoped failure followed by an unscoped failure is two outages, not zero hits."""
    rank, calls = _rank_that(scoped_exc=RuntimeError("no field named x"), unscoped_exc=RuntimeError("read timed out"))

    with pytest.raises(ServiceUnavailableError):
        frames._ranked_or_fallback(rank, scoped=True)
    assert calls == [True, False]


def test_a_working_ranker_is_untouched() -> None:
    """The failure mode that would hide the fix: raising on everything also passes the tests above."""
    rank, calls = _rank_that(scoped_exc=None, unscoped_exc=None, rows=[{"id": 1}, {"id": 2}])

    assert frames._ranked_or_fallback(rank, scoped=False) == [{"id": 1}, {"id": 2}]
    assert calls == [False], "an unscoped call must not attempt a scoped rank first"
