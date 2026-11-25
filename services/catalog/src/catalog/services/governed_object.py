"""The table or namespace a door or a walk acts on, named with the uuid its records are keyed by."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from lakehouse.governed import fga_ids


class GovernedObject(BaseModel):
    """A table or namespace a door acts on: its name and the uuid every record kept for it is keyed by (ADR 0173 §8)."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["table", "namespace"]
    segments: list[str]
    uuid: str
    #: A table's location as its name resolved; empty for a namespace, which has none.
    location: str = ""

    def fga_object(self) -> str:
        """The FGA object its grants are held by: ``table:<uuid>`` or ``namespace:<uuid>`` ([[LH-524]])."""
        return fga_ids.object_of(self.kind, self.uuid)
