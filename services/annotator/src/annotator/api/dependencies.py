"""DI aliases for the annotator service — the lance-ns ``api/dependencies.py`` convention.

Annotated aliases over ``app.state`` come from the shared kernel.
"""

from client_kit.media.deps import DatasetParam, StateDep


__all__ = ["DatasetParam", "StateDep"]
