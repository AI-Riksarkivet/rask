"""The catalog's lineage emitter, for a test whose claim is what a door announces to lineage: each create and write the
door asks it to announce is kept, in order, where the real emitter would build the OpenLineage event and publish it.

A test hands it to the app in place of the emitter its lifespan builds (`app.state.lineage_emitter`), or to
`catalog.core.lineage_emit.emit_write_event` directly. Each method carries the protocol's own keywords, so a door that
passes one the protocol does not name fails here as it would against the real emitter.
"""

from typing import Any

from catalog.core.lineage_emit import NO_FIELDS, EmitFields


class LineageEmitterDouble:
    """The `catalog.core.lineage_emit.LineageEmitter` a test reads back: :attr:`writes`, each announcement as the keywords
    it was given with its fields beside them, in the order they were asked for. It resolves no warehouse to a project."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def project_for(self, warehouse_id: str) -> str | None:
        """No project owns ``warehouse_id``."""
        del warehouse_id
        return None

    async def emit_create(self, *, table_id: str, namespace: str, author: str | None, version: int, fields: EmitFields = NO_FIELDS) -> None:
        """Keep a create's announcement."""
        self.writes.append({"table_id": table_id, "namespace": namespace, "author": author, "version": version, **dict(fields)})

    async def emit_write(
        self, *, table_id: str, namespace: str, author: str | None, version: int | None, operation: str, fields: EmitFields = NO_FIELDS
    ) -> None:
        """Keep a write's announcement."""
        self.writes.append({"table_id": table_id, "namespace": namespace, "author": author, "version": version, "operation": operation, **dict(fields)})
