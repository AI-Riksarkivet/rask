"""A bronze write as the cascade head receives it: rows at a Lance URI, and the catalog's announcement of them.

The lakehouse writes no bronze itself (ADR 0154): a client inserts through the catalog, and the catalog's
lineage event is what `/bronze-arrival` fires on. The in-process cascade suites need both halves without a
running catalog, so the rows are seeded by `seed_bronze` and the announcement is built by the catalog's own
`build_write_event`, the function the catalog's insert door calls, rather than written by hand here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from catalog.core.lineage_emit import build_write_event
from medallion.core.config import project_namespace
from medallion.services.compute import seed_bronze


def write_bronze(uri: str, *, token: str, project: str = "", rows: int = 8) -> dict[str, Any]:
    """Seed ``rows`` bronze rows at ``uri`` and return the catalog's ``insert`` event for them.

    ``token`` becomes the run id, which the head reads as the cascade token when the event carries no
    ``lance.token`` facet, as a catalog insert does not.
    """
    table_id = project_namespace(project, "bronze$events")
    result = seed_bronze(uri, {}, rows=rows, dataset_id=table_id)
    return build_write_event(
        table_id=table_id,
        namespace=project_namespace(project, "bronze"),
        author="writer",
        version=result.version,
        operation="insert",
        run_id=token,
        event_time=datetime.now(UTC).isoformat(),
        job_namespace="lance-catalog",
        source_uri=uri,
        project=project or None,
    )
