"""CONTRACT (owner ruling 2026-09-10): a table a SERVICE registers is not owned by that service.

LH-052 withdrew a pipeline service's `owner` on every tenant WAREHOUSE. It did not touch the second, quieter
grant: `seed_ownership` gives `owner` on the just-created object to whoever created it, and for a
pipeline service's output that creator is the service. So a pipeline identity kept `can_drop`,
`can_deregister`, `can_restore` and `manage_grants` on every table it had ever registered — which,
for such a service, is every governed table of every tenant it has run in. Narrower than the warehouse
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

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from catalog.api.authz import object_grants
from catalog.api.authz.context import AuthzContext
from catalog.core.config import Settings
from lakehouse.governed import fga_provision, fga_reads
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.machine_identity import ServicePrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from tests.unit.named_resolver import NamedResolver, uuid_of


def _settings() -> Settings:
    base = {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y"}
    return Settings.model_validate(
        {
            **base,
            "RASK_FGA_ENABLED": True,
            "RASK_OIDC_ENABLED": True,
            "RASK_OIDC_ISSUER": "https://dex",
            "RASK_OIDC_AUDIENCE": "rask",
            "RASK_OIDC_IDP_ID": "dex",
            "RASK_OIDC_CONNECTORS": {"local": "dex"},
        }
    )


#: The uuid the create minted for ``acme-silver$features``: the object its grants are held by (ADR 0173 §8).
_TABLE_UUID = uuid_of("table", ["acme-silver", "features"])


def test_a_SERVICE_creating_a_table_is_not_granted_owner(openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """The withdrawal itself, on the real store: a service registering its output does not become its owner, and the
    evaluator grants it no ``can_drop`` on it."""
    service = ServicePrincipal.for_account("system:serviceaccount:default:rask-sa-pipeline")

    async def _create() -> bool:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            await object_grants.seed_ownership(
                AuthzContext(client=client, settings=_settings(), token=service),
                resolver=NamedResolver(),
                resource="table",
                segments=["acme-silver", "features"],
                uuid=_TABLE_UUID,
            )
            return await fga_reads.check(client, user=service.subject, relation="can_drop", obj=f"table:{_TABLE_UUID}")

    may_drop = asyncio.run(_create())

    written = openfga_store.tuples()
    assert not [t for t in written if t[1] == "owner"], f"the registering service was granted ownership of its output: {written}"
    assert may_drop is False, "the registering service may drop its output"
    # The hierarchy edge is still written, and that is what keeps this a withdrawal rather than an
    # orphaning: ownership descends to the table from the project's admin through this link.
    parent = f"namespace:{uuid_of('namespace', ['acme-silver'])}"
    assert (parent, "parent", f"table:{_TABLE_UUID}") in written, f"the parent edge was dropped with the owner grant: {written}"


def test_the_context_keeps_the_principal_the_authenticator_built_and_builds_none_itself() -> None:
    """The type the ownership rule reads is the one the verifier proved: a principal passes as the same object, and its
    fields as a dict, which no verifier built, are refused rather than made into a ``ServicePrincipal``."""
    service = ServicePrincipal.for_account("system:serviceaccount:default:rask-sa-pipeline")
    person = PersonPrincipal(principal=Principal(idp_id="dex", subject="bob"), claims=IDToken(iss="https://dex", sub="bob", aud="rask", exp=1, iat=1))

    kept = [AuthzContext(client=None, settings=_settings(), token=caller).token for caller in (service, person)]
    with pytest.raises(ValidationError) as refused:
        AuthzContext(client=None, settings=_settings(), token=service.model_dump())

    assert ([k is c for k, c in zip(kept, (service, person), strict=True)], [(e["type"], e["ctx"]["class"]) for e in refused.value.errors()]) == (
        [True, True],
        [("is_instance_of", "PersonPrincipal"), ("is_instance_of", "ServicePrincipal")],
    )
