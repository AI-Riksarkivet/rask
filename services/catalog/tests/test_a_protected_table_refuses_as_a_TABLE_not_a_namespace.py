"""A protected TABLE must refuse with a table-shaped error, not a namespace-shaped one.

[[LH-019]], unblocked by the owner's R1-R11 acknowledgement 2026-09-16 (the management-API carve it
waited on is R2). The row settled the factual half already: `require_not_protected` raises
`NamespaceNotEmptyError` for EVERY `kind`, including `table`, and justified that as "reused rather than
minting a status the client SDKs do not map" — which is measured false. `InvalidTableStateError` IS
exported by the installed `lance_namespace` and the SDK maps it.

THE SPEC NAMES THE CODE, so this is not a preference between two mappable options.
`lance_docs/ns_catalog/spec.yaml:2431`: **"19 - InvalidTableState: Table is in an invalid state for the
operation"** — which is exactly what "this table is protected against deletion" is. Code 3 is
`NamespaceNotEmpty`, a CONTAINER error, and a generated client that receives it for a table does the
only sensible thing with it: empties the container and retries, forever, against a table that was never
full.

THE CONTAINER KINDS KEEP CODE 3, and that is a scope decision rather than an oversight. `warehouse` and
`project` are rask's own hierarchy and appear nowhere in the spec, so every code is an approximation
there; `namespace` is at least container-shaped. The table is the one the spec covers and the one
clients are generated against, so it is the one where the wrong code has a reader.
"""

from __future__ import annotations

import pytest
from lance_namespace import InvalidTableStateError, NamespaceNotEmptyError

from catalog.api import fga_deps


_PROTECTED = {"protected": "true"}


def test_a_protected_table_raises_the_spec_s_table_state_error() -> None:
    with pytest.raises(InvalidTableStateError) as refusal:
        fga_deps.require_not_protected(_PROTECTED, kind="table", obj_id="ns$t", force=False)

    assert "force=true" in str(refusal.value), "the refusal does not say how to proceed, so it only moves the search to the caller"


def test_it_is_NOT_the_container_error_a_client_would_retry_against() -> None:
    """The defect, stated as its own assertion: a `NamespaceNotEmptyError` for a table sends a generated
    client into empty-and-retry against something that was never full."""
    with pytest.raises(Exception) as refusal:  # noqa: B017 — the point is which type it is NOT
        fga_deps.require_not_protected(_PROTECTED, kind="table", obj_id="ns$t", force=False)

    assert not isinstance(refusal.value, NamespaceNotEmptyError), "a protected TABLE still refuses with the namespace-not-empty code"


@pytest.mark.parametrize("kind", ["namespace", "warehouse", "project"])
def test_a_protected_container_still_refuses_as_a_container(kind: str) -> None:
    """Scope, pinned. Only the table changes: warehouse and project are rask's own hierarchy and appear
    nowhere in the spec, so 3 is as good an approximation there as anything."""
    with pytest.raises(NamespaceNotEmptyError):
        fga_deps.require_not_protected(_PROTECTED, kind=kind, obj_id="x", force=False)


def test_the_HTTP_STATUS_does_not_move_only_the_code_a_client_dispatches_on() -> None:
    """Both codes map to 409 (`service_kit.lakehouse.ns_errors._STATUS`), so nothing that reads only the
    status sees this change at all — no alert re-tunes, no retry policy shifts, no RED metric moves.
    What moves is the `code` in the problem body, 3 -> 19, which is the field a generated client
    dispatches on and the only field that was ever wrong. Asserted rather than assumed, because a
    status regression here (409 -> 500) would turn a governance refusal into a server fault."""
    from lance_namespace import ErrorCode

    from service_kit.lakehouse.ns_errors import status_for

    assert status_for(ErrorCode.INVALID_TABLE_STATE) == 409
    assert status_for(ErrorCode.INVALID_TABLE_STATE) == status_for(ErrorCode.NAMESPACE_NOT_EMPTY)


@pytest.mark.parametrize("kind", ["table", "namespace", "warehouse", "project"])
def test_force_and_an_unprotected_record_still_pass(kind: str) -> None:
    """The guard must not become a refusal machine: force turns exactly one lock, and an unprotected
    record was never refused."""
    fga_deps.require_not_protected(_PROTECTED, kind=kind, obj_id="x", force=True)
    fga_deps.require_not_protected({}, kind=kind, obj_id="x", force=False)
