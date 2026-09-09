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
from lance_namespace import ServiceUnavailableError

from catalog.services.control_records import BindingRecord, validated, validated_or_refuse


def test_the_tolerant_validator_still_SKIPS_because_listings_depend_on_it() -> None:
    """Not made strict in place: a listing that raises on one bad object turns a single tenant's
    corruption into an estate-wide control-plane outage, which is what its docstring promises against."""
    assert validated({"nonsense": 1}, BindingRecord, event="probe", path="p") is None


def test_the_strict_validator_REFUSES_rather_than_answering_absent() -> None:
    """The sibling a singular reader needs. `ServiceUnavailableError` and not a new type, because it is
    what the delete path already raises for an unreadable binding — one condition, one answer, whichever
    door reached it."""
    with pytest.raises(ServiceUnavailableError, match="could not be read"):
        validated_or_refuse({"nonsense": 1}, BindingRecord, event="probe", path="_bindings/x.json")


def test_the_strict_validator_returns_the_VALIDATED_dump_on_a_good_record() -> None:
    """A consumer relies on the declared fields being the declared types — returning the input would
    make the validation decorative."""
    got = validated_or_refuse(
        {"top_ns": "gold", "warehouse_id": "wh1", "root_uri": "s3://b/gold"},
        BindingRecord,
        event="probe",
        path="p",
    )
    assert got["top_ns"] == "gold" and got["warehouse_id"] == "wh1"


@pytest.mark.parametrize(
    ("module", "reader"),
    [
        ("catalog.services.warehouses", "get_warehouse"),
        ("catalog.services.warehouses", "warehouse_for_namespace"),
        ("catalog.services.warehouses", "binding_for_namespace"),
        ("catalog.services.projects", "get_project"),
    ],
)
def test_every_singular_reader_validates_what_it_returns(module: str, reader: str) -> None:
    """The gate, and it is per-reader on purpose: this failed as a CLASS, so a single spot-check would
    pass while three of the four stayed silent about a record they could not parse."""
    import importlib

    source = inspect.getsource(getattr(importlib.import_module(module), reader))
    assert "read_json(" in source, f"{reader} no longer reads a control record — re-point this gate"
    assert "validated_or_refuse(" in source, (
        f"{reader} returns a control record it never validated: a malformed record reaches its caller as "
        "ABSENT, which for the routing resolver means the default root and a write in the wrong bucket"
    )
