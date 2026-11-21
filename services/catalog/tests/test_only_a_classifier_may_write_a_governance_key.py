"""A plain table WRITER may not label data, and may not un-label it either; a classifier attaches only the values it holds.

[[LH-058]]. `rask.classification` on a field decides whether that table's bytes may be vended as raw
object storage. `update_field_metadata` is the one door that writes field metadata, and it resolves to
`can_write_data`. So before this gate, any writer could CLEAR a classification and make their own table directly vendable
again. The control the estate had just gained could be switched off by the population it governs.

THE REFERENCE MODEL NAMES THE PRINCIPLE, not the mechanism: "independent of `modify` (separation of
duties: classify without holding data/DDL rights)". The rung here is `can_classify: classifier`, with
`classifier` shaped exactly like `maintainer` — grantable, conditional, cascading from the parent —
because it is the same kind of privilege: a standing, narrow capability held by someone who is not the
data's owner.

NOT the reference's `tag` TYPE, and the deviation is Lance's: `tag` already means a VERSION REF in this
estate (`can_create_tag`, `_refs/tags/`), because the format defines it that way.

THE DELETE IS THE CASE THAT MATTERS. `update_field_metadata` signals removal with a `None` value, so a
gate that only looked at non-null writes would leave the exact escape open — set is a restriction the
writer would not want, clear is the one they would. `replace` drops every key the body does NOT name, so it un-labels
without ever spelling `rask.`: measured against a real dataset, after `update_field_metadata({"payload": {}},
replace=True)` the classification is GONE and the rows are untouched. A replace is therefore a governance-key write BY
CONSTRUCTION, whether or not the body spells one.

HOLDING `can_classify` IS NOT HOLDING THE LABEL ([[LH-055]], `docs/adr/0070-four-owner-answers-and-the-reference-implementation-that.md`):
the door also asks `can_apply` on each `classification:<value>` the body attaches, every one and not only the first, so
a data-protection officer trusted to mark `pii` cannot mark a table `restricted`. UN-LABELLING IS NOT DELEGATED: `apply`
attaches and never detaches, so a clear asks the table rung alone, and a narrow grant strands no label its holder could
attach and nobody present could remove.

Every decision is the real model's, on a store of this test's own: alice owns ``lance_catalog$db$t`` and has labelled
its ``payload`` ``pii``; bob holds exactly the rungs a case names, ``writer`` among them, since the router asks
``can_write_data`` before the door asks for a governance rung. A refused write leaves every field's metadata as it was.
"""

from __future__ import annotations

from typing import Any

import lance
import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


TABLE = "/v1/table/lance_catalog$db$t"
KEY = "rask.classification"

_CLEAR_BY_NULL = [{"path": "payload", "metadata": {KEY: None}}]
_CLEAR_BY_REPLACE = [{"path": "payload", "metadata": {}, "replace": True}]
_LABEL_NOTE_RESTRICTED = [{"path": "note", "metadata": {KEY: "restricted"}}]
_LABEL_BOTH = [{"path": "payload", "metadata": {KEY: "pii"}}, {"path": "note", "metadata": {KEY: "restricted"}}]
_ORDINARY = [{"path": "payload", "metadata": {"unit": "bytes", "owner": "team-a"}}]


@pytest.mark.parametrize(
    ("held", "updates", "status", "labels"),
    [
        pytest.param({"writer"}, _CLEAR_BY_NULL, 403, {"payload": "pii", "note": None}, id="a-writer-cannot-clear-a-label-by-a-null-value"),
        pytest.param({"writer"}, _CLEAR_BY_REPLACE, 403, {"payload": "pii", "note": None}, id="a-writer-cannot-clear-a-label-by-a-replace"),
        pytest.param({"writer", "apply:restricted"}, _LABEL_NOTE_RESTRICTED, 403, {"payload": "pii", "note": None}, id="a-writer-cannot-label"),
        pytest.param(
            {"writer", "classifier", "apply:restricted"}, _LABEL_NOTE_RESTRICTED, 200, {"payload": "pii", "note": "restricted"}, id="a-classifier-may-label"
        ),
        pytest.param({"writer", "classifier"}, _LABEL_NOTE_RESTRICTED, 403, {"payload": "pii", "note": None}, id="a-classifier-needs-the-value-too"),
        pytest.param({"writer", "classifier"}, _CLEAR_BY_NULL, 200, {"payload": None, "note": None}, id="clearing-takes-the-table-rung-and-no-value-grant"),
        pytest.param({"writer", "classifier", "apply:pii"}, _LABEL_BOTH, 403, {"payload": "pii", "note": None}, id="every-value-in-the-body-is-checked"),
        pytest.param({"writer"}, _ORDINARY, 200, {"payload": "pii", "note": None}, id="an-ordinary-field-property-takes-no-governance-rung"),
    ],
)
def test_only_a_classifier_may_write_a_governance_key(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    held: set[str],
    updates: list[dict[str, Any]],
    status: int,
    labels: dict[str, str | None],
) -> None:
    catalog, alice = governed_catalog, governed_catalog.bearer("alice")
    catalog.store.write((catalog.person("alice"), "apply", "classification:pii"))
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    rows = pa.table({"id": pa.array([1], pa.int64()), "payload": pa.array(["x"]), "note": pa.array(["y"])})
    created = catalog.client.post(f"{TABLE}/create", content=encode_arrow_stream(rows), headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    location = created.json()["location"]
    labelled = catalog.client.post(f"{TABLE}/update_field_metadata", json={"updates": [{"path": "payload", "metadata": {KEY: "pii"}}]}, headers=alice)
    assert labelled.status_code == 200, labelled.text
    for rung in sorted(rung for rung in held if not rung.startswith("apply:")):
        granted = catalog.client.post("/management/v1/table/lance_catalog$db$t/access/grant", json={"user": "dex~bob", "relation": rung}, headers=alice)
        assert granted.status_code == 200, granted.text
    catalog.store.write(*((catalog.person("bob"), "apply", f"classification:{rung.removeprefix('apply:')}") for rung in held if rung.startswith("apply:")))

    answered = catalog.client.post(f"{TABLE}/update_field_metadata", json={"updates": updates}, headers=catalog.bearer("bob"))

    assert answered.status_code == status, answered.text
    if status == 403:
        assert answered.json()["code"] == 15, answered.text
    schema = lance.dataset(location).schema
    assert {name: (schema.field(name).metadata or {}).get(KEY.encode(), b"").decode() or None for name in labels} == labels
