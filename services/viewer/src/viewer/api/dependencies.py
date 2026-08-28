"""DI aliases for the viewer service — the lance-ns ``api/dependencies.py`` convention.

Annotated aliases over ``app.state`` come from the shared kernel; service-specific
deps would live here beside them.
"""

from service_kit.media.deps import DatasetParam, StateDep


__all__ = ["DatasetParam", "StateDep"]
