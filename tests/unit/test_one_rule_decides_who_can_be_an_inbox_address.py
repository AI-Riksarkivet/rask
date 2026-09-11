"""CONTRACT (LH-093, condition 4): every producer applies ONE rule to `lance.originator`.

`originator` is an inbox ADDRESS — `notifications`' fan-out appends it to the audience and delivers
under `NotificationReason.ORIGINATOR`. So a value that is not one person's subject is not a weaker
address, it is a row in an inbox actor named after a role, a team, or `*`.

The rule existed and only one producer applied it. The catalog guarded with `is_person_subject`; the
medallion wrote `if originator:` — and the medallion is precisely the service whose authors are chart
ROLE LITERALS (`data_eng`, `analyst`, `ray`). The guard's own docstring names this exact failure: "the
two disagreeing is the whole failure mode: a value the door lets through and the builder drops is a
silent miss, and one the builder keeps but the door never sanitized is a row in an inbox actor named
after a role."

Parameterised over BOTH builders from one table of cases, so a third producer cannot be added with its
own interpretation — which is how the second one came to differ from the first.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.subjects import is_person_subject


# NO WORKLOAD NAME is asserted here, deliberately: the platform is forbidden to know about one, so a
# denylist entry for a modality would be the defect rather than the fix. These are the chart's generic
# stage authors plus the shapes that address everyone or name an FGA object instead of a subject.
_NOT_ADDRESSES = ["data_eng", "analyst", "ray", "reconcile", "*", "team:eng#member", "user:alice"]
_ADDRESSES = ["CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBh", "alice@example.org", "alice"]


@pytest.mark.parametrize("value", _NOT_ADDRESSES)
def test_a_value_that_addresses_no_one_is_refused_by_the_shared_rule(value: str) -> None:
    assert not is_person_subject(value), f"{value!r} would become an inbox address"


@pytest.mark.parametrize("value", _ADDRESSES)
def test_a_persons_subject_passes(value: str) -> None:
    assert is_person_subject(value), f"{value!r} is a person and was refused"


@pytest.mark.parametrize("value", _NOT_ADDRESSES)
def test_the_MEDALLION_builder_drops_a_value_that_addresses_no_one(value: str) -> None:
    """The producer whose authors ARE role literals — it wrote `if originator:` and kept every one."""
    from medallion.schemas.events import build_run_event

    event = build_run_event(
        operation="embed_features",
        author="data_eng",
        job_namespace="lance-medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="silver",
        output_name="silver$features",
        originator=value,
    )
    assert "originator" not in event["run"]["facets"]["lance"], f"the medallion stamped {value!r} as an address"


@pytest.mark.parametrize("value", _NOT_ADDRESSES)
def test_the_CATALOG_builder_drops_it_too(value: str) -> None:
    from catalog.core.lineage_emit import build_write_event

    event = build_write_event(
        table_id="acme$silver$features",
        namespace="acme$silver",
        author="alice",
        version=1,
        operation="insert_into_table",
        run_id="r1",
        event_time="2026-09-11T00:00:00+00:00",
        job_namespace="lance-catalog",
        originator=value,
    )
    assert "originator" not in event["run"]["facets"]["lance"], f"the catalog stamped {value!r} as an address"
