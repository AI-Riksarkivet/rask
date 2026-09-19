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
    BLOB_RESOLVES,
    COLUMN_DECLARED,
    ERROR,
    NOT_NULL,
    ROW_COUNT_POSITIVE,
    STRUCTURAL_ASSERTIONS,
    WARN,
    Assertion,
)


@pytest.mark.parametrize("name", sorted(STRUCTURAL_ASSERTIONS))
def test_a_structural_finding_is_an_error(name: str) -> None:
    """Derived from the set itself, not a hand-listed copy — a new structural assertion becomes
    `error` on the wire by being added to `STRUCTURAL_ASSERTIONS` and nowhere else."""
    assert Assertion(assertion=name, success=False).severity == ERROR


@pytest.mark.parametrize("name", [ROW_COUNT_POSITIVE, COLUMN_DECLARED])
def test_a_waivable_finding_is_a_warning(name: str) -> None:
    """These CAN be approved through, so calling them `error` would tell a consumer the pipeline
    stopped when a reviewer is entitled to let them past."""
    assert Assertion(assertion=name, success=False).severity == WARN


def test_a_structural_finding_CANNOT_be_downgraded() -> None:
    """The hole worth closing: an `error` arriving as `warn` tells a consumer the pipeline continued
    past a broken join, which is the one thing this label exists to make visible."""
    assert Assertion(assertion=NOT_NULL, success=False, severity=WARN).severity == ERROR
    assert Assertion(assertion=BLOB_RESOLVES, success=True, severity="anything").severity == ERROR


def test_a_passing_assertion_still_carries_its_severity() -> None:
    """Severity is a property of the CHECK, not of its outcome — a consumer grouping by severity must
    see the same label whether the assertion passed or failed."""
    assert Assertion(assertion=NOT_NULL, success=True).severity == ERROR


def test_the_two_values_are_the_spec_s_own() -> None:
    """`error` / `warn` are the vocabulary a standard consumer reads; any other spelling is a field it
    will not understand."""
    assert (ERROR, WARN) == ("error", "warn")
