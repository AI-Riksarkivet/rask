"""The dummy lane's terminal event — the four fields that decide whether a person is ever told.

`notifiable()` discards an event on ANY of four misses and acks it SUCCESS, so a producer that gets
this wrong is reported by nothing. That is why these are asserted field by field rather than through
a round trip: the round trip passes either way.

1. **Terminal state.** COMPLETE / FAIL. A START notifies nobody by product decision.
2. **A targetable principal.** This job authenticates as a SERVICE, and the lineage ingest's
   `enforce_author` OVERWRITES `author` with that service's verified sub — deliberately, since
   honouring a producer-supplied author would let any producer file a row in a named person's inbox.
   So the human rides `lance.originator`, never `author`. The medallion movers get this wrong today:
   they author with a chart role literal (`ray`, `data_eng`), which addresses an inbox actor named
   `ray` and reaches no one.
3. **`lance.project`.** Optional to the schema, and omitting it silently costs EVERY watcher.
4. **≥1 output named exactly as the FGA object is.** Delivery checks `table:<output name>`, so an
   unqualified name against tenant-qualified grants counts every recipient HIDDEN.
"""

from __future__ import annotations

import pytest

from dummy_runner.lineage import build_run_event


def _event(**over: object) -> dict:
    base: dict = {
        "event_type": "COMPLETE",
        "run_id": "e2e-1",
        "to_id": "silver$dummy",
        "from_id": "bronze$events",
        "rows": 64,
        "version": 2,
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
        "project": "acme",
    }
    return build_run_event(**(base | over))


def test_a_completed_run_is_TERMINAL() -> None:
    assert _event()["eventType"] == "COMPLETE"


def test_a_failed_run_is_terminal_and_carries_the_error() -> None:
    event = _event(event_type="FAIL", error="boom", version=None)

    assert event["eventType"] == "FAIL"
    assert event["run"]["facets"]["errorMessage"]["message"] == "boom"


def test_the_human_rides_lance_originator_and_NEVER_author() -> None:
    """Trap 2, stated as the code must obey it.

    Setting `author` here would be overwritten by the ingest anyway — but worse, it would read like
    the field is handled when the person it names is being dropped.
    """
    facets = _event()["run"]["facets"]

    assert facets["lance"]["originator"] == "CiQwOGE4Njg0Yi1kYjg4"
    assert "author" not in facets, "a producer-supplied author is overwritten by the ingest; use originator"


def test_lance_project_is_carried_because_omitting_it_costs_every_watcher() -> None:
    assert _event()["run"]["facets"]["lance"]["project"] == "acme"


def test_the_output_is_named_EXACTLY_as_the_FGA_object() -> None:
    """`silver$dummy`, not `dummy` — delivery checks `table:<name>` and an unqualified name matches
    no tenant-qualified grant, hiding the row from every recipient."""
    outputs = _event()["outputs"]

    assert len(outputs) == 1
    assert outputs[0]["name"] == "silver$dummy"
    assert outputs[0]["namespace"] == "silver", "the namespace is the identifier's first segment"


def test_the_input_edge_names_the_upstream_tier() -> None:
    """Without an input there is no DERIVED_FROM edge, and silver appears in the graph as an orphan
    nobody can trace back to bronze."""
    inputs = _event()["inputs"]

    assert [i["name"] for i in inputs] == ["bronze$events"]
    assert inputs[0]["namespace"] == "bronze"


def test_the_output_version_appears_on_COMPLETE_and_NOT_on_FAIL() -> None:
    """A FAIL must never carry a fabricated version — the run committed nothing."""
    complete = _event()["outputs"][0]
    failed = _event(event_type="FAIL", error="boom", version=None)["outputs"][0]

    assert complete["facets"]["version"]["datasetVersion"] == "2"
    assert "version" not in failed.get("facets", {})


def test_an_originator_that_is_not_a_person_is_DROPPED_rather_than_carried() -> None:
    """Trap 1 and trap 4 together: a role literal or a wildcard is not an address.

    Carrying `ray` writes into an inbox actor literally named `ray`. Dropping it is not a loss —
    the event is still recorded for the graph; it simply targets nobody, which is the honest
    outcome for a run no person asked for.
    """
    for junk in ("ray", "data_eng", "*", "user:*", ""):
        facets = _event(originator=junk)["run"]["facets"]
        assert "originator" not in facets["lance"], f"{junk!r} was carried as an address"


@pytest.mark.parametrize("state", ["START", "RUNNING", "OTHER"])
def test_a_non_terminal_state_is_REFUSED_at_construction(state: str) -> None:
    """The job only ever emits terminal events, so a non-terminal one is a bug at the call site
    rather than something to encode and let `notifiable()` silently discard."""
    with pytest.raises(ValueError, match="terminal"):
        _event(event_type=state)
