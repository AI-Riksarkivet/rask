"""What one OpenLineage event becomes — the rules that decide whether anybody is told at all.

Pure over a parsed payload: no Dapr, no FastAPI, no OpenFGA. Every case here is a rule from the
design rather than an implementation detail, so each test names the rule it is holding in place.
"""

from typing import Any

import pytest

from notifications.api.lineage_events import LineageRunEvent, author_subject, is_terminal, notifiable, source_run_id


def _event(
    *,
    event_type: str = "FAIL",
    run_id: str = "run-1",
    facets: dict[str, Any] | None = None,
    outputs: list[str] | None = None,
    event_time: str = "2026-08-09T12:00:00+00:00",
) -> dict[str, Any]:
    return {
        "eventType": event_type,
        "eventTime": event_time,
        "run": {"runId": run_id, "facets": facets if facets is not None else {"author": {"name": "alice", "sub": "alice"}}},
        "job": {"namespace": "lance-medallion", "name": "htr"},
        "outputs": [{"namespace": "silver", "name": name} for name in (outputs if outputs is not None else ["silver$pages"])],
    }


@pytest.mark.parametrize("state", ["FAIL", "complete"])
def test_the_terminal_states_are_the_notifiable_ones(state: str) -> None:
    assert is_terminal(state)


def test_the_author_comes_from_the_verified_sub() -> None:
    event = LineageRunEvent.model_validate(_event(facets={"author": {"name": "alice", "sub": "alice-sub"}}))
    assert author_subject(event.run) == "alice-sub"


def test_a_name_without_a_sub_is_not_an_audience() -> None:
    """`RunEvent.author` falls back to `author.name` and then the standard `ownership` JOB facet,
    which is right for attribution on a board and wrong for targeting: `ownership` is a
    producer-supplied string nobody verified, so honouring it would let any producer drop a row into a
    named person's inbox. Every estate writer that VERIFIES the author writes `sub`."""
    event = LineageRunEvent.model_validate(_event(facets={"author": {"name": "alice"}}))
    assert author_subject(event.run) is None
    assert notifiable(event) is None


def test_an_ownership_job_facet_is_never_targeted() -> None:
    event = LineageRunEvent.model_validate(_event(facets={"ownership": {"owners": [{"name": "alice"}]}}))
    assert author_subject(event.run) is None


@pytest.mark.parametrize(
    "facets",
    [
        pytest.param({"author": "alice"}, id="facets1"),
        pytest.param({"author": {"sub": ""}}, id="facets2"),
        pytest.param({"author": {"sub": 7}}, id="facets3"),
    ],
)
def test_a_malformed_author_facet_is_a_missing_one_never_a_raise(facets: dict[str, Any]) -> None:
    """Facets are an open bag on an untrusted envelope. A producer writing a number where a string
    belongs must cost a notification, never an exception inside a subscription handler."""
    assert author_subject(LineageRunEvent.model_validate(_event(facets=facets)).run) is None


def test_the_link_is_the_producers_own_run_id() -> None:
    """The graph runId is a derived uuid5 that links to nothing — every ingest-board row 404'd when it
    was used as the link — so the pointer carries `run.facets.lance.run_id` beside it."""
    event = LineageRunEvent.model_validate(_event(facets={"author": {"sub": "alice"}, "lance": {"run_id": "ingest-42"}}))
    assert source_run_id(event.run) == "ingest-42"
    notice = notifiable(event)
    assert notice is not None
    assert notice.delivery.source_run_id == "ingest-42"


def test_a_run_recorded_before_producers_stated_their_own_id_links_to_nothing() -> None:
    notice = notifiable(LineageRunEvent.model_validate(_event()))
    assert notice is not None
    assert notice.delivery.source_run_id is None


def test_the_notification_id_is_the_id_the_bell_already_keys_on() -> None:
    """`runNotificationId` is `${run.run_id}@${state}` — the GRAPH id, upper-cased state. Sharing the
    scheme is what makes dismissing a run's earlier state still let its later one through."""
    notice = notifiable(LineageRunEvent.model_validate(_event(event_type="fail", run_id="run-7")))
    assert notice is not None
    assert notice.delivery.notification_id == "run-7@FAIL"


def test_a_run_with_no_outputs_notifies_nobody_under_fga_on_or_off() -> None:
    """The governed read path drops a dataset-less row when FGA is on — it would pass the visibility
    test vacuously and disclose run/author/error to a caller holding no grants. This plane refuses it
    one step earlier and unconditionally: a pointer names the object it is about, and there is no
    honest value for that field."""
    assert notifiable(LineageRunEvent.model_validate(_event(outputs=[]))) is None
    assert notifiable(LineageRunEvent.model_validate(_event(outputs=[""]))) is None


def test_the_pointer_names_the_primary_output_while_the_check_covers_them_all() -> None:
    """The pointer is a claim-check: it names ONE object, which the render path re-checks, and says
    nothing about the others. The delivery check is the subset test over the whole set — a different
    question (may you be told at all), not a weaker version of the same one."""
    notice = notifiable(LineageRunEvent.model_validate(_event(outputs=["silver$pages", "gold$lines"])))
    assert notice is not None
    assert notice.delivery.object_id == "silver$pages"
    assert notice.outputs == frozenset({"silver$pages", "gold$lines"})
