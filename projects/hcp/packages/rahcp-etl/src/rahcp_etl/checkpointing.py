"""KV-backed checkpoint storage for pipeline state."""

from __future__ import annotations

import json
import logging
from typing import Any

import nats  # ty: ignore[unresolved-import]
from nats.js.kv import KeyValue  # ty: ignore[unresolved-import]

log = logging.getLogger(__name__)


class CheckpointStore:
    """KV-backed progress checkpoints using NATS JetStream KV.

    Each pipeline run gets a key: ``{pipeline_id}`` with value containing
    the last completed stage and its output state. On failure, the pipeline
    can resume from the last checkpoint instead of restarting.
    """

    def __init__(self, kv: KeyValue) -> None:
        self._kv = kv

    @classmethod
    async def create(
        cls,
        nc: nats.NATS,
        bucket: str = "etl-checkpoints",
    ) -> CheckpointStore:
        """Create or bind to a KV bucket."""
        js = nc.jetstream()
        kv = await js.create_key_value(bucket=bucket)
        return cls(kv)

    async def save(
        self,
        pipeline_id: str,
        stage: str,
        state: dict[str, Any],
    ) -> None:
        """Save a checkpoint after a successful stage."""
        payload = json.dumps({"stage": stage, "state": state}).encode()
        await self._kv.put(pipeline_id, payload)
        log.debug("Checkpoint saved: %s @ %s", pipeline_id, stage)

    async def load(self, pipeline_id: str) -> dict[str, Any] | None:
        """Load the last checkpoint for a pipeline run.

        Returns ``{"stage": str, "state": dict}`` or ``None`` if no
        checkpoint exists.
        """
        try:
            entry = await self._kv.get(pipeline_id)
            return json.loads(entry.value)
        except Exception:
            return None

    async def clear(self, pipeline_id: str) -> None:
        """Clear the checkpoint for a pipeline run."""
        try:
            await self._kv.delete(pipeline_id)
        except Exception:
            pass
