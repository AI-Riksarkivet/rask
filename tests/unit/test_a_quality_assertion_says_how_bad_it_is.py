"""A failed assertion carries its SEVERITY, so a consumer can tell blocking from advisory ([[LIN-002]]).

The spec's `DataQualityAssertionsDatasetFacet` gives each assertion a `severity` — `error` when the
failure blocks the pipeline, `warn` when it does not. rask emitted neither, so a standard consumer read
a list of `success: false` with no way to tell a broken join from an unusual-but-accepted row count:
the two outcomes this estate treats most differently.

THE VALUE IS NOT INVENTED FOR THE WIRE. `STRUCTURAL_ASSERTIONS` already draws exactly that line —
"findings NO approval can wave through" — and is already enforced twice, at the medallion's review and
the catalog's publish door. Deriving `severity` from it is what stops the wire and the gate
disagreeing about which failures are blocking.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.quality import (
    ERROR,
    WARN,
    Assertion,
)


@pytest.mark.parametrize("name", ["blob_resolves"])
def test_a_structural_finding_is_an_error(name: str) -> None:
    """A failed structural assertion reads as `error` on the wire, the severity `STRUCTURAL_ASSERTIONS` gives it."""
    assert Assertion(assertion=name, success=False).severity == ERROR


def test_the_two_values_are_the_spec_s_own() -> None:
    """`error` / `warn` are the vocabulary a standard consumer reads; any other spelling is a field it
    will not understand."""
    assert (ERROR, WARN) == ("error", "warn")
