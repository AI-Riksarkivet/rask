"""The visibility re-derivation — the invariant the bus's ungoverned-ness forces on this plane.

What these pin is the shape of the question, not OpenFGA's answer to it: ONE round-trip over the
candidate set, `can_get_metadata` on `table:<name>`, the subset test for delivery, and the
three-outcome rule whose middle case (enabled but unwired) must never be permissive.
"""

from typing import TYPE_CHECKING, Any, cast

import pytest

from notifications.api import visibility as visibility_module
from notifications.api.visibility import FGA_OBJECT_TYPE, METADATA_RELATION, Visibility


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
