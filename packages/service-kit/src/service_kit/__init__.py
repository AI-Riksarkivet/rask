"""The rask platform library: the service app factory, config, DI, middleware, the auth plane and the naming grammar.

It is what a client of the lakehouse may pin. The lakehouse's own kernel is ``lakehouse_kit``, which imports this
package and which this package never imports.

NOTHING IS IMPORTED EAGERLY HERE, and that is the point. The app factory imports FastAPI, python-dotenv, the OTel
wiring, the middleware stack and the probe router, so a package `__init__` holding it would make
`from service_kit.config import Settings`, or any import of `service_kit.exceptions` or `service_kit.governed.*`, pay
for all of it first: a writer's job that wanted one helper would import a web framework.

The factory lives in :mod:`service_kit.app`. The names below are resolved on FIRST ACCESS through
PEP 562's module `__getattr__`, so `from service_kit import make_service_app` and
`from service_kit import setup_logging` resolve to the factory's names while `import service_kit.config`
costs a config module.

Submodules are unaffected — `from service_kit import naming` is an import-system lookup, not an
attribute one, and never goes through this file.
"""

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    # For type checkers and IDEs only: at runtime these arrive through `__getattr__` below. Under
    # TYPE_CHECKING there is no import cost to avoid, so the real names are declared here rather than
    # left implicit.
    # Redundant aliases: the re-export form static tooling understands. `__all__` below is the
    # runtime statement of the same surface.
    from service_kit.app import LifespanFactory as LifespanFactory
    from service_kit.app import build_settings as build_settings
    from service_kit.app import default_lifespan as default_lifespan
    from service_kit.app import make_service_app as make_service_app
    from service_kit.app import setup_logging as setup_logging
    from service_kit.otel import setup_otel as setup_otel


#: name -> the module that defines it. The package's PUBLIC surface, stated in one place.
_LAZY = {
    "LifespanFactory": "service_kit.app",
    "build_settings": "service_kit.app",
    "default_lifespan": "service_kit.app",
    "make_service_app": "service_kit.app",
    "setup_logging": "service_kit.app",
    "setup_otel": "service_kit.otel",
}

#: Spelled out rather than derived from `_LAZY`, so a reader and a static analyser see the surface
#: without executing anything. `test_package_surface_is_declared_and_lazy` pins the two in step.
__all__ = [
    "LifespanFactory",
    "build_settings",
    "default_lifespan",
    "make_service_app",
    "setup_logging",
    "setup_otel",
]


def __getattr__(name: str) -> object:
    """Resolve a re-exported name on first access (PEP 562)."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module), name)


def __dir__() -> list[str]:
    """`dir(service_kit)` must list the lazy names too — otherwise tab-completion, `inspect` and
    `from service_kit import *` all report a package that looks empty."""
    return sorted({*globals(), *_LAZY})
