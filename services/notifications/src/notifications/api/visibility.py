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

from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed import fga


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


log = logging.getLogger(__name__)

#: The FGA type every notifiable object is checked as. A CONSTANT rather than a setting on purpose:
#: it is a coordinate the estate has to agree on, and a per-service knob is exactly how two services
#: end up authorizing against different object types while both look configured. lineage carries the
#: same value as a default that nothing in `chart/` overrides.
FGA_OBJECT_TYPE: Final = "table"

#: What a RENDER asks: "is there anything beneath this you may see?". Permissive on containers by
#: design (C1's `child` edge), which is right — a breadcrumb to your own data must resolve.
METADATA_RELATION: Final = "can_get_metadata"

#: What a DELIVERY asks: "do you have a stake in this object itself?". Bare `reader` on every
#: notifiable type, so on a leaf it is the same set as `METADATA_RELATION` and on a container it is
#: strictly tighter. S4 landed the relation; until then this path asked the render's question and got
#: away with it because the plane only ever notified on `table:` outputs.
NOTIFY_RELATION: Final = "can_be_notified"


def _as_object(name: str) -> str:
    """The FGA object for one pointer's `object_id` — qualified, but never qualified TWICE.

    The plane has two pointer sources and they spell this differently, which is the whole reason this
    function exists rather than an f-string at the call site:

    * a LINEAGE run stamps the BARE dataset name (`notifiable()` takes `outputs[0]`, e.g.
      `silver$pages`), so it needs the type prefix;
    * a GOVERNANCE event stamps the CANONICAL id, already carrying its type —
      `CatalogControlEvent.object_id` is documented as "e.g. `warehouse:acme`, `table:db1$t`,
      `namespace:db1`" — and `as_delivery` copies it through verbatim.

    Prefixing unconditionally checked every v3 row as `table:table:db1$t`, an object that cannot
    resolve, so every governance notification was filtered out of the feed permanently. The badge does
    NOT go through this filter (`/inbox/unread` is answered from the actor's own partition), so the
    result was a badge counting a row the list would never show — an unclearable badge, which is the
    precise failure this plane exists to end.

    Honouring the stamped type is better than merely un-breaking it: `can_get_metadata` is defined on
    the container types as well, so a grant on a warehouse is now asked about the WAREHOUSE rather
    than about a table that never existed.

    A dataset name cannot collide with this: the estate's are `<namespace>$<table>`, delimiter-joined
    and colon-free. An output named with a URI-ish namespace would pass through unqualified and answer
    False — exactly as it answers False today under the prefix, since no tuple exists for it either.
    """
    return name if ":" in name else f"{FGA_OBJECT_TYPE}:{name}"


class Visibility:
    """One subject-agnostic view onto OpenFGA, asked per subject.

    Subject-agnostic because the two callers differ: the HTTP door already knows whose request it is,
    while the bus handler is deriving an AUDIENCE and asks the same question of each candidate. Making
    the subject a parameter rather than constructor state is what lets one instance serve a fan-out
    without a per-recipient object.
    """

    def __init__(self, *, client: "OpenFgaClient | None", enabled: bool) -> None:
        self._client = client
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
        if self._client is None:
            # The middle case of the three-outcome rule, and the one that must never be permissive:
            # FGA on with no client is a BROKEN authorization layer, and answering it open turns that
            # into an open one, silently.
            raise ServiceUnavailableError("authorization is enabled but unavailable")
        objects = {name: _as_object(name) for name in names}
        allowed = await fga.batch_check(
            self._client,
            user=subject,
            relation=relation,
            objects=list(objects.values()),
        )
        return {name for name, obj in objects.items() if allowed.get(obj)}

    async def visible(self, subject: str, names: Collection[str]) -> set[str]:
        """RENDER: the subset of `names` the subject may SEE. Stays on `can_get_metadata`.

        Permissive by design, and it must stay that way: a pointer already in someone's inbox resolves
        through this, and tightening it here would blank rows the delivery gate had already approved.
        """
        return await self._filter(subject, names, METADATA_RELATION)

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
