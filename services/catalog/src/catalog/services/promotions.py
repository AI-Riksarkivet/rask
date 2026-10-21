"""Promotion holds: a version the project's quality gate holds for a person, answered on the catalog (ADR 0156 C1, [[LH-466]]).

Readers of a governed tier read the version tagged ``published``, and only the publish door moves that tag once
the project's gate passes the version. When the project's declared gate has review on and a version breaches it,
the door records a HOLD instead: one record per table under ``_promotions/`` on the control root, with the held
version pinned by the tag :data:`HOLD_TAG` so version cleanup cannot collect it while it waits. A ``can_promote``
holder answers it: approval moves ``published`` to the held version, rejection restores main to the published
one. A hold nobody answers expires on the catalog's cron, and ``published`` stays where it was.

**ONE HOLD PER TABLE, ON ITS NEWEST HELD VERSION.** Main is linear, so a newer version carries the older one's
changes: a newer breaching publish replaces the open hold, and a publication that moves ``published`` to or past
the held version closes it as superseded. The last decision stays on the record until the table is held again,
so "who approved this" has an answer.

**A DECISION NAMES THE VERSION IT DECIDES, AND IS RECORDED BEFORE IT ACTS.** A record leaves ``open`` only through
an ETag-guarded write that finds it still open at that version (:func:`service_kit.lakehouse.records.mutate_json`).
So an approval cannot land on a newer hold the approver never reviewed, two people deciding one hold cannot both act,
and two replicas' sweeps expire one hold once.

**THE HOLD IS ON THE TABLE ITS UUID NAMES, AT ITS LOCATION** (ADR 0173 §8, [[LH-523]]). The record is keyed by the
table's uuid, so a rename keeps it and a table created later under the same name holds none of it; a decision also
checks the name still opens the dataset the hold was taken on. A hold on a table that is gone is not answerable and
expires at its deadline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from lance_namespace import InvalidTableStateError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from catalog.services import publication
from service_kit.lakehouse.record_store import get_record, list_records, record_key
from service_kit.lakehouse.records import RecordExistsError, RecordMissingError, create_json, mutate_json


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from lance_namespace import LanceNamespace

    from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

HOLDS_PREFIX: Final = "_promotions"
#: Pins the held version against version cleanup, which keeps only tagged versions past its window. Under the
#: platform's `rask.` prefix, which the spec tag doors refuse, so no client moves or deletes it.
HOLD_TAG: Final = "rask.held"

type HoldState = Literal["open", "approved", "rejected", "expired", "superseded"]


class Hold(BaseModel):
    """One table's hold: open, or the last decision on it."""

    model_config = ConfigDict(extra="forbid")

    #: The table's name (``acme-silver$orders``) when the version was held, for display.
    table: str
    #: The table's uuid, which the record is keyed by.
    uuid: str
    #: The dataset the hold was taken on, so a decision on a reused id is refused.
    location: str
    version: int = Field(ge=1)
    #: What ``published`` named when the version was held, ``None`` when nothing was published.
    published_version: int | None
    reasons: list[str]
    #: The verified subject who asked to publish, ``None`` with authentication off.
    requested_by: str | None
    #: The person the publication is for and the batch it belongs to, as the publish resolved them, so an
    #: approval announces the publication the publish would have.
    originator: str = ""
    cascade_id: str = ""
    opened_at: datetime
    expires_at: datetime
    state: HoldState = "open"
    decided_by: str | None = None
    decided_at: datetime | None = None
    #: A rejection owes main a restore when something was published to restore to; it is made once, and the
    #: version it committed is recorded here so a repeated rejection never restores again.
    restore_owed: bool = False
    restored_version: int | None = None


def hold_key(table_uuid: str) -> str:
    return record_key(HOLDS_PREFIX, "hold", table_uuid)


def _parse(raw: dict[str, Any] | None) -> Hold | None:
    """A stored record, or ``None`` for none; one this build cannot read is logged and treated as none."""
    if raw is None:
        return None
    try:
        return Hold.model_validate(raw)
    except ValidationError:
        log.warning("promotion_hold_unreadable", extra={"table": raw.get("table")})
        return None


def hold_of(root: str, so: StorageOptions, table_uuid: str) -> Hold | None:
    """The hold record of the table ``table_uuid`` names, open or decided, or ``None``."""
    return _parse(get_record(root, so, hold_key(table_uuid), event="promotion_hold"))


def open_holds(root: str, so: StorageOptions) -> list[Hold]:
    """Every open hold, in table order."""
    holds = (_parse(raw) for raw in list_records(root, so, HOLDS_PREFIX, event="promotion_hold"))
    return sorted((hold for hold in holds if hold is not None and hold.state == "open"), key=lambda hold: hold.table)


def open_hold(root: str, so: StorageOptions, held: Hold) -> Hold:
    """Record ``held`` as its table's open hold, replacing an older or decided one, and return the hold that stands.

    The same version held again keeps the hold that stands, deadline and all.

    Raises:
        InvalidTableStateError: A newer version of the table is held.
    """
    key = hold_key(held.uuid)
    record = held.model_dump(mode="json")
    try:
        create_json(root, so, key, record)
    except RecordExistsError:
        pass
    else:
        return held

    def _replace(current: dict[str, Any]) -> dict[str, Any]:
        standing = _parse(current)
        if standing is None or standing.state != "open" or standing.version < held.version:
            return record
        if standing.version > held.version:
            raise InvalidTableStateError(f"version {standing.version} of {held.table} is held for review and carries version {held.version}; decide on it")
        return current

    return Hold.model_validate(mutate_json(root, so, key, _replace))


def _transition(root: str, so: StorageOptions, table_uuid: str, step: Callable[[Hold], Hold]) -> Hold:
    """Rewrite the table's open hold as ``step`` answers, against the record as it is now.

    Raises:
        InvalidTableStateError: No hold is open on the table, or ``step`` refuses the one that is.
    """

    def _apply(current: dict[str, Any]) -> dict[str, Any]:
        standing = _parse(current)
        if standing is None or standing.state != "open":
            raise InvalidTableStateError("the table holds no version for review")
        return step(standing).model_dump(mode="json")

    try:
        return Hold.model_validate(mutate_json(root, so, hold_key(table_uuid), _apply))
    except RecordMissingError:
        raise InvalidTableStateError("the table holds no version for review") from None


def decide(
    root: str,
    so: StorageOptions,
    table_uuid: str,
    *,
    version: int,
    location: str,
    state: Literal["approved", "rejected"],
    by: str | None,
    at: datetime,
    restore_owed: bool = False,
) -> tuple[Hold, bool]:
    """Record the decision on the table's open hold on ``version`` at ``location``, before the door acts on it.

    Returns the hold as decided and whether this call decided it. The same decision asked again returns the hold as
    first decided, so a decision whose effect failed after it was recorded can be completed. Recorded first, so two
    people deciding one hold at once cannot both act: the write that finds the hold still open wins, and the other
    is refused. ``restore_owed`` is a rejection's: main is to be restored to the published version.

    Raises:
        InvalidTableStateError: The table holds no such version of the dataset at ``location`` for review.
    """
    claimed = False

    def _decide(current: dict[str, Any]) -> dict[str, Any]:
        nonlocal claimed
        standing = _parse(current)
        if standing is None or standing.version != version or standing.location != location:
            raise InvalidTableStateError(f"the table holds no version {version} for review")
        claimed = False
        if standing.state == state:
            return current
        if standing.state != "open":
            raise InvalidTableStateError(f"version {version} of the table is {standing.state}, no longer held for review")
        claimed = True
        return standing.model_copy(update={"state": state, "decided_by": by, "decided_at": at, "restore_owed": restore_owed}).model_dump(mode="json")

    try:
        return Hold.model_validate(mutate_json(root, so, hold_key(table_uuid), _decide)), claimed
    except RecordMissingError:
        raise InvalidTableStateError(f"the table holds no version {version} for review") from None


def record_restore(root: str, so: StorageOptions, table_uuid: str, *, version: int, restored: int | None) -> Hold:
    """Record that the rejection of ``version`` settled what it owed main: restored as ``restored``, or ``None`` when
    there turned out to be nothing to restore. The hold as recorded is returned.

    Raises:
        InvalidTableStateError: The table's hold is not the rejection of ``version``.
    """

    def _settle(current: dict[str, Any]) -> dict[str, Any]:
        standing = _parse(current)
        if standing is None or standing.version != version or standing.state != "rejected":
            raise InvalidTableStateError(f"the table holds no rejection of version {version}")
        if not standing.restore_owed:
            return current
        return standing.model_copy(update={"restore_owed": False, "restored_version": restored}).model_dump(mode="json")

    try:
        return Hold.model_validate(mutate_json(root, so, hold_key(table_uuid), _settle))
    except RecordMissingError:
        raise InvalidTableStateError(f"the table holds no rejection of version {version}") from None


def supersede(root: str, so: StorageOptions, table_uuid: str, *, published: int, at: datetime) -> Hold | None:
    """Close the table's open hold when ``published`` reached its version; the closed hold, else ``None``."""
    hold = hold_of(root, so, table_uuid)
    if hold is None or hold.state != "open" or hold.version > published:
        return None

    def _supersede(standing: Hold) -> Hold:
        if standing.version > published:
            raise InvalidTableStateError(f"the table holds version {standing.version}, past {published}")
        return standing.model_copy(update={"state": "superseded", "decided_at": at})

    try:
        return _transition(root, so, table_uuid, _supersede)
    except InvalidTableStateError:
        return None


def due(root: str, so: StorageOptions, *, now: datetime) -> list[Hold]:
    """Every open hold whose deadline has passed, in table order."""
    return [hold for hold in open_holds(root, so) if hold.expires_at <= now]


def expire(root: str, so: StorageOptions, hold: Hold, *, now: datetime) -> Hold | None:
    """Expire ``hold`` if it is still open on the same version past its deadline; the expired hold, else ``None``."""

    def _expire(standing: Hold) -> Hold:
        if standing.version != hold.version or standing.expires_at > now:
            raise InvalidTableStateError(f"{standing.table} was held again")
        return standing.model_copy(update={"state": "expired", "decided_at": now})

    try:
        return _transition(root, so, hold.uuid, _expire)
    except InvalidTableStateError:
        return None  # decided, expired by another replica, or held again since it was listed


def deadline(opened_at: datetime, hours: float) -> datetime:
    return opened_at + timedelta(hours=hours)


def sync_pin(ns: LanceNamespace, so: dict[str, str], table_id: Sequence[str], hold: Hold | None) -> None:
    """Point :data:`HOLD_TAG` at the hold's version while it is open, and remove it otherwise."""
    if hold is not None and hold.state == "open":
        publication.set_tag(ns, so, table_id, HOLD_TAG, hold.version)
    else:
        publication.drop_tag(ns, so, table_id, HOLD_TAG)
