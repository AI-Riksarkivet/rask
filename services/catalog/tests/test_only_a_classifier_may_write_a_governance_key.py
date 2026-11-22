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

CLASSIFYING IS NOT WRITING (ADR 0225, owner 2026-11-21): a body that changes governance keys and nothing else asks
``can_classify`` in ``can_write_data``'s place, so a classifier labels columns holding no writer rung; one that changes
any ordinary key, alone or beside a label, or replaces a field's metadata, still asks ``can_write_data``. A caller
holding nothing on the table is refused 403 code 15 whatever its body.

Every decision is the real model's, on a store of this test's own: alice owns ``lance_catalog$db$t`` and has labelled
its ``payload`` ``pii``; bob holds exactly the rungs a case names. A refused write leaves every field's metadata as it
was.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import lance
import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.audit import AUDIT_LOGGER


TABLE = "/v1/table/lance_catalog$db$t"
KEY = "rask.classification"

_CLEAR_BY_NULL = [{"path": "payload", "metadata": {KEY: None}}]
_CLEAR_BY_REPLACE = [{"path": "payload", "metadata": {}, "replace": True}]
_LABEL_NOTE_RESTRICTED = [{"path": "note", "metadata": {KEY: "restricted"}}]
_LABEL_BOTH = [{"path": "payload", "metadata": {KEY: "pii"}}, {"path": "note", "metadata": {KEY: "restricted"}}]
_ORDINARY = [{"path": "payload", "metadata": {"unit": "bytes", "owner": "team-a"}}]
_RELABEL_BY_REPLACE = [{"path": "payload", "metadata": {KEY: "pii"}, "replace": True}]
_LABEL_AND_ORDINARY = [{"path": "note", "metadata": {KEY: "pii", "unit": "bytes"}}]


def _field_metadata(location: str) -> dict[str, dict[bytes, bytes]]:
    schema = lance.dataset(location).schema
    return {name: dict(schema.field(name).metadata or {}) for name in schema.names}


@pytest.mark.parametrize(
    ("held", "updates", "status", "labels"),
    [
        pytest.param({"writer"}, _CLEAR_BY_NULL, 403, {"payload": "pii", "note": None}, id="a-writer-cannot-clear-a-label-by-a-null-value"),
        pytest.param({"writer"}, _CLEAR_BY_REPLACE, 403, {"payload": "pii", "note": None}, id="a-writer-cannot-clear-a-label-by-a-replace"),
        pytest.param({"writer", "apply:restricted"}, _LABEL_NOTE_RESTRICTED, 403, {"payload": "pii", "note": None}, id="a-writer-cannot-label"),
        pytest.param(
            {"classifier", "apply:restricted"},
            _LABEL_NOTE_RESTRICTED,
            200,
            {"payload": "pii", "note": "restricted"},
            id="a-classifier-labels-holding-no-writer-rung",
        ),
        pytest.param({"classifier"}, _LABEL_NOTE_RESTRICTED, 403, {"payload": "pii", "note": None}, id="a-classifier-needs-the-value-too"),
        pytest.param({"classifier"}, _CLEAR_BY_NULL, 200, {"payload": None, "note": None}, id="clearing-takes-the-table-rung-and-no-value-grant"),
        pytest.param({"classifier", "apply:pii"}, _LABEL_BOTH, 403, {"payload": "pii", "note": None}, id="every-value-in-the-body-is-checked"),
        pytest.param({"writer"}, _ORDINARY, 200, {"payload": "pii", "note": None}, id="an-ordinary-field-property-takes-no-governance-rung"),
        pytest.param({"classifier"}, _ORDINARY, 403, {"payload": "pii", "note": None}, id="a-classifier-cannot-write-an-ordinary-key"),
        pytest.param(
            {"classifier", "apply:pii"}, _LABEL_AND_ORDINARY, 403, {"payload": "pii", "note": None}, id="a-classifier-cannot-write-one-beside-a-label"
        ),
        pytest.param(
            {"classifier", "apply:pii"}, _RELABEL_BY_REPLACE, 403, {"payload": "pii", "note": None}, id="a-classifier-cannot-replace-a-fields-metadata"
        ),
        pytest.param(set(), _LABEL_NOTE_RESTRICTED, 403, {"payload": "pii", "note": None}, id="a-caller-holding-nothing-is-refused"),
    ],
)
def test_only_a_classifier_may_write_a_governance_key(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    held: set[str],
    updates: list[dict[str, Any]],
    status: int,
    labels: dict[str, str | None],
) -> None:
    catalog = governed_catalog
    location = _a_table_bob_holds(catalog, held)
    before = _field_metadata(location)

    answered = catalog.client.post(f"{TABLE}/update_field_metadata", json={"updates": updates}, headers=catalog.bearer("bob"))

    assert answered.status_code == status, answered.text
    if status == 403:
        assert (answered.json()["code"], _field_metadata(location)) == (15, before), answered.text
    schema = lance.dataset(location).schema
    assert {name: (schema.field(name).metadata or {}).get(KEY.encode(), b"").decode() or None for name in labels} == labels


@pytest.mark.parametrize(
    ("held", "detail", "fields"),
    [
        pytest.param(
            {"classifier"},
            "update_field_metadata: a field this request names is not on the table (nope)",
            None,
            id="a-classifier-who-cannot-see-the-table-is-told-only-its-own-path",
        ),
        pytest.param({"classifier", "reader"}, None, ["id", "payload", "note"], id="a-classifier-who-sees-the-table-is-told-its-fields"),
    ],
)
def test_a_missing_path_discloses_no_schema_the_caller_cannot_see(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    held: set[str],
    detail: str | None,
    fields: list[str] | None,
) -> None:
    """A caller who cannot see the table is told the rask sentence naming only its own path; one who can is told Lance's
    own answer, whose field list is read out of it (the sentence around the list is Lance's and not pinned here)."""
    catalog = governed_catalog
    location = _a_table_bob_holds(catalog, held)
    before = _field_metadata(location)

    answered = catalog.client.post(
        f"{TABLE}/update_field_metadata", json={"updates": [{"path": "nope", "metadata": {KEY: None}}]}, headers=catalog.bearer("bob")
    )

    problem = answered.json()
    assert (answered.status_code, problem["code"], _field_metadata(location)) == (404, 12, before), answered.text
    if detail is not None:
        assert problem["detail"] == detail
    else:
        listed = re.search(r"Available fields: \[(.*)\]", problem["detail"])
        assert listed is not None, problem["detail"]
        assert re.findall(r"'(\w+)'", listed.group(1)) == fields, problem["detail"]


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        pytest.param("", "''", id="an-empty-value"),
        pytest.param("x" * 242, f"'{'x' * 64}'… (242 characters)", id="a-value-past-openfgas-256-rune-object"),
        pytest.param("a b", "'a b'", id="a-space"),
        pytest.param("a\tb", "'a\\tb'", id="a-control-character"),
        pytest.param("a:b", "'a:b'", id="a-second-colon"),
        pytest.param("a#b", "'a#b'", id="a-hash"),
        pytest.param("*", "'*'", id="the-typed-wildcard"),
    ],
)
def test_a_value_openfga_cannot_name_is_refused_as_bad_input_before_any_check(
    governed_catalog: Any,  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    caplog: pytest.LogCaptureFixture,
    value: str,
    shown: str,
) -> None:
    """OpenFGA v1.18.3 refuses each of these as an object id (``classification:<value>``), which the door read as an outage:
    503 and an ``authz_unavailable`` record for a caller's bad input. Each is refused 400 code 13 before ``can_apply`` is
    asked, so no decision on it is recorded and the field keeps its metadata."""
    catalog = governed_catalog
    location = _a_table_bob_holds(catalog, {"classifier"})
    before = _field_metadata(location)
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    caplog.clear()

    answered = catalog.client.post(
        f"{TABLE}/update_field_metadata", json={"updates": [{"path": "note", "metadata": {KEY: value}}]}, headers=catalog.bearer("bob")
    )

    asked = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__.get("audit.relation") == "can_apply"]
    outages = [r for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__.get("audit.reason") == "authz_unavailable"]
    assert (answered.status_code, answered.json()["code"], answered.json()["detail"], _field_metadata(location), asked, outages) == (
        400,
        13,
        f"update_field_metadata: {KEY} value {shown} cannot name a classification (OpenFGA takes 1 to 241 characters, none of them a space, "
        "a control character, ':' or '#', and not the wildcard '*')",
        before,
        [],
        [],
    ), answered.text


def _a_table_bob_holds(catalog: Any, held: set[str]) -> str:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    """``lance_catalog$db$t``, alice's, its ``payload`` labelled ``pii``, with bob holding ``held``; its location."""
    alice = catalog.bearer("alice")
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
    return location
