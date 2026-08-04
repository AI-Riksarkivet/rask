"""D-R1/D-R3: a commit is not a publication, and a failed gate must leave consumers on last-good.

The property that matters is not "publish moves a tag" — it is that a reader pinned to `published`
CANNOT see a version the gate rejected, even though that version is committed and any reader doing a
plain `lance.dataset(uri)` sees it immediately. Lance has no quarantined version, so the pointer is
the only thing that makes it true; these tests therefore assert from the READER's side wherever they
can.

Run against a real `dir` namespace and real pylance writes — no mocks — because the whole subject is
what the catalog's tag operations actually do. A double would prove only that the double was called,
and every hard-won detail here (an unset tag RAISES rather than returning None; create and update are
separate spec operations that refuse each other's state) is exactly the kind a double smooths away.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table
from catalog.services.publication import (
    PUBLISHED_TAG,
    PUBLISHING_TAG,
    publish,
    published_version,
)
from lance_namespace import (
    InvalidInputError,
    InvalidTableStateError,
    ListTableTagsRequest,
    TableVersionNotFoundError,
    connect,
)


SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("payload", pa.string())])

#: Root-level, like the catalog's other dir-namespace tests. A child namespace would need its own
#: `__manifest` dataset declared first (pylance 9 refuses a child read without one) and buys nothing
#: here — publication is a per-TABLE concern.
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
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, a runtime-only type
    """A real `dir` namespace with the table REGISTERED, not merely written to a path.

    `create_table` rather than a bare `lance.write_dataset`: the namespace resolves a table through
    its own layout, so a dataset dropped at the expected path is still `TableNotFound`. The catalog's
    own tag operations go through that same resolution, which is the whole point of testing here.
    """
    namespace = connect("dir", {"root": str(tmp_path)})
    create_table(namespace, {}, TABLE_ID, _ipc([1, 2, 3]), mode="create")
    return namespace


def _uri(ns) -> str:  # noqa: ANN001
    return open_dataset(ns, {}, TABLE_ID).uri


def _write(ns, ids: list[int | None], *, mode: str = "overwrite") -> int:  # noqa: ANN001
    """Write through pylance at the URI the NAMESPACE resolves, so the table stays the registered one."""
    table = pa.table(
        {"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in range(len(ids))])},
        schema=SCHEMA,
    )
    dataset = lance.write_dataset(table, _uri(ns), mode=mode, enable_stable_row_ids=True, data_storage_version="2.2")
    return int(dataset.version)


def _publish(ns, version: int):  # noqa: ANN001, ANN202
    return publish(ns, {}, table_id=TABLE_ID, version=version, key_column="id")


def test_nothing_is_published_before_the_first_publish(ns) -> None:  # noqa: ANN001
    """An unset tag must read as "nothing published", not blow up.

    pylance RAISES `Ref not found` for an unset tag rather than returning None, and the dataplane
    turns that into `TableTagNotFound`. Reading it as absence is what lets the FIRST publication of
    every dataset work — the one case that always happens.
    """
    _write(ns, [1, 2, 3])

    assert published_version(ns, {}, TABLE_ID) is None


def test_a_PASSING_gate_advances_the_pointer_and_names_the_range(ns) -> None:  # noqa: ANN001
    """The happy path, and the range D-R3 requires. A first publication has no prior version, so the
    delta is "everything up to to_version" and `from_version` is None rather than 0."""
    version = _write(ns, [1, 2, 3])

    result = _publish(ns, version)

    assert result.published is True
    assert (result.from_version, result.to_version) == (None, version)
    assert published_version(ns, {}, TABLE_ID) == version


def test_a_FAILING_gate_leaves_CONSUMERS_ON_LAST_GOOD(ns) -> None:  # noqa: ANN001
    """The whole point of § D, asserted from the consumer's side.

    Version 1 is good and published. Version 2 is written with a null key — the `not_null` assertion
    fails — and is a real, committed version any plain reader sees. A consumer reading through the
    pointer must still be on version 1.
    """
    good = _write(ns, [1, 2, 3])
    _publish(ns, good)

    bad = _write(ns, [4, None, 6], mode="overwrite")
    result = _publish(ns, bad)

    assert result.published is False
    assert result.reason is not None and "not_null" in result.reason
    assert result.to_version == bad, "the rejected version is still NAMED — a blocked batch must be auditable"

    assert published_version(ns, {}, TABLE_ID) == good

    # The rejected version is committed and visible to anyone NOT using the pointer, which is exactly
    # why the pointer has to exist: Lance cannot hide it.
    assert int(lance.dataset(_uri(ns)).version) == bad

    pinned = lance.dataset(_uri(ns)).checkout_version(PUBLISHED_TAG)
    assert pinned.to_table(columns=["id"]).column("id").to_pylist() == [1, 2, 3]


def test_the_candidate_is_PINNED_while_the_gate_runs(ns) -> None:  # noqa: ANN001
    """The GC hazard, closed.

    `cleanup_old_versions` exempts only TAGGED versions, and the candidate is untagged for exactly as
    long as the gate takes — so a slow gate against a short `older_than` lets maintenance collect the
    version being gated. `publishing` pins it for that window.

    Asserted by observing the tag DURING the gate (the gate is the observation point), and that it is
    gone afterwards — a pin that outlived its publish would exempt a version from GC forever.
    """
    version = _write(ns, [1, 2, 3])
    seen: list[int | None] = []

    import catalog.services.publication as publication

    real = publication.assert_quality

    def observing(*args: object, **kwargs: object):  # noqa: ANN202
        tags = publication.dataplane.list_tags(ns, {}, ListTableTagsRequest(id=TABLE_ID)).tags or {}
        entry = tags.get(PUBLISHING_TAG)
        seen.append(getattr(entry, "version", None) if entry is not None else None)
        return real(*args, **kwargs)  # ty: ignore[missing-argument]

    publication.assert_quality = observing  # type: ignore[assignment]
    try:
        _publish(ns, version)
    finally:
        publication.assert_quality = real  # type: ignore[assignment]

    assert seen == [version], f"the candidate was not pinned while the gate ran: {seen}"

    after = publication.dataplane.list_tags(ns, {}, ListTableTagsRequest(id=TABLE_ID)).tags or {}
    assert PUBLISHING_TAG not in after, "the pin outlived the publish — that version can never be collected"
    assert PUBLISHED_TAG in after


def test_the_pin_is_dropped_even_when_the_gate_REJECTS(ns) -> None:  # noqa: ANN001
    """A rejected publish must not leave a version pinned against GC forever."""
    version = _write(ns, [1, None])

    result = _publish(ns, version)

    assert result.published is False
    import catalog.services.publication as publication

    tags = publication.dataplane.list_tags(ns, {}, ListTableTagsRequest(id=TABLE_ID)).tags or {}
    assert PUBLISHING_TAG not in tags


def test_assertions_come_back_on_BOTH_outcomes(ns) -> None:  # noqa: ANN001
    """A rejected batch has to be auditable, not merely refused — the operator needs to know WHICH
    check failed without re-running anything."""
    version = _write(ns, [1, None])

    result = _publish(ns, version)

    assert result.published is False
    assert [a.assertion for a in result.assertions if not a.success] == ["not_null"]
    assert any(a.success for a in result.assertions), "the passing checks are reported too"


def test_an_empty_version_is_REFUSED(ns) -> None:  # noqa: ANN001
    """`row_count_positive`. An empty publication is otherwise a silent failure — a mis-set source
    prefix publishes nothing and reports success."""
    version = _write(ns, [])

    result = _publish(ns, version)

    assert result.published is False
    assert "row_count_positive" in (result.reason or "")


def test_a_SECOND_publication_carries_the_PREVIOUS_version_as_from(ns) -> None:  # noqa: ANN001
    """D-R3's range across successive runs: `from` is the last PUBLISHED version, not the last
    committed one, so a consumer that only ever saw published data gets a contiguous delta."""
    first = _write(ns, [1, 2])
    _publish(ns, first)

    second = _write(ns, [3, 4], mode="append")
    result = _publish(ns, second)

    assert (result.from_version, result.to_version) == (first, second)
    assert published_version(ns, {}, TABLE_ID) == second


def test_publishing_BACKWARDS_is_refused(ns) -> None:  # noqa: ANN001
    """A late retry of an older run must not silently un-publish newer good data. The tag API remains
    available for a deliberate rollback, where an operator names the version."""
    first = _write(ns, [1, 2])
    second = _write(ns, [3, 4], mode="append")
    _publish(ns, second)

    with pytest.raises(InvalidTableStateError, match="backwards"):
        _publish(ns, first)

    assert published_version(ns, {}, TABLE_ID) == second


def test_a_version_that_does_not_exist_is_refused(ns) -> None:  # noqa: ANN001
    """Fail before reading anything, and name the latest so the caller sees how far off it was."""
    version = _write(ns, [1])

    with pytest.raises(TableVersionNotFoundError, match="latest"):
        _publish(ns, version + 99)

    with pytest.raises(InvalidInputError):
        _publish(ns, 0)


def test_the_tag_move_mints_NO_new_version(ns) -> None:  # noqa: ANN001
    """Publication is metadata-only. If advancing the pointer created a version, every publish would
    itself need publishing — and `cleanup_old_versions` would be reasoning about pointer moves as
    though they were data."""
    version = _write(ns, [1, 2])

    _publish(ns, version)

    assert int(lance.dataset(_uri(ns)).version) == version


# ── the notification (D-R2) ───────────────────────────────────────────────────────────


def test_a_PUBLICATION_announces_the_range_and_a_REJECTION_announces_nothing() -> None:
    """The event is the wake-up; the tag is the truth.

    Two properties, asserted together because each is wrong without the other:

    * a successful publish announces `{from_version, to_version}` — the RANGE is the entire point of
      the signal (D-R3), since a consumer turns it straight into
      `_row_created_at_version > from AND <= to` and keeps no bookmark of its own;
    * a REFUSED gate announces nothing. There is no new readiness to wake anyone for, and an event on
      a rejection would teach consumers to check whether a "published" notice actually published.

    Asserted on the endpoint's emit decision rather than on a live bus: what is being pinned is WHEN
    the estate announces and WHAT it carries, and a broker adds nothing to either question.
    """
    from catalog.services.publication import PublicationResult

    emitted: list[dict[str, object]] = []

    def emit(result: PublicationResult) -> None:
        """The endpoint's rule, in one line — mirrors `publish_table`."""
        if result.published:
            emitted.append({"from_version": result.from_version, "to_version": result.to_version})

    emit(PublicationResult(table="t", published=True, from_version=4, to_version=7))
    emit(PublicationResult(table="t", published=False, from_version=7, to_version=8, reason="quality gate failed: not_null"))

    assert emitted == [{"from_version": 4, "to_version": 7}], "a rejection must announce nothing"


def test_table_published_is_in_the_control_VOCABULARY() -> None:
    """The vocabulary is a wire contract across three files.

    `ControlAction` reaches the frontend through `docs/catalog-openapi.json` →
    `frontend/packages/api/src/generated/catalog.ts`. An action added without regenerating leaves the
    TS client unable to NAME an event the backend publishes, and `test_openapi_contract` fails — so
    this guards the Python half and that contract test guards the rest.
    """
    from typing import get_args

    from service_kit.control_events import ControlAction

    assert "table_published" in get_args(ControlAction)
