"""A validator may accept a promotion the gate refused — and may never accept a corrupt one.

The medallion's quality gate had two answers and grew a third: a held promotion becomes a QUESTION a
person can answer. Under a tag-driven cascade the resume must move the `published` tag, and no door
did that past a failed gate — `publish` re-ran the assertions and refused, `tags/update` moved the tag
and emitted nothing. So "a validator accepted data the gate refused" was unexpressible.

This is that door, and its whole safety rests on two properties:

  * the caller names EXACTLY which assertions it accepted, so an override is a statement about known
    findings rather than a blanket `force=true` that also waves through whatever appears later;
  * STRUCTURAL failures can never be accepted, by anyone. A null key or an unresolvable blob pointer
    means the data is wrong, not unusual, and no approval makes it right. The medallion's review
    already refuses to ASK about those — this refuses to ACT on them, so a caller that bypasses the
    workflow cannot publish corrupt data either.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance_namespace import CreateNamespaceRequest, connect

from catalog.services import table_claims
from catalog.services.dataplane import create_table
from catalog.services.publication import publish
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.objectfs import StorageOptions


SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string())])
TABLE_ID = ["db", "pages"]


def _table(ids: list[int | None]) -> pa.Table:
    return pa.table(
        {"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in range(len(ids))])},
        schema=SCHEMA,
    )


def _registry(tmp_path: Path) -> base_registry.BaseRegistry:
    """The control root the doors read, so the table holds its location under a uuid as every table does (ADR 0173 §6)."""
    return base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={})


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, runtime-only
    namespace = connect("dir", {"root": str(tmp_path)})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(namespace, StorageOptions(), TABLE_ID, _table([1, 2, 3]), root=str(tmp_path), mode="create", registry=_registry(tmp_path), holder="db$pages")
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID)


def _uri(ns) -> str:  # noqa: ANN001
    from tables_by_name import open_by_name

    return open_by_name(ns, StorageOptions(), TABLE_ID).uri


def _write(ns, ids: list[int | None]) -> int:  # noqa: ANN001
    table = pa.table({"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in range(len(ids))])}, schema=SCHEMA)
    return int(lance.write_dataset(table, _uri(ns), mode="overwrite", data_storage_version="2.2").version)


def _empty(ns) -> int:  # noqa: ANN001
    table = pa.table({"id": pa.array([], pa.int64()), "payload": pa.array([], pa.string())}, schema=SCHEMA)
    return int(lance.write_dataset(table, _uri(ns), mode="overwrite", data_storage_version="2.2").version)


class TestAnAcceptedFindingPublishes:
    def test_an_empty_batch_can_be_accepted(self, ns, table: DoorTable) -> None:  # noqa: ANN001
        """`row_count_positive` is the archetype of an UNUSUAL finding: a batch that legitimately
        shipped zero rows is not broken, and a service cannot tell that from a bug — a person can."""
        version = _empty(ns)

        refused = publish(table, StorageOptions(), version=version, key_column="id")
        assert refused.published is False

        accepted = publish(table, StorageOptions(), version=version, key_column="id", accept_assertions=["row_count_positive"])
        assert accepted.published is True
        assert accepted.accepted == ["row_count_positive"]

    def test_a_MISSING_declared_column_can_be_accepted(self, ns, table: DoorTable) -> None:  # noqa: ANN001
        """The other reviewable class: a consumer agreed to the schema change."""
        version = _write(ns, [1, 2, 3])

        accepted = publish(
            table,
            StorageOptions(),
            version=version,
            key_column="id",
            required_columns=["id", "thumbnail"],
            accept_assertions=["column_declared"],
        )

        assert accepted.published is True


class TestStructuralFindingsAreNeverAcceptable:
    def test_a_NULL_KEY_is_refused_even_when_named(self, ns, table: DoorTable) -> None:  # noqa: ANN001
        """The load-bearing refusal. The medallion's review will not ask about this; the door will not
        act on it either, so bypassing the workflow buys nothing."""
        version = _write(ns, [1, None, 3])

        result = publish(table, StorageOptions(), version=version, key_column="id", accept_assertions=["not_null"])

        assert result.published is False
        assert result.accepted == []
        assert "not_null" in (result.reason or "")


class TestAnOverrideIsNotABlanketForce:
    def test_an_UNNAMED_failure_still_refuses(self, ns, table: DoorTable) -> None:  # noqa: ANN001
        """Accepting one finding must not wave through a second the approver never saw — which is the
        difference between this and `force=true`."""
        version = _empty(ns)

        result = publish(
            table,
            StorageOptions(),
            version=version,
            key_column="id",
            required_columns=["thumbnail"],
            accept_assertions=["row_count_positive"],
        )

        assert result.published is False
        assert "column_declared" in (result.reason or "")

    def test_accepting_a_finding_that_did_not_occur_changes_nothing(self, ns, table: DoorTable) -> None:  # noqa: ANN001
        """A clean version publishes, and records no acceptance — the field means "waved through",
        never "was asked for"."""
        version = _write(ns, [1, 2, 3])

        result = publish(table, StorageOptions(), version=version, key_column="id", accept_assertions=["row_count_positive"])

        assert result.published is True
        assert result.accepted == []
