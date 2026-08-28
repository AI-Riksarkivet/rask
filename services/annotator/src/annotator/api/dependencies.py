"""DI aliases for the annotator service — the lance-ns ``api/dependencies.py`` convention.

Annotated aliases over ``app.state`` come from the shared kernel; service-specific
deps live here beside them.
"""

from typing import Annotated

from fastapi import Depends, Request

from service_kit.control_emit import ControlEmitter, NoopControlEmitter
from service_kit.media.deps import DatasetParam, StateDep


def get_control_emitter(request: Request) -> ControlEmitter:
    """The control-plane change-event emitter built in the app lifespan.

    Falls back to the no-op rather than raising, so a route may always ``await emit_control(...)``
    without a ``getattr`` guard. That fallback is load-bearing for the tests, which build the app
    without a lifespan and would otherwise need to stub this at every task route.
    """
    emitter = getattr(request.app.state, "control_emitter", None)
    return emitter if emitter is not None else NoopControlEmitter()


ControlEmitterDep = Annotated[ControlEmitter, Depends(get_control_emitter)]


__all__ = ["ControlEmitterDep", "DatasetParam", "StateDep", "get_control_emitter"]
