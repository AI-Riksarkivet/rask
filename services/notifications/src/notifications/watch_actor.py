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
without OpenFGA — the same doctrine the annotator's actors carry and the same reason: an actor that
authorizes is an actor that cannot be unit-tested, and a second place for the rule to drift.
"""

import json
from typing import Any

from dapr.actor import Actor, ActorInterface, actormethod


#: The actor type name, spelled once. The sidecar routes on it and `/dapr/config` advertises it.
WATCH_ACTOR_TYPE = "WatchIndexActor"

#: The single state key: a JSON list of subjects, insertion-ordered. A LIST rather than a set because
#: JSON has no set, and insertion order is a free, stable tiebreaker for anything that renders it.
WATCHERS_KEY = "watchers"


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
    async def list_watchers(self) -> dict[str, Any]:
        raise NotImplementedError


class WatchIndexActor(Actor, WatchIndexActorInterface):
    """Holds one project's watcher list. Single-activation IS the lock — no etags, no OCC."""

    async def _load(self) -> list[str]:
        has, raw = await self._state_manager.try_get_state(WATCHERS_KEY)
        return list(json.loads(raw)) if has and raw else []

    async def watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add a subject. Idempotent: a retried watch reports `added: False` rather than duplicating."""
        subject = str(payload["subject"])
        watchers = await self._load()
        if subject in watchers:
            return {"added": False, "total": len(watchers)}
        watchers.append(subject)
        await self._state_manager.set_state(WATCHERS_KEY, json.dumps(watchers))
        await self._state_manager.save_state()
        return {"added": True, "total": len(watchers)}

    async def unwatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove a subject. Idempotent in the same direction — unwatching twice is not an error."""
        subject = str(payload["subject"])
        watchers = await self._load()
        if subject not in watchers:
            return {"removed": False, "total": len(watchers)}
        watchers = [s for s in watchers if s != subject]
        await self._state_manager.set_state(WATCHERS_KEY, json.dumps(watchers))
        await self._state_manager.save_state()
        return {"removed": True, "total": len(watchers)}

    async def list_watchers(self) -> dict[str, Any]:
        """Every subject watching this project. The fan-out's second targeting source."""
        watchers = await self._load()
        return {"subjects": watchers, "total": len(watchers)}
