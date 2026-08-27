"""A truncated authorization listing must not read as a complete one.

open_fastapi-audit — "Five governed collection listings filter against a single `fga.list_objects`
call the OpenFGA server silently truncates — and mint their page cursor from the truncated list".

`list_users` already handles this: it compares its result length against `LIST_USERS_SERVER_CAP` and
logs `openfga_list_users_possibly_truncated`, because ListUsers has no pagination and a result at the
cap is more likely the server's ceiling than the true count. `list_objects` had no such check — the
same silent ceiling, on the call five governed collection listings intersect their rows against.

WHAT IT COSTS, and the finding is careful to bound it: an entitled caller sees FEWER of their own rows
than they hold, and the cursor is minted from the shortened list, so paging forward cannot recover
them either. It hides data rather than exposing it — the fail-closed direction — and only past
OpenFGA's 1000-object cap, which nothing in this estate is near today. That is why it is medium: a
scale-gated silent undercount, not an open door.

WHY THE SYMMETRY IS THE FIX RATHER THAN THE BATCH_CHECK REWRITE. The finding offers both. The
`batch_check`-over-the-page rewrite is the better long-run shape — authorization evaluated on the
O(page_size) rows actually being served rather than an unbounded pre-fetch — but it changes the
semantics of five listing routes, and the failure it would fix is the same one the cheap symmetric
guard makes VISIBLE. Making a silent truncation loud is what turns "nobody is near the cap" from an
assumption into something the estate would tell you about; the rewrite can then be done on evidence
rather than on a ceiling nobody has hit. The guard is also exactly what the sibling call already does,
so it is the version this codebase can be consistent about.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from service_kit.governed import fga


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_the_cap_is_declared_for_objects_as_well_as_users() -> None:
    assert hasattr(fga, "LIST_OBJECTS_SERVER_CAP"), (
        "list_objects has no declared server cap, so nothing can compare a result against it — the sibling list_users has carried one since it was written"
    )
    assert fga.LIST_OBJECTS_SERVER_CAP > 0


class _Response:
    def __init__(self, objects: list[str]) -> None:
        self.objects = objects


class _Client:
    def __init__(self, objects: list[str]) -> None:
        self._objects = objects

    async def list_objects(self, _request: object) -> _Response:
        return _Response(self._objects)


@pytest.mark.asyncio
async def test_a_result_at_the_cap_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """At the ceiling, the answer is probably not the whole answer — say so."""
    from typing import cast

    from openfga_sdk.client import OpenFgaClient

    objects = [f"table:t{i}" for i in range(fga.LIST_OBJECTS_SERVER_CAP)]
    with caplog.at_level(logging.WARNING):
        result = await fga.list_objects(cast("OpenFgaClient", _Client(objects)), user="gina", relation="can_get_metadata", object_type="table")

    assert len(result.objects) == fga.LIST_OBJECTS_SERVER_CAP
    assert "openfga_list_objects_possibly_truncated" in caplog.text, (
        "a listing that came back exactly at the server cap was returned as though complete — the "
        "caller sees fewer of their own rows than they hold, and the page cursor is minted from the "
        "shortened list so paging forward cannot recover them"
    )


@pytest.mark.asyncio
async def test_an_ordinary_result_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    """A warning on every listing is a warning nobody reads."""
    from typing import cast

    from openfga_sdk.client import OpenFgaClient

    with caplog.at_level(logging.WARNING):
        await fga.list_objects(cast("OpenFgaClient", _Client(["table:a", "table:b"])), user="gina", relation="can_get_metadata", object_type="table")
    assert "possibly_truncated" not in caplog.text


def test_both_listing_calls_guard_their_cap() -> None:
    """The symmetry is the point: two calls with the same silent ceiling, one guarded and one not, is
    how the unguarded one stayed invisible."""
    import inspect

    for name, cap in (("list_users", "LIST_USERS_SERVER_CAP"), ("list_objects", "LIST_OBJECTS_SERVER_CAP")):
        source = inspect.getsource(getattr(fga, name))
        assert cap in source, f"{name} does not compare its result against {cap}"
        assert "possibly_truncated" in source, f"{name} never reports a truncated result"


def test_list_objects_returns_the_flag_not_just_a_list() -> None:
    """A log tells an OPERATOR. `pagination.md` requires the CLIENT be able to learn its answer was
    cut — "clients don't compute them" — and a caller holding a short list cannot tell a small estate
    from a truncated one. So the truncation travels with the result rather than only to the log."""
    import inspect

    assert hasattr(fga, "ObjectListing"), "list_objects returns a bare list, so no caller can propagate truncation"
    assert set(fga.ObjectListing._fields) == {"objects", "truncated"}
    assert "ObjectListing" in inspect.signature(fga.list_objects).return_annotation


@pytest.mark.asyncio
async def test_the_flag_is_true_only_at_the_cap() -> None:
    from typing import cast

    from openfga_sdk.client import OpenFgaClient

    short = await fga.list_objects(cast("OpenFgaClient", _Client(["table:a"])), user="g", relation="r", object_type="table")
    assert short.objects == ["table:a"]
    assert short.truncated is False

    capped = await fga.list_objects(
        cast("OpenFgaClient", _Client([f"table:t{i}" for i in range(fga.LIST_OBJECTS_SERVER_CAP)])),
        user="g",
        relation="r",
        object_type="table",
    )
    assert capped.truncated is True


def test_every_governed_listing_propagates_the_flag() -> None:
    """All five, because a flag one route surfaces and four swallow is worse than none: it teaches a
    client to trust an absence that means nothing on the other four."""
    import pathlib

    sites = {
        "services/catalog/src/catalog/api/v1/endpoints/tables.py",
        "services/catalog/src/catalog/api/v1/endpoints/namespaces.py",
        "services/catalog/src/catalog/api/v1/endpoints/models.py",
        "services/catalog/src/catalog/api/v1/endpoints/warehouses.py",
    }
    missing = [site for site in sorted(sites) if "authorization_truncated" not in (pathlib.Path(REPO_ROOT) / site).read_text()]
    assert not missing, f"these governed listings drop the truncation flag on the floor: {missing}"
