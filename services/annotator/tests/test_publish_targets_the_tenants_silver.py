"""Every tenant's annotations published into ONE shared `silver` namespace.

`docs/architecture/ingest-and-tier-movement.md` §3 FIX 1: derive the publish target from the tenant, "instead of the bare
literal `silver`". The literal is still there — and the comment above it says the opposite of what the
code does: *"the default target is the tenant warehouse's `silver` namespace"*, over
`DEFAULT_TARGET_NAMESPACE: Final[str] = "silver"`.

This is the defect `services/ingest/src/ingest/naming.py` was written to end, one tier over. Its
docstring makes the argument: a PROJECT is not a namespace, the tier is project-qualified
(`bind86-bronze`), and "two writers of one convention will drift; the only question is when." Ingest
qualifies bronze. The annotator does not qualify silver, so with two tenants annotating, both land in
`silver$labels_<id>` — one namespace, one FGA parent, one set of grants.

THE ORDERING IS THE DANGEROUS PART, and it is why this cannot be fixed at one site. The HTTP door
checks `can_create_table` on `namespace:<target>` BEFORE the actor writes. Qualifying the actor's
default alone would make the door authorize `namespace:silver` while the write lands in
`acme-silver` — a gate checking a different object than the one written, which is worse than the
unqualified write it was meant to fix. The door resolves the effective namespace, authorizes THAT,
and hands THAT to the actor, so one string crosses the gate and reaches the table id.
"""

from __future__ import annotations

from service_kit.lakehouse.warehouse_registry import namespace_for


class TestTheSharedHelper:
    """It lives in service-kit beside `project_namespace`, not in the annotator — the same reason
    `project_namespace` itself moved out of the medallion: a convention two services must agree on
    cannot live inside one of them."""

    def test_a_tenant_qualifies_the_tier(self) -> None:
        assert namespace_for("acme", "silver") == "acme-silver"
