"""[[LH-534]] A table no uuid identifies is the estate admin's to deregister, through the spec's own door.

A table's grants are held by the uuid its location's claim records (ADR 0173 §8), so a ``__manifest`` row at a location
no claim names holds none, and every door that resolves its name answers 500 code 18. Measured live on 2026-10-24 (helm
rev 10): ``lh528-85958d7b-bronze$moved``, a row at ``held``, where neither bytes nor a claim lie. Its deregister asks the
estate admin's verb for it, ``can_deregister_unidentified_table`` on the estate root, in place of ``can_deregister``, which no tuple can hold
for it, and refuses anyone else as it refuses a name that holds nothing. A deregister deletes no byte.

The real catalog app over a real ``dir`` namespace, each decision OpenFGA's on a store of this test's own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from lance_namespace import RegisterTableRequest, connect

from lakehouse.governed.audit import AUDIT_LOGGER, AuditOutcome


def test_only_the_estate_admin_deregisters_a_table_no_uuid_identifies(
    governed_catalog: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    catalog = governed_catalog
    created = catalog.create_in_the_estate("alice", "db")
    assert created.status_code == 200, created.text
    connect("dir", {"root": str(tmp_path)}).register_table(RegisterTableRequest(id=["lance_catalog", "db", "lost"], location="held"))
    catalog.store.write((catalog.person("root"), "owner", "estate:rask"))

    refused = catalog.client.post("/v1/table/lance_catalog$db$lost/deregister", headers=catalog.bearer("alice"), json={})
    removed = catalog.client.post("/v1/table/lance_catalog$db$lost/deregister", headers=catalog.bearer("root"), json={})
    described = catalog.client.post("/v1/table/lance_catalog$db$lost/describe", headers=catalog.bearer("alice"), json={})

    assert (refused.status_code, refused.json().get("detail"), removed.status_code, described.status_code) == (
        403,
        "can_deregister required on table:lance_catalog$db$lost",
        200,
        404,
    )
    # The trail names the estate verb each decision was made on, the refusal first.
    assert [
        (record.__dict__["audit.outcome"], record.__dict__["audit.relation"])
        for record in caplog.records
        if record.name == AUDIT_LOGGER and record.__dict__.get("audit.reason") == "unidentified_table"
    ] == [(AuditOutcome.DENY, "can_deregister_unidentified_table"), (AuditOutcome.ALLOW, "can_deregister_unidentified_table")]


def test_only_the_estate_admin_declares_a_tier_on_a_namespace_no_project_owns(governed_catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """A tier is declared by the namespace's project admin (ADR 0160); a namespace of the estate's own warehouse has none,
    so its tier is the estate admin's, asked as its own verb on the estate root. Both may create a namespace there."""
    catalog = governed_catalog
    catalog.store.write(
        (catalog.person("root"), "owner", "estate:rask"),
        (catalog.person("root"), "writer", "warehouse:lance_catalog"),
        (catalog.person("alice"), "writer", "warehouse:lance_catalog"),
    )
    declaring = {"properties": {"rask.tier": "silver"}}

    refused = catalog.client.post("/v1/namespace/lance_catalog$staged/create", headers=catalog.bearer("alice"), json=declaring)
    declared = catalog.client.post("/v1/namespace/lance_catalog$staged/create", headers=catalog.bearer("root"), json=declaring)

    assert (refused.status_code, refused.json().get("detail"), declared.status_code) == (403, "can_declare_unowned_tier required on estate:rask", 200), (
        declared.text
    )
