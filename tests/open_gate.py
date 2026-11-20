"""A gate that admits every delivery, for a test of what lineage's handler does with an event once its door let it in.

The handler takes its gate with no default, so a call site that names none does not record unjudged events
(`lineage.services.consumer.handle_cloud_event`); a test whose claim is the parse, the record or the verdict names this one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lineage.models import DatasetEvent, RunEvent


async def admits_everything(_event: RunEvent | DatasetEvent, _arrived: Mapping[str, Any]) -> frozenset[str]:
    """Admit the event, aliasing no table."""
    return frozenset()
