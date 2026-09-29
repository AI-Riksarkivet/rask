"""The source version a run CONSUMED survives into the run board, and survives a later bare event.

docs/DECISIONS.md "Cascade repair" (C3b). `498b5531` put `from_version`/`to_version` into the `lance` run facet, so
the cascade's delta boundary is finally recorded — and it is still unqueryable, which is one layer
further along than the gap that commit closed. `RunStatus` folds `operation`, `source_run_id` and
`promotion_status` off that facet and not the range, so nothing can answer *"what source version has
silver actually consumed?"* — which is exactly the predicate the cascade lag detector needs.

STICKINESS IS THE WHOLE DIFFICULTY, and the three fields beside it already say why: a reconcile or
backfill event for the same graph run carries NO lance facet, and a later bare event that clobbered the
value would erase what an earlier one declared. `operation` and `source_run_id` and `promotion_status`
each use `CASE WHEN $x = '' THEN <keep> ELSE $x END` for that reason. A version is an INT, so the empty
string is not available as "the event did not say" — the sentinel has to be a value a version can never
take.
"""

from __future__ import annotations

from lineage.models import RunEvent


def _event(**lance: object) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-04T06:00:00Z",
            "producer": "test",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {"lance": {"_producer": "t", **lance}}},
            "job": {"namespace": "medallion", "name": "embed_features"},
            "outputs": [{"namespace": "silver", "name": "silver$features"}],
        }
    )


def test_the_consumed_ceiling_is_read_off_the_facet() -> None:
    assert _event(operation="embed_features", to_version=7).consumed_to_version == 7
