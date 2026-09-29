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

import pytest

from service_kit.governed import fga


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
