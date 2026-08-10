"""The visibility re-derivation — the invariant the bus's ungoverned-ness forces on this plane.

What these pin is the shape of the question, not OpenFGA's answer to it: ONE round-trip over the
candidate set, `can_get_metadata` on `table:<name>`, the subset test for delivery, and the
three-outcome rule whose middle case (enabled but unwired) must never be permissive.
"""

from typing import TYPE_CHECKING, Any, cast

import pytest

from notifications.api import visibility as visibility_module
from notifications.api.visibility import FGA_OBJECT_TYPE, METADATA_RELATION, Visibility
from service_kit.exceptions import ServiceUnavailableError


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


#: A wired client, standing in for the one the lifespan builds. Its identity is all `Visibility` uses
#: — the round-trip itself is the recorder below — so the cast is the whole of the double.
WIRED = cast("OpenFgaClient", object())


class _Recorder:
    """Stands in for `service_kit.governed.fga.batch_check` and records every call it is asked to make."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        self.calls.append({"user": user, "relation": relation, "objects": list(objects)})
        return {obj: obj in self.allowed for obj in objects}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder(allowed=set())
    monkeypatch.setattr(visibility_module.fga, "batch_check", rec)
    return rec


@pytest.mark.asyncio
async def test_with_fga_off_everything_is_visible() -> None:
    """A dev stack behaves exactly as it did before authorization existed — one anonymous inbox,
    nothing filtered."""
    view = Visibility(client=None, enabled=False)
    assert await view.visible("alice", ["silver$pages"]) == {"silver$pages"}
    assert await view.sees_all("alice", ["silver$pages", "gold$lines"])


@pytest.mark.asyncio
async def test_enabled_but_unwired_refuses_rather_than_allows() -> None:
    """The middle case of the three-outcome rule. Permissive here turns a BROKEN authorization layer
    into an open one, silently — the failure nobody notices until it matters."""
    view = Visibility(client=None, enabled=True)
    with pytest.raises(ServiceUnavailableError):
        await view.visible("alice", ["silver$pages"])


@pytest.mark.asyncio
async def test_an_empty_candidate_set_is_answered_rather_than_refused() -> None:
    """It short-circuits BEFORE the unwired refusal, matching the estate's existing filter. Nothing
    can be disclosed by answering an empty question, while 503ing a page that had no rows to filter
    would turn "your inbox is empty" into an error."""
    view = Visibility(client=None, enabled=True)
    assert await view.visible("alice", []) == set()


@pytest.mark.asyncio
async def test_one_round_trip_over_the_whole_candidate_set(recorder: _Recorder) -> None:
    """`batch_check`, never N `check`s — the shape the governed lineage read already uses."""
    recorder.allowed = {f"{FGA_OBJECT_TYPE}:silver$pages"}
    view = Visibility(client=WIRED, enabled=True)

    assert await view.visible("alice", ["silver$pages", "gold$lines"]) == {"silver$pages"}

    assert len(recorder.calls) == 1
    assert recorder.calls[0] == {
        "user": "alice",
        "relation": METADATA_RELATION,
        "objects": [f"{FGA_OBJECT_TYPE}:silver$pages", f"{FGA_OBJECT_TYPE}:gold$lines"],
    }


@pytest.mark.asyncio
async def test_the_objects_are_checked_as_tables(recorder: _Recorder) -> None:
    """`table:` and `can_get_metadata` are settled, not chosen here: the lineage governed path builds
    `table:` objects (its `fga_object_type` default is overridden by nothing in the chart), and the
    concurrent upward-visibility change touches the two CONTAINER types only — `table#can_get_metadata`
    is still bare `reader`. So this plane asks the one question the widening does not reach."""
    view = Visibility(client=WIRED, enabled=True)
    await view.visible("alice", ["silver$pages"])
    assert recorder.calls[0]["objects"] == ["table:silver$pages"]
    assert recorder.calls[0]["relation"] == "can_get_metadata"


@pytest.mark.asyncio
async def test_one_invisible_output_drops_the_row(recorder: _Recorder) -> None:
    """`names <= visible` is a SUBSET test, and it is the half people get wrong. A run that wrote a
    dataset you may read and one you may not is a run you are not told about — being told names the
    run, its author and its outcome."""
    recorder.allowed = {"table:silver$pages"}
    view = Visibility(client=WIRED, enabled=True)

    assert await view.sees_all("alice", ["silver$pages"])
    assert not await view.sees_all("alice", ["silver$pages", "gold$lines"])


@pytest.mark.asyncio
async def test_the_subset_test_costs_one_round_trip_too(recorder: _Recorder) -> None:
    recorder.allowed = {"table:silver$pages", "table:gold$lines"}
    view = Visibility(client=WIRED, enabled=True)

    assert await view.sees_all("alice", ["silver$pages", "gold$lines"])
    assert len(recorder.calls) == 1
