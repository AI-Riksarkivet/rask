"""A warehouse cascade delete revokes the tuples of the TABLES it destroys, not only the namespace's.

[[LH-148]]. `DELETE /v1/warehouses/{id}?cascade=true` drops each bound namespace through the NATIVE
`drop_namespace`, which destroys its child tables inside one call — and then revoked
`namespace:<id>` and nothing else. Every table under it kept its FGA tuples while its dataset was
gone.

THAT IS WHERE THE 121 CAME FROM, measured 2026-09-20. `governed_tables` in the lineage reconcile is
"the table ids carrying at least one authorization tuple", so those orphans stayed in `governed`
with no dataset node to match — surfacing as `unknown_to_graph=126` under an alert whose words are
"a write lost its provenance". Six track-a e2e warehouses at ~20 tables each accounts for almost
all of it.

THE NAMESPACE DOOR ALREADY DOES THIS RIGHT, which is what makes it a gap rather than a design:
`_collect_descendants` exists there to enumerate children BEFORE the cascade removes them, precisely
so their tuples can be revoked after. The warehouse path took the same destructive action with none
of it.

THE WIRING IS ASSERTED SEPARATELY FROM THE BEHAVIOUR, and that is not ceremony: the first version of
this file only checked that the helper EXISTED, and mutation-checking showed deleting the call site
left it green. A helper nothing calls is not a fix.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest

from catalog.api.v1.endpoints import namespaces as ns_api
from catalog.api.v1.endpoints import warehouses as wh_api
from catalog.core.config import Settings


@pytest.mark.asyncio
async def test_every_descendant_object_has_its_tuples_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE BEHAVIOUR: a table under the doomed namespace is revoked by its OWN object id."""
    revoked: list[str] = []

    async def _fake_revoke(_client: Any, _settings: Any, _token: Any, obj: str) -> int:
        revoked.append(obj)
        return 1

    monkeypatch.setattr(wh_api, "_revoke_tuples", _fake_revoke)
    monkeypatch.setattr(ns_api, "_collect_descendants", lambda _ns, _seg: [("table", ["acme", "doomed"]), ("namespace", ["acme", "nested"])])

    class _Settings:
        delimiter = "$"

    removed = await wh_api._revoke_descendants_of(object(), None, cast(Settings, _Settings()), None, ["acme"])  # noqa: SLF001 — the helper is the unit

    assert removed == 2
    assert revoked == ["table:acme$doomed", "namespace:acme$nested"], f"descendants were not revoked by their own ids: {revoked}"


@pytest.mark.asyncio
async def test_an_unlistable_subtree_does_not_block_the_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort by contract: a cascade that stopped here would leave MORE orphans, not fewer."""

    def _boom(_ns: Any, _seg: Any) -> Any:
        raise RuntimeError("namespace unreadable")

    monkeypatch.setattr(ns_api, "_collect_descendants", _boom)

    class _Settings:
        delimiter = "$"

    assert await wh_api._revoke_descendants_of(object(), None, cast(Settings, _Settings()), None, ["acme"]) == 0  # noqa: SLF001


def test_the_cascade_actually_calls_it_and_before_the_drop() -> None:
    """THE WIRING. A helper nothing calls is not a fix, and the ORDER is the whole mechanism: after
    `drop_namespace` the children cannot be listed, so a revoke placed later would enumerate nothing."""
    source = inspect.getsource(wh_api.delete_warehouse)

    assert "_revoke_descendants_of" in source, "the warehouse cascade does not revoke descendants at all"
    assert source.index("_revoke_descendants_of") < source.index('"drop_namespace"'), (
        "the descendant revoke runs AFTER the native drop, which destroys the children it needed to enumerate"
    )
