"""What the catalog's maintenance doors answer: the catalog serves each model as its door's response, and maintenance
reads the answer with the same model, so the two cannot disagree on a field.

They ship in one image and deploy together, so a field the catalog renames is renamed for the reader in the same build;
a reader of its own copy would only fail at run time, on the estate.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class CleanupDisarmed(BaseModel):
    """What the disarm of a ref's commit-path cleanup did ([[LH-575]])."""

    #: The ref's version after it: the config-only commit it made, or the version it found when no key was set.
    version: int
    #: The ``lance.auto_cleanup.*`` keys it removed; empty when none was set.
    disarmed: list[str] = Field(default_factory=list)


class FloorRaised(BaseModel):
    """What a raise of a table's listing floor committed ([[LH-094]], [[LH-575]]); nothing on a dry run."""

    #: The version the raise committed.
    version: int | None = None
    #: The epoch the ``rask.maintenance.floor_raised_at`` stamp it committed names.
    raised_at: float | None = None


class TrashReleaseResponse(BaseModel):
    """What releasing a trash record past its deadline did: the grants revoked, and whether the record was stamped."""

    tuples_revoked: int
    #: ``False`` when the record was gone by the stamp, purged or undropped meanwhile; the grants are revoked either way.
    stamped: bool


class TrashEndResponse(BaseModel):
    """What ending a trash record did beside ending its records: the grants it still held, revoked."""

    tuples_revoked: int


class TrashRefusalResponse(BaseModel):
    """How many refusals the record now counts; ``None`` when the note could not be made (the record went meanwhile)."""

    attempts: int | None = None


class CloneEndingEntry(BaseModel):
    """One registered clone's ending that its door could not clear (ADR 0198 §1): the digest its finish is asked by."""

    #: The SHA-256 of the clone's decoded location, which its tags, its claim and its endings are kept under.
    digest: str
    #: The token of the request that filed it, which names this ending under the digest.
    filing: str
    #: The door that ended the clone.
    door: str
    filed_at: datetime

    @property
    def id(self) -> str:
        """The ending's address at its door: ``<digest>/<filing>``."""
        return f"{self.digest}/{self.filing}"


class CloneEndingsResponse(BaseModel):
    """Every clone's ending not yet cleared, oldest first."""

    endings: list[CloneEndingEntry]


class CloneEndingFinishResponse(BaseModel):
    """What finishing one clone's ending did: how many source tags it released, or that the clone itself stands there
    for good and keeps them."""

    tags_released: int
    kept: bool


class IdempotencyKeysReclaimResponse(BaseModel):
    """What one reclaim of expired `Idempotency-Key` records did ([[LH-576]])."""

    #: Records deleted.
    reclaimed: int
    #: Expired records a retry wrote again since they were listed, kept for another lifetime.
    kept: int
    #: Expired records past this call's limit, for the caller to ask again.
    left: int
    #: Objects under the records' prefix that are no record, left alone.
    unreadable: int


class GrantCensusResponse(BaseModel):
    """Every object's grant count, by type, as the store holds them now: what a drift report's authorization categories
    read. The objects a deployment keeps for itself are left out, since no registry names them."""

    #: ``False`` when authorization is off in the catalog: the store holds nothing to count, which is not an empty one.
    enabled: bool
    #: ``{object_type: {object_id: tuple_count}}``, bookkeeping relations excluded.
    counts_by_type: dict[str, dict[str, int]] = Field(default_factory=dict)


class GhostRevokeResponse(BaseModel):
    """What the catalog revoked, each object's tuple count, and what it refused to, with why."""

    revoked: dict[str, int] = Field(default_factory=dict)
    refused: dict[str, str] = Field(default_factory=dict)


class TenancyRebuildResponse(BaseModel):
    """The tenancy edges the catalog wrote, each ``project:<p> project warehouse:<id>``, and the warehouses whose record
    justifies none, with why."""

    written: list[str] = Field(default_factory=list)
    unjustified: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "CleanupDisarmed",
    "CloneEndingEntry",
    "CloneEndingFinishResponse",
    "CloneEndingsResponse",
    "FloorRaised",
    "GhostRevokeResponse",
    "GrantCensusResponse",
    "IdempotencyKeysReclaimResponse",
    "TenancyRebuildResponse",
    "TrashEndResponse",
    "TrashRefusalResponse",
    "TrashReleaseResponse",
]
