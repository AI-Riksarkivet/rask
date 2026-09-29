"""The graph's "current version" must be the HIGHEST version, not the most recently EVENTED one.

`LATEST_WRITE_VERSION` ordered by `r.event_time DESC LIMIT 1`, and its own comment gave the reason:
"Most-recent by run event_time, **since Lance versions are monotonic per dataset**". That premise was
true until the hole-recovery landed (95e6adb4). `backfill_write` stamps `datetime.now(UTC)` on the run
it MERGEs, so recovering a hole at version 76 makes version 76 the newest event on the dataset — and
the tip query then answers 76 for a table sitting at 87. Monotonicity was the load-bearing assumption
and the recovery falsified it.

OBSERVED LIVE 2026-09-11, within minutes of that deploy: `bronze$events` v87 acquired a SECOND producer
— `('reconcile', '2026-09-11T13:45:39')` beside the real `('ray', '2026-09-10T15:33:56')`. Nothing had
been lost at v87; the sweep read a low tip, classified the dataset `storage_ahead`, counted it in
`backfilled` as a recovered lost write, and MERGEd a phantom run onto a version that already had its
producer. At the following tick `backfilled=52` — exactly the datasets whose holes were recovered at
the previous one, none of which had lost anything.

TWO CALLERS DEPEND ON THIS, and the second is the quieter failure:

* `latest_write_version` — the reconcile tip, which is what produced the phantom runs above.
* `_schema_is_current` — the recency gate that makes column-inventory seeding and prune idempotent
  under redelivery reordering. It asks "is this event's version at least the newest recorded one"; fed
  a back-filled OLD version it answers True for events it should ignore, and the inventory is rewritten
  from a stale event.

WHY A MAXIMUM IS EXPRESSIBLE HERE AT ALL: versions are stored as STRINGS on the edge
(`SET_WROTE_VERSION` writes `str(version)`), so `ORDER BY w.version DESC` would sort "9" above "87".
`max(toInteger(w.version))` is the form that is both correct and supported — verified against the live
AGE graph 2026-09-11, where it answers 87 for `bronze$events` while the event-time form answered 76.
"""

from __future__ import annotations

from lineage.services import cypher as cy


def test_the_tip_query_takes_a_maximum_rather_than_the_newest_event() -> None:
    """THE GATE. An event-time ordering is wrong the moment any run is stamped out of version order.

    Asserted on the statement rather than through a graph because that is where the defect lives: the
    query is a constant, and every caller inherits whatever it means.
    """
    assert "max(toInteger(w.version))" in cy.LATEST_WRITE_VERSION, (
        f"the tip must be the HIGHEST version — a back-filled hole is stamped now() and would otherwise become the answer: {cy.LATEST_WRITE_VERSION}"
    )
    assert "ORDER BY r.event_time" not in cy.LATEST_WRITE_VERSION, (
        f"event-time recency is exactly the ordering the hole-recovery falsified: {cy.LATEST_WRITE_VERSION}"
    )
