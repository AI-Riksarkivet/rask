"""CAT-CORE-12 — the three control-root registries VALIDATE their records instead of eyeballing them.

Warehouses, namespace→warehouse bindings and projects all parse ``json.loads`` into a raw ``dict`` and
then hand-check it with ``.get()`` truthiness::

    if isinstance(record, dict) and record.get("id") and record.get("bucket") and record.get("project"):

Truthiness answers "is this key present and non-empty", never "is this the right SHAPE". Every consumer
then treats the values as strings — ``projects_claiming_bucket`` compares ``r.get("bucket") == bucket``
and coerces ``str(r["project"])``, the resolver reads ``binding["root_uri"]`` straight into a namespace
connection — so a record whose ``bucket`` is a LIST passes the guard, is returned as live, and then
matches no bucket claim at all. That is the wrong direction to fail in: the claim guard exists to stop
a second project registering a bucket somebody already owns.

The declared shape is the fix, and these are the records it must now refuse: present, non-empty, and
the wrong type. The existing missing-key and corrupt-JSON tolerance is asserted alongside, because a
model that made a listing fail closed on one bad object would be a different (worse) defect —
``list_warehouses``' own docstring says one bad record must never void the whole listing.
"""

from __future__ import annotations

import json
from pathlib import Path

from catalog.services import projects as proj_svc
from catalog.services import warehouses as wh_svc


def _root(tmp_path: Path) -> str:
    return f"file://{tmp_path}"


def _write(tmp_path: Path, prefix: str, name: str, payload: object) -> None:
    directory = tmp_path / prefix
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps(payload))


def test_a_warehouse_record_with_a_WRONG_TYPED_identity_field_is_skipped(tmp_path: Path) -> None:
    good = {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "created_at": "t"}
    wh_svc.put_warehouse(_root(tmp_path), {}, good)
    # Present and truthy on every identity field, and structurally useless: `bucket` can never equal
    # the string a claim guard compares it to, and `project` reaches `str(...)` as a dict repr.
    _write(tmp_path, "_warehouses", "zz-typed", {"id": 5, "bucket": ["bkt-a"], "project": {"name": "acme"}})
    assert wh_svc.list_warehouses(_root(tmp_path), {}) == [good]


def test_a_binding_record_with_a_WRONG_TYPED_identity_field_is_skipped(tmp_path: Path) -> None:
    wh_svc.bind_namespace(_root(tmp_path), {}, "bronze", "wh-a", "s3://bkt-a")
    _write(tmp_path, "_warehouses/bindings", "zz-typed", {"top_ns": ["bronze"], "warehouse_id": 7, "root_uri": None})
    readable, skipped = wh_svc.read_bindings(_root(tmp_path), {})
    assert [b["top_ns"] for b in readable] == ["bronze"]
    assert len(skipped) == 1, "an unusable binding must be REPORTED as skipped — the delete door fails closed on it"


def test_a_project_record_with_a_WRONG_TYPED_id_is_skipped(tmp_path: Path) -> None:
    good = {"id": "acme", "created_at": "t", "created_by": "root"}
    proj_svc.put_project(_root(tmp_path), {}, good)
    _write(tmp_path, "_projects", "zz-typed", {"id": ["acme"]})
    assert proj_svc.list_projects(_root(tmp_path), {}) == [good]


def test_the_existing_tolerance_is_unchanged(tmp_path: Path) -> None:
    """A listing must still survive corrupt JSON, a non-object and a missing key — one bad object
    voiding the whole registry would turn one tenant's corruption into an estate-wide outage."""
    good = {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "created_at": "t"}
    wh_svc.put_warehouse(_root(tmp_path), {}, good)
    (tmp_path / "_warehouses" / "zzz-corrupt.json").write_text("{truncated")
    (tmp_path / "_warehouses" / "zzz-notdict.json").write_text('["not", "a", "record"]')
    (tmp_path / "_warehouses" / "zzz-idless.json").write_text('{"bucket": "x", "project": "p"}')
    assert wh_svc.list_warehouses(_root(tmp_path), {}) == [good]
