"""What one tick of a catalog timer works with (`catalog.api.timers` runs each on its period).

A tick is the catalog acting on its own, with no caller: it converges records the control root holds (a due erasure
release, a hold past its deadline, a staged event, a version lineage's feed lacks, a namespace index's history, the
declaration git renders). Its work reaches the catalog's state the way a door's does, through the helpers a request
resolves its namespaces, uuids and emitters with, so a :class:`Tick` carries a request scope of its own: the app, and a
cache that lives for this tick alone, as a request's does for the request.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI, Request

from catalog.api.dependencies import ObjectUuids, get_control_emitter
from catalog.core.config import Settings
from lakehouse.events.control_emit import ControlEmitter
from lakehouse.storage_options import StorageOptions


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Tick:
    """One tick's collaborators: the app's state, the settings, the clock a deadline is judged by, the control lane.

    Args:
        app: The catalog app whose lifespan built what the tick works with.
        settings: The catalog's settings.
        clock: The wall clock a due time is judged by.
        control: The control emitter the tick announces on; the app's own, naming each object by its uuids, unless given.
    """

    def __init__(self, app: FastAPI, settings: Settings, *, clock: Callable[[], datetime] = _utc_now, control: ControlEmitter | None = None) -> None:
        self.app = app
        self.settings = settings
        self.clock = clock
        self.request = Request({"type": "http", "app": app, "method": "POST", "path": "/", "headers": [], "query_string": b"", "path_params": {}, "state": {}})
        self.control = control if control is not None else get_control_emitter(self.request, ObjectUuids(self.request, settings))

    @property
    def storage_options(self) -> StorageOptions:
        """The store options a tick opens a table with, under the data plane's bounds as a door's are."""
        return self.settings.storage_options(plane="data")
