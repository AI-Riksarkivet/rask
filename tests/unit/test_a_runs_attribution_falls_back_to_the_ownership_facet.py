"""A run's attribution is rask's `rask_author` run facet, else the standard `ownership` job facet, else nobody.

The feed records this as the row's `author`, the name a reader sees. It is attribution, not authorization:
an external OpenLineage producer that names its owner only in the standard facet is still attributed, and
no producer-supplied name ever authorizes anything (`lineage.models.author_sub_from_payload` reads `sub` alone).
"""

from __future__ import annotations

from lineage.models import RunEvent


def test_a_run_naming_no_author_is_attributed_to_nobody() -> None:
    event = RunEvent.model_validate({"eventType": "START", "eventTime": "t", "run": {"runId": "r2"}, "job": {"namespace": "j", "name": "n"}})

    assert event.author is None


def test_a_run_with_no_author_facet_is_attributed_to_its_jobs_owner() -> None:
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "t",
            "run": {"runId": "r"},
            "job": {"namespace": "ray-jobs", "name": "j", "facets": {"ownership": {"owners": [{"name": "carol", "type": "user"}]}}},
        }
    )

    assert event.author == "carol"
