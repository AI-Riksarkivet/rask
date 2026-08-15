"""The FGA object a pointer resolves to — and the two conventions that reach it.

THE PLANE HAS TWO POINTER SOURCES AND THEY SPELL `object_id` DIFFERENTLY.

* A LINEAGE run stamps the bare dataset name: `notifiable()` takes `outputs[0]`, e.g. `silver$pages`.
* A GOVERNANCE event stamps the CANONICAL, ALREADY-QUALIFIED id: `CatalogControlEvent.object_id` is
  documented as "e.g. `warehouse:acme`, `table:db1$t`, `namespace:db1`", and `as_delivery` copies it
  through verbatim.

`Visibility._filter` was written when only the first source existed — its own comment says so ("the
plane only ever notified on `table:` outputs") — and prefixes `table:` unconditionally. So every v3
governance row was checked as `table:table:db1$t`, an object that cannot resolve, and was filtered out
of the feed forever.

The failure is worse than an invisible row, because the BADGE does not go through this filter:
`GET /inbox/unread` is answered from the actor's own partition. A grant notification therefore
produced a badge that counted a row the list would never show — an unclearable badge, which is the
exact failure ("counting work you cannot see") this plane exists to end.
"""

from typing import Any, cast

import pytest

from notifications.api import visibility as visibility_module
from notifications.api.visibility import Visibility


WIRED = cast("Any", object())


class _Recorder:
    """Stands in for `fga.batch_check`, recording the objects it was actually asked about."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.objects: list[str] = []

    async def __call__(self, client: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        self.objects = list(objects)
        return {obj: obj in self.allowed for obj in objects}


def _view(monkeypatch: pytest.MonkeyPatch, allowed: set[str]) -> tuple[Visibility, _Recorder]:
    recorder = _Recorder(allowed)
    monkeypatch.setattr(visibility_module.fga, "batch_check", recorder)
    return Visibility(client=WIRED, enabled=True), recorder


@pytest.mark.asyncio
async def test_a_bare_dataset_name_is_qualified_as_a_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lineage's convention, unchanged — this is the behaviour the prefix was written for."""
    view, recorder = _view(monkeypatch, {"table:silver$pages"})

    visible = await view.visible("alice", {"silver$pages"})

    assert recorder.objects == ["table:silver$pages"]
    assert visible == {"silver$pages"}


@pytest.mark.asyncio
async def test_an_already_qualified_id_is_not_qualified_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    view, recorder = _view(monkeypatch, {"table:db1$t"})

    visible = await view.visible("alice", {"table:db1$t"})

    assert recorder.objects == ["table:db1$t"], "the governance id was re-prefixed into table:table:…"
    assert visible == {"table:db1$t"}


@pytest.mark.asyncio
async def test_a_governance_id_keeps_its_own_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant on a warehouse is checked on the WAREHOUSE, not on a table that does not exist.

    Better than merely un-breaking it: `can_get_metadata` is defined on the container types too, so
    honouring the stamped type asks the question the model actually answers.
    """
    view, recorder = _view(monkeypatch, {"warehouse:acme"})

    visible = await view.visible("alice", {"warehouse:acme"})

    assert recorder.objects == ["warehouse:acme"]
    assert visible == {"warehouse:acme"}


@pytest.mark.asyncio
async def test_the_delivery_gate_uses_the_same_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sees_all` shares `_filter`, so fixing one must fix both — and it must not widen either."""
    view, recorder = _view(monkeypatch, {"table:db1$t"})

    assert await view.sees_all("alice", {"table:db1$t"}) is True
    assert recorder.objects == ["table:db1$t"]

    assert await view.sees_all("alice", {"warehouse:acme"}) is False
