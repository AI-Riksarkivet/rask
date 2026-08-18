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
from catalog.services.dataplane import create_table
from catalog.services.publication import publish
from lance_namespace import connect


SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string())])
TABLE_ID = ["pages"]


def _ipc(ids: list[int | None]) -> bytes:
    table = pa.table(
        {"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in range(len(ids))])},
        schema=SCHEMA,
    )
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, runtime-only
    namespace = connect("dir", {"root": str(tmp_path)})
    create_table(namespace, {}, TABLE_ID, _ipc([1, 2, 3]), mode="create")
    return namespace


def _uri(ns) -> str:  # noqa: ANN001
    from catalog.core.namespace import open_dataset

    return open_dataset(ns, {}, TABLE_ID).uri


def _write(ns, ids: list[int | None]) -> int:  # noqa: ANN001
    table = pa.table({"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in range(len(ids))])}, schema=SCHEMA)
    return int(lance.write_dataset(table, _uri(ns), mode="overwrite", data_storage_version="2.2").version)


def _empty(ns) -> int:  # noqa: ANN001
    table = pa.table({"id": pa.array([], pa.int64()), "payload": pa.array([], pa.string())}, schema=SCHEMA)
    return int(lance.write_dataset(table, _uri(ns), mode="overwrite", data_storage_version="2.2").version)


class TestAnAcceptedFindingPublishes:
    def test_an_empty_batch_can_be_accepted(self, ns) -> None:  # noqa: ANN001
        """`row_count_positive` is the archetype of an UNUSUAL finding: a batch that legitimately
        shipped zero rows is not broken, and a service cannot tell that from a bug — a person can."""
        version = _empty(ns)

        refused = publish(ns, {}, table_id=TABLE_ID, version=version, key_column="id")
        assert refused.published is False

        accepted = publish(ns, {}, table_id=TABLE_ID, version=version, key_column="id", accept_assertions=["row_count_positive"])
        assert accepted.published is True
        assert accepted.accepted == ["row_count_positive"]

    def test_a_MISSING_declared_column_can_be_accepted(self, ns) -> None:  # noqa: ANN001
        """The other reviewable class: a consumer agreed to the schema change."""
        version = _write(ns, [1, 2, 3])

        accepted = publish(
            ns,
            {},
            table_id=TABLE_ID,
            version=version,
            key_column="id",
            required_columns=["id", "thumbnail"],
            accept_assertions=["column_declared"],
        )

        assert accepted.published is True


class TestStructuralFindingsAreNeverAcceptable:
    def test_a_NULL_KEY_is_refused_even_when_named(self, ns) -> None:  # noqa: ANN001
        """The load-bearing refusal. The medallion's review will not ask about this; the door will not
        act on it either, so bypassing the workflow buys nothing."""
        version = _write(ns, [1, None, 3])

        result = publish(ns, {}, table_id=TABLE_ID, version=version, key_column="id", accept_assertions=["not_null"])

        assert result.published is False
        assert result.accepted == []
        assert "not_null" in (result.reason or "")

    def test_naming_EVERYTHING_does_not_help(self, ns) -> None:  # noqa: ANN001
        """There is no spelling of the request that publishes corrupt data."""
        version = _write(ns, [1, None, 3])

        result = publish(
            ns,
            {},
            table_id=TABLE_ID,
            version=version,
            key_column="id",
            accept_assertions=["not_null", "blob_resolves", "row_count_positive", "column_declared"],
        )

        assert result.published is False


class TestAnOverrideIsNotABlanketForce:
    def test_an_UNNAMED_failure_still_refuses(self, ns) -> None:  # noqa: ANN001
        """Accepting one finding must not wave through a second the approver never saw — which is the
        difference between this and `force=true`."""
        version = _empty(ns)

        result = publish(
            ns,
            {},
            table_id=TABLE_ID,
            version=version,
            key_column="id",
            required_columns=["thumbnail"],
            accept_assertions=["row_count_positive"],
        )

        assert result.published is False
        assert "column_declared" in (result.reason or "")

    def test_accepting_a_finding_that_did_not_occur_changes_nothing(self, ns) -> None:  # noqa: ANN001
        """A clean version publishes, and records no acceptance — the field means "waved through",
        never "was asked for"."""
        version = _write(ns, [1, 2, 3])

        result = publish(ns, {}, table_id=TABLE_ID, version=version, key_column="id", accept_assertions=["row_count_positive"])

        assert result.published is True
        assert result.accepted == []

    def test_the_default_is_unchanged(self, ns) -> None:  # noqa: ANN001
        version = _empty(ns)

        assert publish(ns, {}, table_id=TABLE_ID, version=version, key_column="id").published is False


class TestTheOverrideNeedsAHigherRungThanPublishItself:
    """Publishing is owner-tier (`can_update_tag`); accepting a finding the gate raised is a
    VALIDATOR's act. An override that needed only what an ordinary publish needs would be no gate at
    all — any owner could wave through their own failed batch."""

    def test_the_route_requires_can_promote_when_assertions_are_accepted(self) -> None:
        import inspect

        from catalog.api.v1.endpoints import publication as endpoint

        source = inspect.getsource(endpoint)
        assert "if body.accept_assertions:" in source
        assert 'relation="can_promote"' in source, (
            "an accepted-assertion publish must cross a rung ABOVE can_update_tag; without it the override is available to anyone who could already publish"
        )

    def test_an_ordinary_publish_crosses_no_extra_door(self) -> None:
        """The check is conditional on purpose — gating every publish at validator would make the
        cascade's own registrations impossible."""
        import inspect

        from catalog.api.v1.endpoints import publication as endpoint

        body = inspect.getsource(endpoint.publish_table)
        gate = body.index("if body.accept_assertions:")
        call = body.index('relation="can_promote"')
        assert gate < call, "the validator check must sit inside the accept_assertions branch"
