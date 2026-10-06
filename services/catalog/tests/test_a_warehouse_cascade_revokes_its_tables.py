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
"""

from __future__ import annotations

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
