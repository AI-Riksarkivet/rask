"""The end of a registered clone, filed before its row goes and removed once its holds are released (ADR 0198 §1).

A registered shallow clone holds a tag on each source it reads through (:mod:`lakehouse.clone_sources`), named for its
location. Every door that ends the clone removes its row, then releases those tags, then its location's claim; a store
error on a source's tags, or a process dying, between the row and the tags would leave a tag standing for good, and the
source undestroyable, since a re-issued call no longer finds the row. So the door files an ENDING before the row goes,
naming the clone's location, its uuid, the source locations its tags stand on and whether its base record goes with its
bytes, and removes it last; a door whose native call failed leaves it, since no read tells it whether the row it then
finds is still the clone's. An ending a door did not remove is finished by the catalog's finish door, which maintenance's hourly
audit asks (``catalog.services.clone_endings``): what to release was decided by the door that ended the clone, never
guessed by a reader, so no finish can end a live clone's hold.

ONE OBJECT PER FILING, ``_clone_endings/<sha256 of the decoded location>/<filing token>.json``: the location's digest is
the one its claim and its tag are keyed by, and the token is the filing request's. Each object has one writer, the door
that filed it, and one other actor, a finish, which marks it under the ETag it read before acting, so neither an ending
filed again at the location nor a second finish is ever acted on by a finish that read an earlier one. An ending is
removed by a plain delete once its holds are released: a delete of an object no one else writes needs no condition, and
an ending ended is gone, so the listing reads only what stands.

AN ENDING STANDING AT A LOCATION refuses a registration there (``register_table``): the clone registered next would hold
tags of the same name, which a finish could then release.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, Field

from lakehouse import records
from lakehouse.location_claims import ClaimStore, claim_digest
from lakehouse.record_store import delete_record
from lakehouse.stored_objects import list_under


PREFIX: Final = "_clone_endings"
_SUFFIX: Final = ".json"


class EndingMark(BaseModel):
    """A finish acting on the ending: while it stands, another finish refuses."""

    token: str
    at: datetime


class EndingIntent(BaseModel):
    """What a door ending the clone at ``location`` decided to release."""

    #: The clone's location as the door read it, the catalog's spelling: what the claim, the base record and the tag
    #: name are keyed off.
    location: str
    #: The clone's canonical table id when the door ended it: the holder of the location's claim it releases.
    table: str
    #: The clone's uuid, as its location's claim recorded it then; ``None`` for a table no uuid identifies ([[LH-534]]).
    uuid: str | None = None
    #: Each source table's root the clone's tags stand on, as the clone's base record names it (``derived_from``).
    sources: list[str] = Field(default_factory=list)
    #: Whether the clone's base record goes with it (its bytes are destroyed), or stays for the bytes left in place.
    forget_record: bool
    #: The door that filed it, for an operator.
    door: str

    def names(self, uuid: str | None, table: str) -> bool:
        """Whether a claim recording ``uuid`` for ``table`` is the clone this ending ends: by its uuid, or by its name
        where it records none."""
        return uuid == self.uuid if self.uuid is not None else (uuid is None and table == self.table)


class CloneEnding(EndingIntent):
    """What the door ending the clone at ``location`` will release, filed for request ``filing``, not yet removed."""

    #: The filing request: its own door removes the ending, and the ending is addressed by it.
    filing: str
    #: When it was filed, on the store's clock; a finish leaves it alone until a claim's lease has passed since.
    filed_at: datetime
    finishing: EndingMark | None = None

    @property
    def digest(self) -> str:
        return digest_of(self.location)


class EndingRead(BaseModel):
    """An ending as one read found it, with the ETag a write acting on it must still match and the store's clock then."""

    ending: CloneEnding
    etag: str
    at: datetime


class EndingChangedError(Exception):
    """The ending is no longer the one the request read."""


def digest_of(location: str) -> str:
    """The digest the endings of the clone at ``location``, in either spelling, are kept under: its claim's and its tag's."""
    return claim_digest(location)


def _key(digest: str, filing: str) -> str:
    return f"{PREFIX}/{digest}/{filing}{_SUFFIX}"


def file(store: ClaimStore, intent: EndingIntent, *, filing: str) -> CloneEnding:
    """File ``intent`` for the request ``filing``, before its door removes anything.

    Raises:
        records.RecordExistsError: The request filed an ending at the location already.
        OSError: The store refused a read or the write.
    """
    at = records.read_json_at(store.control_root, store.storage_options, _key(digest_of(intent.location), filing)).at
    ending = CloneEnding(**intent.model_dump(), filing=filing, filed_at=at)
    records.create_json(store.control_root, store.storage_options, _key(ending.digest, filing), ending.model_dump(mode="json"))
    return ending


def standing(store: ClaimStore) -> list[tuple[str, str]]:
    """The ``(digest, filing)`` of every ending standing, off one listing of the prefix: no ending is read.

    Raises:
        OSError: The store refused the listing.
    """
    found: list[tuple[str, str]] = []
    for stored in list_under(f"{store.control_root.rstrip('/')}/{PREFIX}", store.storage_options):
        *_, digest, name = stored.path.rsplit("/", 2)
        if name.endswith(_SUFFIX):
            found.append((digest, name.removesuffix(_SUFFIX)))
    return found


def stands_at(store: ClaimStore, location: str) -> bool:
    """Whether an ending stands at ``location``, in either spelling, off one listing of its digest.

    Raises:
        OSError: The store refused the listing.
    """
    return bool(list_under(f"{store.control_root.rstrip('/')}/{PREFIX}/{digest_of(location)}", store.storage_options))


def read(store: ClaimStore, digest: str, filing: str) -> EndingRead | None:
    """The ending ``filing`` filed under ``digest``, with its ETag and the store's clock; ``None`` when none stands.

    Raises:
        ValueError: The stored record is not an ending.
    """
    found = records.read_json_at(store.control_root, store.storage_options, _key(digest, filing))
    if found.stored is None:
        return None
    return EndingRead(ending=CloneEnding.model_validate(found.stored.record), etag=found.stored.etag, at=found.at)


def remove(store: ClaimStore, ending: CloneEnding) -> bool:
    """Remove ``ending``, its holds released; ``False`` when it was gone already.

    Raises:
        OSError: The store refused the delete.
    """
    return delete_record(store.control_root, store.storage_options, _key(ending.digest, ending.filing))


def mark(store: ClaimStore, read_as: EndingRead, *, token: str) -> None:
    """Mark the ending ``read_as`` found as being finished by request ``token``, under that read's ETag.

    Raises:
        EndingChangedError: The ending changed since that read.
    """
    marked = read_as.ending.model_copy(update={"finishing": EndingMark(token=token, at=read_as.at)})
    try:
        records.replace_json(store.control_root, store.storage_options, _key(marked.digest, marked.filing), marked.model_dump(mode="json"), etag=read_as.etag)
    except records.RecordChangedError:
        raise EndingChangedError(f"the ending {marked.digest}/{marked.filing} changed since it was read") from None
