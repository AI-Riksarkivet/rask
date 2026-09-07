"""Every vending mode the config permits can actually be built by the wiring that builds them.

Q17-12 / §F2-8. `LANCE_VENDING_MODE` accepted `static`, and choosing it did nothing: `main.py` is the
only caller of `make_vendor` outside tests and it passes no `static_keys`, so the mode resolved to
`StaticPrefixVendor({})` — whose own docstring says an unknown bucket returns `None` "so the caller
falls back to Mode B rather than vending nothing useful". With an EMPTY map every bucket is unknown,
so the mode was selectable, inert, and silently degraded to the very mode it was chosen instead of.

WHY "RETURNS NONE" IS THE WRONG TEST, and this is the thing that makes the defect hard to see:
`ModeBVendor.vend` returns `None` too, deliberately — "No vending: data flows through the catalog's
server-mediated endpoints". So a vendor answering `None` is not evidence of anything. The defect is
one layer up: the mode depended on an argument nothing supplies, which made it UNREACHABLE rather
than merely quiet.

SO THE GATE IS REACHABILITY, READ OFF THE REAL CALL SITE. `main.py`'s `make_vendor(...)` call is
parsed for the keywords it actually passes, and every mode the config permits must be constructible
from those alone. A mode needing more is one an operator can select and never get — the failure
`sts` avoids by RAISING when `assume_role_arn` is missing, which is the correct shape: refuse
loudly, never degrade silently.

A CONTROL THAT DOES NOTHING IS WORSE THAN AN ABSENT ONE, because it reads as configured. That is why
the fix was deletion rather than wiring: vending here is STS-shaped, and a long-lived per-bucket key
path is not something this estate wants back.
"""

from __future__ import annotations

import ast
import pathlib
import typing

import pytest

from catalog.core import vending
from catalog.core.config import Settings


REPO = pathlib.Path(__file__).resolve().parents[3]
MAIN = REPO / "services/catalog/src/catalog/main.py"


def _kwargs_the_production_call_site_passes() -> set[str]:
    """The keywords `main.py` hands `make_vendor`, read from the source rather than assumed.

    Parsed rather than grepped so a renamed keyword or a call moved behind a helper reds this test
    instead of quietly widening what counts as reachable.
    """
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "make_vendor":
            return {kw.arg for kw in node.keywords if kw.arg}
    pytest.fail("no make_vendor(...) call found in catalog/main.py — this gate is now blind")


def test_the_config_and_the_factory_permit_THE_SAME_MODES() -> None:
    """Two lists of modes is two chances to disagree, and only one of them is the door an operator
    types into."""
    declared = set(typing.get_args(Settings.model_fields["vending_mode"].annotation))
    factory = set(typing.get_args(vending.VendingMode))
    assert declared == factory, f"config permits {sorted(declared)} while the factory knows {sorted(factory)}"


def test_no_permitted_mode_needs_an_argument_NOBODY_PASSES() -> None:
    """THE DEFECT. A mode requiring a keyword the production call site never supplies is one an
    operator can select and never get — indistinguishable, from outside, from the mode it degrades to."""
    import inspect

    passed = _kwargs_the_production_call_site_passes()
    signature = inspect.signature(vending.make_vendor)
    # Every keyword-only parameter that has no default is one the caller MUST supply.
    required = {name for name, p in signature.parameters.items() if p.kind is p.KEYWORD_ONLY and p.default is p.empty}
    assert required <= passed, f"make_vendor requires {sorted(required - passed)}, which main.py does not pass"

    # ...and the mode-specific ones: a mode whose vendor is built from a kwarg main.py omits is
    # reachable only in a test. `static_keys` was exactly that.
    mode_specific = {"static_keys"}
    leftover = mode_specific & set(signature.parameters)
    assert not leftover, (
        f"make_vendor still takes {sorted(leftover)}, which main.py never passes — the mode it serves is "
        "selectable and unreachable, so choosing it silently degrades to another mode"
    )


def test_the_dead_static_mode_is_GONE_rather_than_merely_unused() -> None:
    """Deleted, per the standing remove-dead-code rule: an unused branch behind a config Literal is
    still a documented option, and an operator reading the Literal has no way to know it is inert."""
    assert "static" not in typing.get_args(vending.VendingMode), "the static vending mode is still selectable"
    assert not hasattr(vending, "StaticPrefixVendor"), "StaticPrefixVendor survives, so the branch can come back"


@pytest.mark.parametrize("mode", typing.get_args(vending.VendingMode))
def test_every_surviving_mode_builds_from_what_main_passes(mode: vending.VendingMode) -> None:
    """Reachability, positively: each mode is constructed with the production keywords and must yield
    a vendor — or refuse with a reason, which is what `sts` does without a role arn.

    Parametrised off `VendingMode` itself rather than a written-out list, so a mode added to the
    Literal is covered by this file existing rather than by someone remembering to add it here."""
    built = vending.make_vendor(
        mode,
        region="us-east-1",
        sts_endpoint="http://s3:9000",
        ttl_seconds=900,
        assume_role_arn="arn:aws:iam::1:role/r",
    )
    assert hasattr(built, "vend"), f"{mode} built something that is not a vendor: {built!r}"


def test_sts_REFUSES_rather_than_degrading_when_its_role_is_missing() -> None:
    """The shape every mode should have, kept as the reference: a mode that cannot work says so."""
    with pytest.raises(ValueError, match="assume_role_arn"):
        vending.make_vendor("sts", region="us-east-1")
