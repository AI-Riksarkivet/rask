"""A singular control-record read REFUSES a malformed record; it does not answer "absent" (§ Q3-33).

THE ASYMMETRY THIS CLOSES, measured 2026-09-09. `validated()` was called at exactly three sites and all
three were LIST paths (`list_warehouses`, `read_bindings`, `list_projects`). The five SINGULAR readers
validated nothing — and those are the ones the destructive doors read through: warehouse delete, project
delete, the unbind door, and the routing resolver.

THE TOLERANT FORM CANNOT SERVE A SINGULAR READ, and that is the whole reason for a second helper rather
than a stricter one. `validated()` answers `None`, which is right for a listing: one tenant's corruption
must not void an estate-wide result, and destructive callers get fail-closed behaviour from being told
WHICH paths were skipped. A singular reader has no such channel — its only return value is the record,
so `None` does not mean "unreadable" there, it means ABSENT.

AND THE CONSEQUENCE IS NOT SYMMETRIC. For `get_warehouse`, absent is a 404 and fail-closed. For
`warehouse_for_namespace` it is "unbound" — and an unbound namespace routes at the DEFAULT root, so a
binding nobody can parse would send a tenant's writes to the wrong bucket while the registry still named
the right one. The module already stated the rule this restores: `read_bindings`' own docstring says
"the DELETE door must fail closed on one. A binding it cannot read is a namespace it cannot see."
"""

from __future__ import annotations

import inspect

import pytest


@pytest.mark.parametrize(
    ("module", "reader"),
    [
        ("catalog.services.warehouses", "get_warehouse"),
    ],
)
def test_every_singular_reader_validates_what_it_returns(module: str, reader: str) -> None:
    """The reader must pass the control record it reads through the strict validator."""
    import importlib

    source = inspect.getsource(getattr(importlib.import_module(module), reader))
    assert "read_json(" in source, f"{reader} no longer reads a control record — re-point this gate"
    assert "validated_or_refuse(" in source, (
        f"{reader} returns a control record it never validated: a malformed record reaches its caller as "
        "ABSENT, which for the routing resolver means the default root and a write in the wrong bucket"
    )
