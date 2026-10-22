"""Re-deriving what one subject may see — the invariant the whole plane rests on.

**The bus is UNGOVERNED.** A subscriber receives every tenant's runs, so a pointer written straight
off a delivery would be an authorization decision made by whoever published the event. Visibility is
therefore re-derived TWICE against the same rule the lineage read path already enforces: at DELIVERY,
before a pointer is written, and again at RENDER, when the panel resolves stored pointers.

**THE TWO CHECKS ASK DIFFERENT QUESTIONS, and since S4 they ask different RELATIONS.**

* **Delivery** asks `can_be_notified` — bare `reader` on the notifiable types. A notification asserts
  a stake in the OBJECT ITSELF.
* **Render** asks `can_get_metadata` — `reader or can_get_metadata from child` on containers since
  C1. It answers "is there anything beneath this you may see?", which is right for resolving a
  breadcrumb (and for resolving a pointer already in someone's inbox) and wrong for interrupting
  someone.

Both are batch-checked on `table:<dataset>` — one round-trip over the whole candidate set, never one
`check` per object. Two things about the pair are settled rather than assumed:

* On `table` and `materialized_view` the two relations are the SAME SET (both bare `reader`), so
  splitting them changed no audience on the day it landed. That is what made it safe to do before any
  container-scoped event exists to need it.
* The lineage governed path checks `table:` objects — `fga_object_type` defaults to `"table"` and is
  set by nothing in the chart — and this plane notifies on terminal states whose outputs are
  datasets. Same type, same answer.

The split becomes load-bearing the moment a governance event names a `namespace` or `warehouse`: under
one-rule-everywhere that event would be delivered to every subject holding `reader` on any single
table anywhere beneath it — an audience nobody chose, arriving as a push.
"""

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING, Final

from notifications.models import WATCH_RELATION
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed import fga


if TYPE_CHECKING:
    from service_kit.governed.decider import AccessDecider


log = logging.getLogger(__name__)

#: What a RENDER asks: "is there anything beneath this you may see?". Permissive on containers by
#: design (C1's `child` edge), which is right — a breadcrumb to your own data must resolve.
METADATA_RELATION: Final = "can_get_metadata"

#: What a DELIVERY asks: "do you have a stake in this object itself?". Bare `reader` on every
#: notifiable type, so on a leaf it is the same set as `METADATA_RELATION` and on a container it is
#: strictly tighter. S4 landed the relation; until then this path asked the render's question and got
#: away with it because the plane only ever notified on `table:` outputs.
NOTIFY_RELATION: Final = "can_be_notified"


def _as_object(object_id: str) -> str | None:
    """The FGA object one pointer's `object_id` names, or ``None`` when it names none.

    A pointer's `object_id` is the object as OpenFGA holds it, from either lane: a lineage run's output as the table its
    uuid names (`table:<uuid>`, [[LH-524]]), a governance event's own `object_id` (`warehouse:acme`,
    `namespace:<uuid>`). A prefix is honoured only when it names a type the model defines: anything else (an output
    named like a URI, `s3://bucket/run1`) is no object, and is not asked about, so a delivery never retries forever
    on OpenFGA's `type 's3' not found`.
    """
    kind, sep, _rest = object_id.partition(":")
    return object_id if sep and kind in fga.model_types() else None


class Visibility:
    """One subject-agnostic view onto OpenFGA, asked per subject.

    Subject-agnostic because the two callers differ: the HTTP door already knows whose request it is,
    while the bus handler is deriving an AUDIENCE and asks the same question of each candidate. Making
    the subject a parameter rather than constructor state is what lets one instance serve a fan-out
    without a per-recipient object.
    """

    def __init__(self, *, decider: "AccessDecider | None", enabled: bool) -> None:
        self._decider = decider
        self._enabled = enabled

    async def _filter(self, subject: str, names: Collection[str], relation: str) -> set[str]:
        """The subset of `names` that `subject` holds `relation` on, in one round-trip.

        The empty set short-circuits BEFORE the unwired-client refusal, matching the estate's existing
        filter. That is not a hole: an empty candidate set yields an empty answer either way, so there
        is nothing an outage could disclose by being answered — while 503ing a page that had no rows
        to filter would turn "your inbox is empty" into an error.
        """
        if not self._enabled or not names:
            return set(names)
        if self._decider is None:
            # The middle case of the three-outcome rule, and the one that must never be permissive:
            # FGA on with no client is a BROKEN authorization layer, and answering it open turns that
            # into an open one, silently.
            raise ServiceUnavailableError("authorization is enabled but unavailable")
        objects = {name: obj for name in names if (obj := _as_object(name)) is not None}
        if not objects:
            return set()
        allowed = await self._decider.batch_check(user=subject, relation=relation, objects=list(objects.values()))
        return {name for name, obj in objects.items() if allowed.get(obj)}

    async def visible(self, subject: str, names: Collection[str]) -> set[str]:
        """RENDER: the subset of `names` the subject may SEE. Stays on `can_get_metadata`.

        Permissive by design, and it must stay that way: a pointer already in someone's inbox resolves
        through this, and tightening it here would blank rows the delivery gate had already approved.
        """
        return await self._filter(subject, names, METADATA_RELATION)

    async def still_a_member(self, subject: str, project_id: str) -> bool:
        """Whether `subject` still holds `member` on `project:<id>` — re-checked at DELIVERY.

        `project#member` gates watch CREATION, and three docstrings in this plane asserted it was
        re-checked when a watch is delivered on. It was not. For most subjects the visibility gate
        catches it anyway, because the FGA model routes membership into readership
        (`warehouse#reader: … or member from project`, inherited down to `table#can_be_notified`) — so
        revoking membership drops them. The residue is a subject holding an INDEPENDENT reader grant
        on the output: they kept receiving `reason: watch` rows for a project they were offboarded
        from, indefinitely, since only they can delete their own watch.

        FAIL-CLOSED, matching every other gate here: authorization enabled with no client is a BROKEN
        authorization layer, and answering it open turns that into an open one. The caller maps the
        refusal to RETRY rather than to a silent drop.
        """
        if not self._enabled or not project_id:
            return True
        if self._decider is None:
            raise ServiceUnavailableError("authorization is enabled but unavailable")
        return await self._decider.check(user=subject, relation=WATCH_RELATION, obj=f"project:{project_id}")

    async def sees_all(self, subject: str, names: Collection[str]) -> bool:
        """DELIVERY: whether `subject` may be notified about EVERY name — the subset test, and the
        half people get wrong.

        `names <= allowed` rather than "any of them": one un-notifiable output drops the row. A run
        that wrote a dataset you have a stake in and one you do not is a run you are not told about,
        because being told names the run, its author and its outcome.

        Asks `can_be_notified`, NOT the render's question. On today's `table:` outputs the two are the
        same set, so this changed no audience the day it landed — the split is what keeps a
        container-scoped event (S4's governance targeting) from reaching everyone holding a grant on
        anything beneath it.
        """
        return set(names) <= await self._filter(subject, names, NOTIFY_RELATION)
