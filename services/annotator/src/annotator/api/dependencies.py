"""DI aliases for the annotator service — the lance-ns ``api/dependencies.py`` convention.

Annotated aliases over ``app.state`` come from the shared kernel; service-specific
deps would live here beside them.
"""

from common.deps import AuthorDep, DatasetParam, StateDep


__all__ = ["AuthorDep", "DatasetParam", "StateDep"]
