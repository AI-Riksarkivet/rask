"""The project-watch index — one virtual actor per PROJECT, listing the subjects watching it.

**Why an index actor at all.** Inbox actors are one-per-subject, so nothing in this service can
answer the question a fan-out actually asks: "who is watching project X?". Without it the only way
to widen an audience beyond authorship would be to enumerate every subject and ask each one, which is
the shape that does not scale and the shape OpenFGA's own `list_users` exists to avoid.

**Membership GATES watching; it never implies it.** The estate's standing rule (D4/§6), and the whole
reason this registry is application state rather than tuples: "alice wants to be told about project
X" is a PREFERENCE, not a permission. Modelling it as tuples would put per-user mutable state on the
authorization hot path, and `core-separation` says tuples are authorization facts. The permission —
`project#member` — is checked at watch-create, against the project, which is the object that exists
(create-on-parent doctrine). It is re-checked at DELIVERY, because membership can be revoked between
the watch and the run.

**Registration is synchronous and idempotent**, the `tenant_actor` shape: a retried watch must not
double-list, and a failure must fail the request loudly rather than leave a watch that the settings
page shows and the fan-out never reads. An unlisted-but-believed watch is worse than a refused one.

**Authorization is NOT performed here.** The HTTP layer checks FGA first and the actor stays testable
without OpenFGA: an actor that authorizes is an actor that cannot be unit-tested, and a second place for the
rule to drift.

**A watch begins when it is registered, and covers only what happens after.** The fan-out asks who was
watching when a run HAPPENED, never who watches when it is DELIVERED. Every run reaches this service twice,
on the bus and again on the reconciler's tick through lineage's feed, so a watch keyed on delivery and
registered between the two is told of work from before it: measured on the e2e-stack lane 2026-10-17, a
watcher who began just before an approval was given WATCH rows for two writes made seconds earlier
([[XC-094]]). Keyed on the run's time, both deliveries name the same watchers.
"""

import json
from datetime import UTC, datetime
from typing import Any

from dapr.actor import Actor, ActorInterface, actormethod


#: The actor type name, spelled once. The sidecar routes on it and `/dapr/config` advertises it.
WATCH_ACTOR_TYPE = "WatchIndexActor"

#: The single state key: a JSON object from each watching subject to the instant its watch began (ISO 8601, UTC),
#: in the order the watches began, which is a free, stable tiebreaker for anything that renders it.
WATCHING_KEY = "watching"


class WatchIndexActorInterface(ActorInterface):
    """The wire surface — these names are the sidecar's routing ids and are fixed here.

    `@actormethod(name=...)` is load-bearing: `ActorProxy.__getattr__` dispatches from a map keyed by
    the WIRE name, so a call site using the Python name raises `AttributeError` against a real sidecar
    while unit-test fakes stay green. Everything in this service goes through `typed_proxy`, which
    reads this metadata, so call sites keep the Python names.
    """

    @actormethod(name="Watch")
    async def watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="Unwatch")
    async def unwatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @actormethod(name="ListWatchers")
    async def list_watchers(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class WatchIndexActor(Actor, WatchIndexActorInterface):
    """Holds one project's watcher list. Single-activation IS the lock — no etags, no OCC."""

    async def _load(self) -> dict[str, str]:
        has, raw = await self._state_manager.try_get_state(WATCHING_KEY)
        return dict(json.loads(raw)) if has and raw else {}

    async def _save(self, watching: dict[str, str]) -> None:
        await self._state_manager.set_state(WATCHING_KEY, json.dumps(watching))
        await self._state_manager.save_state()

    async def watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add a subject, its watch beginning now. Idempotent: a retried watch reports `added: False` and keeps
        the instant the watch first began, rather than duplicating it or moving it later."""
        subject = str(payload["subject"])
        watching = await self._load()
        if subject in watching:
            return {"added": False, "total": len(watching)}
        watching[subject] = datetime.now(UTC).isoformat()
        await self._save(watching)
        return {"added": True, "total": len(watching)}

    async def unwatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove a subject. Idempotent in the same direction — unwatching twice is not an error."""
        subject = str(payload["subject"])
        watching = await self._load()
        if subject not in watching:
            return {"removed": False, "total": len(watching)}
        del watching[subject]
        await self._save(watching)
        return {"removed": True, "total": len(watching)}

    async def list_watchers(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The subjects whose watch had begun at `payload["at"]`, the instant a run happened (ISO 8601): the
        fan-out's second targeting source, the same whichever lane delivers the run and however late."""
        at = datetime.fromisoformat(str(payload["at"]))
        watching = await self._load()
        subjects = [subject for subject, began in watching.items() if datetime.fromisoformat(began) <= at]
        return {"subjects": subjects, "total": len(subjects)}
