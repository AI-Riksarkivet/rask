"""CONTRACT (owner ruling 2026-09-10): a table a SERVICE registers is not owned by that service.

LH-052 withdrew the cascade's `owner` on every tenant WAREHOUSE. It did not touch the second, quieter
grant: `seed_ownership` gives `owner` on the just-created object to whoever created it, and for a
stage runner's output that creator is the stage runner. So a cascade identity kept `can_drop`,
`can_deregister`, `can_restore` and `manage_grants` on every table it had ever registered — which,
for the cascade, is every governed tier of every tenant it has run in. Narrower than the warehouse
grant it replaced, and not nothing.

THE PROJECT ADMIN ALREADY OWNS IT, so nothing needs to be granted in its place. The chain is
`warehouse.owner: … or admin from project` → `namespace.owner: … or owner from parent` →
`table.owner: … or owner from parent`, so a project's admin reaches every table beneath it
transitively. Declining to seed the service therefore leaves the table owned by a HUMAN rather than
by nobody — which is what makes this a withdrawal rather than an orphaning, and it is asserted below
against the real evaluator rather than argued from the model text.

THE DISCRIMINATOR IS THE PRINCIPAL'S TYPE, not a name pattern. Only the service-account verifier
builds a `ServicePrincipal` (`catalog/api/security.py`, [[LH-220]]), so the test asks how the caller was
proved rather than what its subject is called, which would drift the moment a service is renamed.

A PERSON who creates a table still owns it: pinned on a real OpenFGA by
`services/catalog/tests/test_a_grant_is_held_by_the_objects_uuid.py::test_every_tuple_a_create_writes_names_the_objects_uuid`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from catalog.api import fga_deps
from catalog.api.security import Caller
from catalog.core.config import Settings
from service_kit.governed import fga
from service_kit.governed.machine_identity import ServicePrincipal
from tests.unit.named_resolver import NamedResolver, uuid_of


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


def _settings() -> Settings:
    base = {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y"}
    return Settings.model_validate(
        {
            **base,
            "RASK_FGA_ENABLED": True,
            "RASK_OIDC_ENABLED": True,
            "RASK_OIDC_ISSUER": "https://dex",
            "RASK_OIDC_AUDIENCE": "rask",
            "RASK_OIDC_CONNECTORS": {"local": "dex"},
        }
    )


async def _tuples_written(token: Caller, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """The TUPLES one table create actually writes, as (user, relation, object).

    Captured at `write_tuples` rather than at `grant_on_create`, because the call still happens either
    way — only its `grant_owner` argument differs — so asserting the call shape would pass against a
    flag that was threaded and then ignored. The tuples are what OpenFGA ends up holding.
    """
    written: list[tuple[str, str, str]] = []

    async def _capture(_client: object, tuples: Any, **_kw: Any) -> None:
        written.extend((t.user, t.relation, t.object) for t in tuples)

    monkeypatch.setattr(fga, "write_tuples", _capture)
    await fga_deps.seed_ownership(
        cast("OpenFgaClient", object()),  # the capture never touches the client
        _settings(),
        token,
        NamedResolver(),
        resource="table",
        segments=["acme-silver", "features"],
        uuid=_TABLE_UUID,
    )
    return written


#: The uuid the create minted for ``acme-silver$features``: the object its grants are held by (ADR 0173 §8).
_TABLE_UUID = uuid_of("table", ["acme-silver", "features"])


@pytest.mark.asyncio
async def test_a_SERVICE_creating_a_table_is_not_granted_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The withdrawal itself: a stage runner registering its output does not become its owner."""
    written = await _tuples_written(ServicePrincipal(service_account="system:serviceaccount:default:rask-sa-silver-to-gold"), monkeypatch)
    assert not [t for t in written if t[1] == "owner"], f"the registering service was granted ownership of its output: {written}"
    # The hierarchy edge is still written, and that is what keeps this a withdrawal rather than an
    # orphaning: ownership descends to the table from the project's admin through this link.
    parent = f"namespace:{uuid_of('namespace', ['acme-silver'])}"
    assert (parent, "parent", f"table:{_TABLE_UUID}") in written, f"the parent edge was dropped with the owner grant: {written}"
