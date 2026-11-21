"""A 409 from `register_table` converges the table's structural edge instead of leaving it orphaned.

[[LH-164]]. A table whose seed never ran carries NO FGA tuples at all. Every `table` relation in
`model.fga` resolves through a direct tuple or `X from parent`, so such a table denies EVERY principal
— including the identity that registered it — and no door can repair it, because re-registering is
precisely what lands on the already-exists path. Measured 2026-09-15 on the live estate: five of the
cascade's own tiers in that state, with `can_maintain`, `can_write_data` and `can_get_metadata` all
False for maintenance AND for the cascade identities that write them.

THE 409 IS UNCHANGED, AND THAT IS DELIBERATE. The Lance Namespace spec gives this door two modes —
`Create` (fails 409) and `Overwrite` — and no ExistOk, so answering 200 to a duplicate registration
would be a wire-contract change no generated client expects. What was wrong was never the status; it
was leaving the id ungoverned while refusing it. The refusal stands and the structural fact converges.

ONLY WHEN THE LOCATION MATCHES. A 409 at a DIFFERENT location is a genuine id conflict — another
table living there — and writing an edge for it would attach somebody else's object to this caller's
namespace. That check is the security boundary of this whole behaviour, which is why it is tested in
both directions.
"""

from __future__ import annotations

import pytest

from catalog.api.v1.endpoints.tables import _registration_points_at


class TestTheLocationGuard:
    """The claim is relative and the registration absolute, so the match is a tail on a boundary."""

    @pytest.mark.parametrize(
        "registered,claimed",
        [
            ("s3://bucket/8f3a21bc_silver$features", "8f3a21bc_silver$features"),
        ],
    )
    def test_a_matching_registration_converges(self, registered: str, claimed: str) -> None:
        assert _registration_points_at(registered, claimed) is True

    @pytest.mark.parametrize(
        "registered,claimed",
        [
            # THE ATTACK the boundary check exists for: a bare `endswith` accepts this, and it is a
            # DIFFERENT table — converging it would attach another tenant's object to this namespace.
            ("s3://bucket/other_silver$features", "silver$features"),
        ],
    )
    def test_a_DIFFERENT_location_is_a_real_conflict_and_does_not_converge(self, registered: str, claimed: str) -> None:
        assert _registration_points_at(registered, claimed) is False
