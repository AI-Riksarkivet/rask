"""Re-deriving what one subject may see — the invariant the whole plane rests on.

**The bus is UNGOVERNED.** A subscriber receives every tenant's runs, so a pointer written straight
off a delivery would be an authorization decision made by whoever published the event. Visibility is
therefore re-derived TWICE against the same rule the lineage read path already enforces: at DELIVERY,
before a pointer is written, and again at RENDER, when the panel resolves stored pointers.

The rule is `can_get_metadata` on `table:<dataset>`, batch-checked — one round-trip over the whole
candidate set, never one `check` per object. Three things about it are settled rather than assumed:

* `table#can_get_metadata` is bare `reader`, and the concurrent change that gives `warehouse` and
  `namespace` upward visibility (`reader or can_get_metadata from child`) touches the two CONTAINER
  types only. So the widening does not reach this plane at all.
* The lineage governed path checks `table:` objects — `fga_object_type` defaults to `"table"` and is
  set by nothing in the chart — and this plane notifies on terminal states whose outputs are
  datasets. Same type, same relation, same answer.
* The tighter relation a PUSH really wants (`can_be_notified: reader` on the notifiable types) is a
  coordinated model change, and on `table` it would be the same set as today — so it costs nothing
  now and becomes load-bearing only when a container-scoped event can be notified on.

`can_get_metadata` answers "is there anything beneath this you may see?" — the right question for
rendering a breadcrumb and the wrong one for interrupting someone. On `table` the two coincide, which
is why S1 is free to ask this one.
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

#: The relation a render asks for. Delivery asks for it too, today — see the module docstring.
METADATA_RELATION: Final = "can_get_metadata"


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

    async def visible(self, subject: str, names: Collection[str]) -> set[str]:
        """The subset of `names` that `subject` may read, in one round-trip.

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
        allowed = await fga.batch_check(
            self._client,
            user=subject,
            relation=METADATA_RELATION,
            objects=[f"{FGA_OBJECT_TYPE}:{name}" for name in names],
        )
        return {name for name in names if allowed.get(f"{FGA_OBJECT_TYPE}:{name}")}

    async def sees_all(self, subject: str, names: Collection[str]) -> bool:
        """Whether `subject` may see EVERY name — the subset test, and the half people get wrong.

        `names <= visible` rather than "any of them": one invisible output drops the row. A run that
        wrote a dataset you may read and one you may not is a run you are not told about, because
        being told names the run, its author and its outcome.
        """
        return set(names) <= await self.visible(subject, names)
