"""A DDL change delivered as a ``DatasetEvent`` still puts a row in the inbox of whoever made it.

[[LIN-004]] moves a catalog DDL change off ``RunEvent`` onto ``DatasetEvent`` — which carries no
``run`` and no ``eventType``, the two fields :class:`LineageRunEvent` requires with ``min_length=1``.
`ingest.py` DROPS a payload that does not validate, so without this the change would silently stop
notifying.

IT NOTIFIES TODAY, MEASURED RATHER THAN ASSUMED. All four DDL events on the live feed (2026-09-23)
project to a `Notifiable`: three name a Dex sub, one a service identity. So the regression this guards
is a person who is told now and would not be told after.

THE IDENTITY MUST BE DERIVED, NOT MINTED. `notification_id` is `${run_id}@${state}` and a static event
has no run id, while the two lanes see DIFFERENT copies of the same event — the bus lane takes the
catalog's raw publish, the feed lane takes what lineage stored. A random id would land two pointers
for one change; `service_kit.openlineage.static_event_id` derives one from the content so both lanes
agree, which is the property `deliver_lineage_event`'s own docstring rests on.
"""

from __future__ import annotations

from notifications.api.lineage_events import LineageRunEvent, notifiable


AUTHOR = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjYSBWxvY2Fs"


def _static(**facet_overrides: object) -> dict[str, object]:
    """A spec-shaped DatasetEvent for a `create_table`, as the catalog will publish it."""
    facets: dict[str, object] = {
        "author": {"sub": AUTHOR, "name": "Alice"},
        "lance": {"operation": "create_table", "version": 1, "project": "acme"},
        "lifecycleStateChange": {"lifecycleStateChange": "CREATE"},
    }
    facets.update(facet_overrides)
    return {
        "eventTime": "2026-09-23T10:00:00+00:00",
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "dataset": {"namespace": "lance-catalog", "name": "silver$features", "facets": facets},
    }


def test_a_static_metadata_change_projects_to_a_notifiable() -> None:
    """The whole regression: a DDL change must keep reaching the person who made it."""
    notice = notifiable(LineageRunEvent.model_validate(_static()))
    assert notice is not None
    assert notice.author == AUTHOR
    assert notice.delivery.object_id == "silver$features"
    assert notice.project == "acme"


def test_the_identity_is_derived_from_the_event_so_two_lanes_agree() -> None:
    """The bus lane and the feed lane see different copies; a minted id would land two pointers."""
    first = notifiable(LineageRunEvent.model_validate(_static()))
    second = notifiable(LineageRunEvent.model_validate(_static()))
    assert first is not None and second is not None
    assert first.delivery.notification_id == second.delivery.notification_id


def test_a_different_change_to_the_same_table_is_a_different_notification() -> None:
    """A drop and a re-create of one table are two facts; collapsing them silences the second."""
    created = notifiable(LineageRunEvent.model_validate(_static()))
    later = _static()
    later["eventTime"] = "2026-09-23T11:00:00+00:00"
    recreated = notifiable(LineageRunEvent.model_validate(later))
    assert created is not None and recreated is not None
    assert created.delivery.notification_id != recreated.delivery.notification_id


def test_an_unauthored_static_change_notifies_NOBODY() -> None:
    """v1's audience IS the author — the rule does not relax because the event kind changed."""
    payload = _static()
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset["facets"] = {"lance": {"operation": "create_table"}}
    assert notifiable(LineageRunEvent.model_validate(payload)) is None


def test_the_ownership_facet_is_still_not_an_author() -> None:
    """`ownership` is producer-supplied; honouring it would put a row in any named person's inbox."""
    payload = _static(ownership={"owners": [{"name": AUTHOR, "type": "MAINTAINER"}]})
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    facets = dataset["facets"]
    assert isinstance(facets, dict)
    del facets["author"]
    assert notifiable(LineageRunEvent.model_validate(payload)) is None
