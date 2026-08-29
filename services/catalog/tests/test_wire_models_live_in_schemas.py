"""catalog-api-05 — every wire model lives in ``catalog.schemas``; the endpoints stay routing-only.

``schemas.py``'s own header declares itself the single home "instead of scattered inline across the
endpoint modules" and cites a bug for the drift; fourteen request/response models had scattered anyway
(me, members, events, warehouses, user_state, projects). A runtime walk, not grep: any BaseModel
subclass DEFINED in an endpoints module (``__module__`` says where a class was born, aliases cannot
hide it) is an offence. Class names are the OpenAPI schema names, so a move preserves the contract.
"""

from __future__ import annotations

import importlib
import pkgutil

import catalog.api.v1.endpoints as endpoints_pkg
from pydantic import BaseModel


def _endpoint_modules() -> list:
    return [importlib.import_module(f"{endpoints_pkg.__name__}.{info.name}") for info in pkgutil.iter_modules(endpoints_pkg.__path__)]


def test_the_walk_imports_the_endpoint_plane() -> None:
    modules = _endpoint_modules()
    assert len(modules) > 10, f"only {len(modules)} endpoint modules imported — the walk is not seeing the plane"


def test_no_wire_model_is_defined_in_an_endpoint_module() -> None:
    offences = [
        f"{mod.__name__.rsplit('.', 1)[-1]}.py defines {name}"
        for mod in _endpoint_modules()
        for name, obj in vars(mod).items()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == mod.__name__
    ]
    assert not offences, (
        "wire models defined inline in endpoint modules — move them to catalog.schemas (the declared "
        "single home; class names are the OpenAPI schema names, so the contract survives the move):\n  " + "\n  ".join(sorted(offences))
    )
