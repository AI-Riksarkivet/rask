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

import inspect

import pytest

from service_kit.lakehouse.warehouse_registry import namespace_for, tier_namespace


class TestTheSharedHelper:
    """It lives in service-kit beside `project_namespace`, not in the annotator — the same reason
    `project_namespace` itself moved out of the medallion: a convention two services must agree on
    cannot live inside one of them."""

    def test_a_tenant_qualifies_the_tier(self) -> None:
        assert namespace_for("acme", "silver") == "acme-silver"

    def test_no_tenant_is_the_single_tenant_default_unchanged(self) -> None:
        """Byte-identical to today's behaviour for an untenanted estate — the fix must not rename
        every existing namespace."""
        assert namespace_for("", "silver") == "silver"

    def test_an_unsafe_project_does_not_become_a_name_segment(self) -> None:
        """The guard `is_safe_project` already applies elsewhere, for the stated reason: a value
        outside the path-safe shape must never become an S3 prefix or a lineage-name qualifier."""
        assert namespace_for("../etc", "silver") == "silver"
        assert namespace_for("a b", "silver") == "silver"

    def test_the_tier_name_comes_from_the_same_env_the_medallion_reads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A tier the writer and the cascade disagree about is a write nothing downstream sees."""
        monkeypatch.setenv("MEDALLION_SILVER_NAMESPACE", "curated")
        assert tier_namespace("silver") == "curated"
        assert namespace_for("acme", "silver") == "acme-curated"

    def test_it_defaults_to_the_tier_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MEDALLION_SILVER_NAMESPACE", raising=False)
        assert tier_namespace("silver") == "silver"


class TestTheDoorResolvesBeforeItAuthorizes:
    def test_the_bare_literal_is_gone_from_the_door(self) -> None:
        from annotator.api.v1.endpoints import project_events

        source = inspect.getsource(project_events)
        assert 'DEFAULT_TARGET_NAMESPACE: Final[str] = "silver"' not in source, (
            "the door still defaults to an unqualified namespace, so every tenant publishes into one"
        )

    def test_the_door_derives_the_target_from_the_projects_tenant(self) -> None:
        from annotator.api.v1.endpoints import project_events

        source = inspect.getsource(project_events.fire_project_event)
        assert "namespace_for" in source, "the door does not tenant-qualify the publish target"
        assert "tenant" in source, "the door does not read the project's tenant"

    def test_the_SAME_namespace_is_authorized_and_dispatched(self) -> None:
        """The ordering hazard, asserted structurally: the value handed to `_authorize_publish` and
        the value put on the actor payload must be one variable, not two expressions that happen to
        agree today."""
        from annotator.api.v1.endpoints import project_events

        source = inspect.getsource(project_events.fire_project_event)
        assert "_authorize_publish(checker, subject, project_id, target)" in source, "the authorized namespace must be the resolved one"
        assert '"target_namespace": target' in source, "the dispatched namespace must be the authorized one"


class TestTheFallbackSitesAreQualifiedToo:
    @pytest.mark.parametrize(
        ("module", "what"),
        [
            ("annotator.projects.project_actor", "the actor pins the namespace with the publish token"),
            ("annotator.projects.lakehouse", "the watchdog resumes a publish with no request in sight"),
        ],
    )
    def test_no_bare_silver_literal_remains(self, module: str, what: str) -> None:
        import importlib

        source = inspect.getsource(importlib.import_module(module))
        assert 'or "silver"' not in source, f"{module} still falls back to an unqualified namespace — {what}"
