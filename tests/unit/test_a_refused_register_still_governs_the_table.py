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
both directions and on a path BOUNDARY rather than by `endswith`.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from catalog.api.v1.endpoints.tables import _registration_points_at


class TestTheLocationGuard:
    """The claim is relative and the registration absolute, so the match is a tail on a boundary."""

    @pytest.mark.parametrize(
        "registered,claimed",
        [
            ("s3://bucket/8f3a21bc_silver$features", "8f3a21bc_silver$features"),
            ("s3://bucket/8f3a21bc_silver$features/", "8f3a21bc_silver$features"),
            ("s3://bucket/medallion/lakehouse$silver", "medallion/lakehouse$silver"),
            ("8f3a21bc_silver$features", "8f3a21bc_silver$features"),
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
            ("s3://bucket/prefix8f3a21bc_silver$features", "8f3a21bc_silver$features"),
            # A genuine id conflict: same id, different home.
            ("s3://bucket-a/8f3a21bc_silver$features", "different_silver$features"),
            ("s3://bucket/8f3a21bc_silver$features", ""),
        ],
    )
    def test_a_DIFFERENT_location_is_a_real_conflict_and_does_not_converge(self, registered: str, claimed: str) -> None:
        assert _registration_points_at(registered, claimed) is False


def test_the_guard_is_reachable_from_the_register_door() -> None:
    """The helper is worthless if the door stopped calling it — the dead-code-that-looks-live shape.

    An earlier draft of this convergence resolved BOTH sides through `absolute_table_location`, which
    prefers the described location over its argument: both sides came back identical, so the guard
    admitted every 409 regardless of where it pointed, while reading as a careful check.

    `linecache.checkcache()` first, and that is not ceremony: `inspect.getsource` serves a CACHED copy
    of an already-imported module, so without it this assertion passes against a file that no longer
    contains what it claims — which is exactly how the companion assertion that used to live here
    survived a mutation that deleted the thing it was pinning.
    """
    import inspect
    import linecache

    from catalog.api.v1.endpoints import tables

    linecache.checkcache(tables.__file__)
    body = inspect.getsource(tables.register_table)
    assert "_registration_points_at" in body, "the register door no longer consults the location guard"


@pytest.mark.asyncio
async def test_the_convergence_writes_the_EDGE_and_never_an_owner_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rung the convergence may write, asserted on the TUPLES rather than on a call argument.

    Re-registering somebody else's table must not make the caller its owner — the same
    ownership-seizure refusal that guards the ExistOk arm of `create`, audited CRITICAL there. The
    structural `parent` edge is safe to write because it names where the table lives, it is
    idempotent, and it confers nothing by itself.
    """
    from typing import TYPE_CHECKING, cast

    from catalog.api import fga_deps
    from catalog.core.config import Settings
    from service_kit.governed import fga
    from service_kit.governed.oidc import IDToken

    if TYPE_CHECKING:
        from openfga_sdk.client import OpenFgaClient

    settings = Settings.model_validate(
        {
            "LANCE_S3_ACCESS_KEY_ID": "x",
            "LANCE_S3_SECRET_ACCESS_KEY": "y",
            "RASK_FGA_ENABLED": True,
            "RASK_OIDC_ENABLED": True,
            "RASK_OIDC_ISSUER": "https://dex",
            "RASK_OIDC_AUDIENCE": "rask",
        }
    )
    token = IDToken.model_validate({"sub": "mallory", "iss": "https://dex", "aud": "rask", "iat": 0, "exp": 1})
    written: list[tuple[str, str, str]] = []

    async def _capture(_client: object, tuples: Iterable[fga.ClientTuple], **_kw: object) -> None:
        written.extend((t.user, t.relation, t.object) for t in tuples)

    monkeypatch.setattr(fga, "write_tuples", _capture)

    await fga_deps.seed_ownership(
        cast("OpenFgaClient", object()),  # the capture never touches the client
        settings,
        token,
        resource="table",
        segments=["db1", "users"],
        may_grant_owner=False,
    )

    assert [t for t in written if t[1] == "owner"] == [], f"re-registering seized ownership: {written}"
    assert ("namespace:db1", "parent", "table:db1$users") in written, f"the structural edge was not written: {written}"
