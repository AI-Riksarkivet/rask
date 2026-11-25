"""The run id every lakehouse emitter derives (`lakehouse.events.openlineage.run_id_for`).

The OpenLineage spec gate for what the emitters put on the bus is ``tests/unit/test_openlineage_spec_conformance.py``.
"""

import uuid

from lakehouse.events.openlineage import run_id_for


def test_run_id_for_is_a_deterministic_uuid() -> None:
    a, b = run_id_for("ingest-silver.lines-7"), run_id_for("ingest-silver.lines-7")
    assert a == b
    assert uuid.UUID(a)
    assert run_id_for("another-seed") != a
