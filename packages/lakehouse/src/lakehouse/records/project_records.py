"""The project registry's record: a tenant, as the catalog writes it and every reader holds it.

One JSON object per project under ``<control_root>/_projects/``, written by the catalog's project doors and the
declaration converge. The catalog owns the writes (`catalog/services/projects.py`); the model lives here because
maintenance reads the same records for its drift report and tenancy rebuild, and a per-service copy of the shape would
drift.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


__all__ = [
    "ProjectRecord",
    "UnreadableProjectRecordError",
    "read",
]


class UnreadableProjectRecordError(ValueError):
    """A project record is stored and cannot be read as one, so whether the tenant stands, is being deleted, is
    protected or is git's is unknown. Names where the record sits and the fields that are not a record's."""


class ProjectRecord(BaseModel):
    """A tenant. Existence lives in the record, so the id is the whole of its identity.

    The fields a reader keys on are declared; every other field its writer stored rides along (``extra="allow"``), and
    :meth:`stored` writes back exactly the fields the record was read with.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    id: str = Field(min_length=1)
    #: The principal key of the caller who started its delete, while that delete is unfinished ([[LH-576]]). From then
    #: on the project is absent to every door but its delete.
    deleting_by: str | None = Field(default=None, min_length=1)
    created_at: str | None = None
    created_by: str | None = None
    #: The deletion-protection flag (``true`` when armed) and who armed it.
    protected: str | None = None
    protected_by: str | None = None
    #: Who owns the project's declaration (``git``); absent, its own doors do.
    managed_by: str | None = None

    @property
    def is_being_deleted(self) -> bool:
        """Whether a delete of the project has started and not finished."""
        return self.deleting_by is not None

    @property
    def is_protected(self) -> bool:
        """Whether deletion protection is armed: the flag reads ``true`` in any case."""
        return (self.protected or "").lower() == "true"

    def stored(self) -> dict[str, Any]:
        """The record as it is written: the fields it was built or read with, and no other."""
        return self.model_dump(mode="json", exclude_unset=True)


def read(raw: object, *, at: str) -> ProjectRecord:
    """``raw`` as a project record; ``at`` names where it sits, for the refusal.

    Raises:
        UnreadableProjectRecordError: It is no project record.
    """
    try:
        return ProjectRecord.model_validate(raw)
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(str(part) for part in error["loc"]) or "the record" for error in exc.errors(include_url=False)}))
        raise UnreadableProjectRecordError(f"the project record at {at} cannot be read: {fields} is not what a record holds") from exc
