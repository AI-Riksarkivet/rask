"""An erasure obligation: a subject's erasure the catalog accepted for one table and holds open until it has verified it.

[[LH-493]], ADR 0163. The erasure door records the obligation and answers. A bring-your-own job holding `writer` and
`maintainer` rewrites on its own compute, each commit judged at `version/create`. The verification door judges what the
job left (`erasure.verify`) and releases the obligation only when every head is clean and nothing but a scheduled pin
release keeps a residual. The catalog never does the job's work and never retries it: a missed deadline is visible on
the obligation, and nothing acts on it.

**ONE RECORD PER OBLIGATION ON THE CONTROL ROOT** (`_erasure_obligations/<id>.json`), created put-if-not-exists and
changed only through an ETag-guarded replace (`service_kit.lakehouse.records.mutate_json`), so two verifications of one
obligation cannot lose each other's write, and a released obligation never goes back to open.

**AN OBLIGATION ON A DERIVED TABLE NAMES THE ONE IT FOLLOWS** ([[LH-332]], ADR 0178). A verification holds its obligation
open until every table the edge list records as derived from its table holds an obligation following it, open or
released, directly or through a chain (:func:`followed_tables`). The link is the obligation's own ``follows``, never an
obligation recorded on the derived table later, because one recorded there for another subject would release this one with
its subject forgotten in that table.

**THE PREDICATE IS NEVER STORED, ONLY ITS DIGEST, AND THE DIGEST GOES AT RELEASE.** The predicate names the subject
([[LH-281]]). The record keeps the SHA-256 of its exact text so that a verification is held to the predicate the
requester asked to erase: a job presenting a weaker one, say one nothing matches, would otherwise verify clean and
release the obligation while the subject stays. A digest of a short identifier can be confirmed by guessing, so it binds
rather than hides; while the obligation is open the table itself still holds what it would confirm, and the digest is
wiped when the obligation is released.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from typing import Any, Final, Literal

from lance_namespace import InvalidInputError, InvalidTableStateError
from pydantic import BaseModel, ConfigDict, Field

from catalog.services.erasure import OPEN_OUTCOMES, ErasureReport, PinRelease, SurfaceResult, shown
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import list_records
from service_kit.lakehouse.records import RecordMissingError, create_json, mutate_json, read_json


log = logging.getLogger(__name__)

OBLIGATION_PREFIX: Final = "_erasure_obligations"

#: An obligation id as the door mints it (`uuid4().hex`), so a path segment can never name another control-root key.
_OBLIGATION_ID: Final = re.compile(r"[0-9a-f]{32}")

type ObligationState = Literal["open", "released"]


class ErasureObligationView(BaseModel):
    """An erasure obligation as the doors show it: its predicate's digest and its storage location stay on the record."""

    obligation_id: str = Field(description="The obligation's id, which the verification door names.")
    table: str = Field(description="The table's name now, or the name the obligation kept when the table is gone.")
    table_uuid: str = Field(description="The uuid of the table the erasure was asked of (ADR 0173 §11): a rename keeps it.")
    refs: dict[str, int] = Field(
        description=(
            "`main` and every branch the table listed when the obligation was recorded, each with its head version then: an index "
            "segment committed after it is never taken as proof, so the job drops its indexes and rebuilds them after the release."
        )
    )
    retain_days: int = Field(description="The version history the requester keeps, in days: each verification reclaims everything older.")
    requested_by: str | None = Field(description="Who asked for the erasure: `user:` and the principal key their token proved.")
    requested_at: datetime = Field(description="When the obligation was recorded.")
    deadline: datetime = Field(description="When the erasure is due. Nothing extends it, and nothing acts on it when it passes.")
    follows: str | None = Field(
        description=(
            "The erasure obligation, on a table this one was derived from, that this one follows: that obligation stays open until "
            "every table derived from its table holds one following it, directly or through a chain."
        )
    )
    overdue: bool = Field(description="True while the obligation is open past its deadline.")
    state: ObligationState = Field(description="`open` until a verification settles it, then `released`.")
    verified_at: datetime | None = Field(description="When the last verification ran; null before the first.")
    open_surfaces: list[SurfaceResult] = Field(
        description=(
            "Every surface the last verification left open: what the job still owes, a step that did not go through, bytes nothing rewrites, "
            "a derived table no obligation follows this one on. Derived tables the reader may not see are counted, never named."
        )
    )
    released_at: datetime | None = Field(description="When a verification released it; null while open.")
    release: PinRelease | None = Field(description="The release of the refs that pinned a residual, scheduled when the obligation was released.")


class ErasureObligation(BaseModel):
    """One obligation, as it lives on the control root."""

    model_config = ConfigDict(extra="forbid")

    obligation_id: str
    #: The table's name when the erasure was asked of it, for the log and a table that is gone.
    table: str
    #: The table's uuid (ADR 0173 §8): a verification acts on this table and no other, whatever it is named.
    table_uuid: str
    #: The canonical FGA object (``table:<id>``) the table was when the obligation was recorded.
    object_id: str
    #: The dataset root it was recorded on: a verification acts on these bytes and on no other table's.
    location: str
    #: SHA-256 of the predicate's exact text; empty once released.
    predicate_digest: str
    retain_days: int
    #: Each ref's head version when the obligation was recorded: `erasure.verify`'s ``trusted``.
    refs: dict[str, int]
    requested_by: str | None
    requested_at: datetime
    deadline: datetime
    #: The obligation this one follows, on a table this one was derived from ([[LH-332]]).
    follows: str | None = None
    state: ObligationState = "open"
    verified_at: datetime | None = None
    open_surfaces: list[SurfaceResult] = Field(default_factory=list)
    released_at: datetime | None = None
    release: PinRelease | None = None

    def view(self, now: datetime, *, table: str, hidden: Collection[str]) -> ErasureObligationView:
        """The record as the doors show it at ``now``, naming its table ``table``, to a reader who may not see the derived
        tables ``hidden`` keys (:func:`catalog.services.erasure.shown`)."""
        return ErasureObligationView(
            obligation_id=self.obligation_id,
            table=table,
            table_uuid=self.table_uuid,
            refs=self.refs,
            retain_days=self.retain_days,
            requested_by=self.requested_by,
            requested_at=self.requested_at,
            deadline=self.deadline,
            follows=self.follows,
            overdue=self.state == "open" and now > self.deadline,
            state=self.state,
            verified_at=self.verified_at,
            open_surfaces=shown(self.open_surfaces, hidden=hidden),
            released_at=self.released_at,
            release=self.release,
        )


class ErasureVerification(BaseModel):
    """What one verification found, and the obligation as it stands after it."""

    obligation: ErasureObligationView
    report: ErasureReport


class ErasureObligations(BaseModel):
    """One page of the obligations a caller may maintain, the earliest deadline first."""

    obligations: list[ErasureObligationView]
    page_token: str | None = Field(default=None, description="Pass back to read the next page; null on the last one.")


def digest(predicate: str) -> str:
    """The SHA-256 of ``predicate``'s exact text, the only trace of it an obligation keeps."""
    return hashlib.sha256(predicate.encode()).hexdigest()


def record_key(obligation_id: str) -> str:
    return f"{OBLIGATION_PREFIX}/{obligation_id}.json"


def record(control_root: str, storage_options: StorageOptions, obligation: ErasureObligation) -> None:
    """Write ``obligation`` where the doors find it; raises when the store does not take it."""
    create_json(control_root, storage_options, record_key(obligation.obligation_id), obligation.model_dump(mode="json"))


def read(control_root: str, storage_options: StorageOptions, obligation_id: str) -> ErasureObligation:
    """The obligation ``obligation_id`` names.

    Raises:
        InvalidInputError: ``obligation_id`` is not an id the door mints.
        InvalidTableStateError: No such obligation is recorded.
    """
    if not _OBLIGATION_ID.fullmatch(obligation_id):
        raise InvalidInputError(f"{obligation_id!r} is not an erasure obligation id")
    found = read_json(control_root, storage_options, record_key(obligation_id))
    if found is None:
        raise InvalidTableStateError(f"no erasure obligation {obligation_id} is recorded")
    return ErasureObligation.model_validate(found[0])


def list_obligations(control_root: str, storage_options: StorageOptions) -> list[ErasureObligation]:
    """Every obligation the control root holds; one that cannot be read is logged and left out."""
    obligations: list[ErasureObligation] = []
    for raw in list_records(control_root, storage_options, OBLIGATION_PREFIX, event="erasure_obligation"):
        try:
            obligations.append(ErasureObligation.model_validate(raw))
        except ValueError as exc:
            log.error("erasure_obligation_unreadable", extra={"obligation": str(raw.get("obligation_id", ""))[:64], "error": str(exc)})
    return obligations


def require_followable(control_root: str, storage_options: StorageOptions, obligation_id: str) -> None:
    """Refuse an obligation following ``obligation_id`` unless that names an obligation the catalog recorded.

    Raises:
        InvalidInputError: ``obligation_id`` is not an obligation id, or no such obligation is recorded.
    """
    try:
        read(control_root, storage_options, obligation_id)
    except InvalidTableStateError as exc:
        raise InvalidInputError(f"follows names no erasure obligation: {exc}") from exc


def followed_tables(obligations: Sequence[ErasureObligation], source: str) -> set[str]:
    """The uuids of the tables an obligation following ``source`` is recorded on, open or released: one naming it in
    ``follows``, or naming an obligation that follows it, at any depth.

    A chain counts because a table derived from ``source``'s through another is that other table's derived table too, and
    its obligation names the one it was derived from directly. One whose chain reaches an obligation that is not listed
    follows nothing this can confirm.
    """
    by_id = {obligation.obligation_id: obligation for obligation in obligations}
    return {obligation.table_uuid for obligation in obligations if _reaches(obligation, source, by_id)}


def _reaches(obligation: ErasureObligation, source: str, by_id: dict[str, ErasureObligation]) -> bool:
    seen: set[str] = set()
    step = obligation.follows
    while step is not None and step not in seen:
        if step == source:
            return True
        seen.add(step)
        followed = by_id.get(step)
        step = followed.follows if followed is not None else None
    return False


def require_verifiable(obligation: ErasureObligation, *, table_uuid: str, location: str, predicate: str) -> None:
    """Refuse a verification of ``obligation`` on the table ``table_uuid`` names, at ``location``, with ``predicate``.

    Raises:
        InvalidTableStateError: The obligation was recorded on another table, or it is already released.
        InvalidInputError: ``predicate`` is not the one the obligation was recorded with.
    """
    if obligation.table_uuid != table_uuid or obligation.location != location:
        raise InvalidTableStateError(f"this table holds no erasure obligation {obligation.obligation_id}")
    if obligation.state == "released":
        raise InvalidTableStateError(f"erasure obligation {obligation.obligation_id} was released at {obligation.released_at}")
    if not hmac.compare_digest(digest(predicate), obligation.predicate_digest):
        raise InvalidInputError(f"the predicate is not the one erasure obligation {obligation.obligation_id} was recorded with")


def settle(control_root: str, storage_options: StorageOptions, obligation_id: str, *, report: ErasureReport, now: datetime) -> ErasureObligation:
    """Record what ``report`` found on the obligation: released when it settled, else open, naming every open surface.

    A released obligation stays released whatever a later report says.

    Raises:
        InvalidTableStateError: The obligation is no longer recorded.
    """
    try:
        return ErasureObligation.model_validate(mutate_json(control_root, storage_options, record_key(obligation_id), _settling(report, now)))
    except RecordMissingError as exc:
        raise InvalidTableStateError(f"no erasure obligation {obligation_id} is recorded") from exc


def _settling(report: ErasureReport, now: datetime) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def apply(current: dict[str, Any]) -> dict[str, Any]:
        standing = ErasureObligation.model_validate(current)
        if standing.state == "released":
            return current
        if report.settled:
            released = {"state": "released", "predicate_digest": "", "released_at": now, "verified_at": now, "open_surfaces": [], "release": report.release}
            return standing.model_copy(update=released).model_dump(mode="json")
        opened = [surface for surface in report.surfaces if surface.outcome in OPEN_OUTCOMES]
        return standing.model_copy(update={"verified_at": now, "open_surfaces": opened}).model_dump(mode="json")

    return apply
