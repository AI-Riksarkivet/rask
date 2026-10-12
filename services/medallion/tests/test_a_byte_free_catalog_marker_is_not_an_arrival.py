"""The head fires on a batch LANDING, not on a metadata change that names the same table.

The catalog publishes its own markers onto the very topic `/bronze-arrival` consumes: a `register_table`,
`deregister_table` or `declare_table` is a COMPLETE event whose one output is the bronze table, identical on
every field the head matches on. Firing on one starts a cascade over a batch that never moved, carrying a token
nothing else in the run has.
"""

from __future__ import annotations

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import ingest_trigger


def _event(lance: dict[str, str]) -> dict[str, object]:
    return {
        "eventType": "COMPLETE",
        "run": {"runId": "r1", "facets": {"lance": lance}},
        "outputs": [{"namespace": "bronze", "name": "bronze$events"}],
    }


@pytest.mark.parametrize("operation", ["register_table", "deregister_table", "declare_table"])
def test_a_byte_free_catalog_marker_fires_nothing(operation: str) -> None:
    assert ingest_trigger._bronze_write_dataset(_event({"operation": operation}), MedallionSettings(), "") is None


def test_a_real_bronze_write_still_fires() -> None:
    """The denylist must not become an allowlist: an external OpenLineage producer names its own
    operations, or none at all, and it has to keep driving the cascade. The catalog's own insert is
    `tests/integration/test_a_catalog_insert_into_bronze_starts_the_cascade.py`."""
    for lance in ({"operation": "append_batch"}, {}):
        assert ingest_trigger._bronze_write_dataset(_event(lance), MedallionSettings(), "") == "bronze$events"
