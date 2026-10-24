"""[[LH-534]] A table no uuid identifies is the estate admin's to deregister, through the spec's own door.

A table's grants are held by the uuid its location's claim records (ADR 0173 §8), so a ``__manifest`` row at a location
no claim names holds none, and every door that resolves its name answers 500 code 18. Measured live on 2026-10-24 (helm
rev 10): ``lh528-85958d7b-bronze$moved``, a row at ``held``, where neither bytes nor a claim lie. Its deregister asks the
estate admin's rung, ``can_observe_events`` on the estate root, in place of ``can_deregister``, which no tuple can hold
for it, and refuses anyone else as it refuses a name that holds nothing. A deregister deletes no byte.

The real catalog app over a real ``dir`` namespace, each decision OpenFGA's on a store of this test's own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lance_namespace import RegisterTableRequest, connect


def test_only_the_estate_admin_deregisters_a_table_no_uuid_identifies(governed_catalog: Any, tmp_path: Path) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    catalog = governed_catalog
    created = catalog.client.post("/v1/namespace/db/create", headers=catalog.bearer("alice"), json={})
    assert created.status_code == 200, created.text
    connect("dir", {"root": str(tmp_path)}).register_table(RegisterTableRequest(id=["db", "lost"], location="held"))
    catalog.store.write((catalog.person("root"), "owner", "estate:rask"))

    refused = catalog.client.post("/v1/table/db$lost/deregister", headers=catalog.bearer("alice"), json={})
    removed = catalog.client.post("/v1/table/db$lost/deregister", headers=catalog.bearer("root"), json={})
    described = catalog.client.post("/v1/table/db$lost/describe", headers=catalog.bearer("alice"), json={})

    assert (refused.status_code, refused.json().get("detail"), removed.status_code, described.status_code) == (
        403,
        "can_deregister required on table:db$lost",
        200,
        404,
    )
