"""A committed version whose announcement was lost reaches the feed within one reconcile tick, exactly once ([[LH-497]]).

The catalog announces a write after its commit, and a crash between the commit and the staging of its event loses the
announcement while the version stays committed. Here the loss is real rather than simulated: the catalog announces on
a bus that refuses every publish while it is away, and keeps no outbox, so each write's own announcement fails and
nothing is staged. One tick of the catalog's reconcile then finds each version from the table's version log and announces it,
under the run id derived from the commit, so the write's own announcement, delivered late, lands no second row.

A re-announcement is signed only for the author the catalog recorded authorizing the commit ([[LH-550]]), the caller its
door authenticated, so it names the author the write's own announcement names. A version committed past every door names
whoever its writer stamped as ``rask.author``, and can carry the transaction uuid of a commit the catalog did record
(measured on pylance 13.0.0, ``LanceDataset.commit`` keeps the uuid its transaction is given). The record names the
version its commit landed at, so the catalog recorded nobody for this one: it is counted unauthored, which pages, and
announced for nobody. So is a write through a door whose author record the store refused once it landed, logged at ERROR
and counted, and a version whose transaction cannot be read, which the format lets a manifest carry
(``lance_docs/lance/format/table/transaction.md`` § Inline Transaction Sections) and no catalog commit leaves.

A registered table's versions before its register were committed past every door, so none is the catalog's to announce or
count, whatever ``rask.author`` it carries: the register's own marker is its announcement, and only a commit made through
the catalog after the register is looked for. Here the history is a stock create, an append naming another estate's person
and an append on a branch, each of which would be counted unauthored or announced if it were looked for.

Driven over real sockets against the real catalog and lineage apps on a real ``dir`` namespace, as
`test_the_commit_feed_is_a_lance_table_followed_forward.py` drives them: the claim is about what a pipeline following
``/events?since=`` reads. The catalog authenticates every caller, alice through the loopback Dex and lineage's catalog
calls through the loopback service-account issuer, as a deployment's does; lineage takes each event from its own consumer and
verifies no signature, since this estate names no signer. What stands in is the bus the two share
(`lineage_app.joined_bus`), which keeps every publish it took or refused, the paging counters, recorded by doubles of
their instruments, and two faults, each injected for one step: the store refusing one author record, and
one version's transaction reading as none.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from lance.fragment import write_fragments
from lance.namespace import DirectoryNamespace
from lance_namespace import DescribeTableRequest, DescribeTableResponse, InternalError, PermissionDeniedError

from lakehouse.events.bus_delivery import Outcome
from lakehouse.lancekit import manifest_wire
from lakehouse.lancekit.listing_floor import FLOOR_KEY
from lakehouse.lancekit.manifest_names import committed_version
from tests.integration.lineage_app import catalog_tick, joined_bus, live_catalog, live_lineage
from tests.traces import trace_id_of


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}


def _rows(start: int, stop: int) -> bytes:
    table = pa.table({"id": pa.array(range(start, stop), pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _follow(client: httpx.Client) -> list[dict[str, Any]]:
    """Walk the feed the way a pipeline does: from the last cursor until a poll brings nothing new."""
    seen: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(20):
        page = client.get("/events", params={"since": cursor}).json()
        seen += page["events"]
        if not page["events"] and page["next_cursor"] == cursor:
            return seen
        cursor = page["next_cursor"]
    raise AssertionError(f"the walk did not settle: {seen}")


def _commits(rows: list[dict[str, Any]], table: str) -> list[tuple[str, str, str, str | None]]:
    """``(table, operation, version, author)`` of each feed row announcing a write of ``table``."""
    found = []
    for row in rows:
        event: Mapping[str, Any] = row["event"]
        for output in event.get("outputs") or []:
            if output["name"] == table:
                facets = event["run"]["facets"]
                author = (facets.get("rask_author") or {}).get("sub")
                found.append((output["name"], facets["rask_lance"]["operation"], output["facets"]["version"]["datasetVersion"], author))
    return found


#: The caller's own trace, as W3C Trace Context's example writes one.
_TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
_TRACED: Final = {"traceparent": f"00-{_TRACE}-00f067aa0ba902b7-01"}


def _stamped_behind(location: str, version: int, behind: timedelta) -> None:
    """Write ``version``'s manifest again stamped ``behind`` its commit time, every other byte kept: what a writer whose
    clock runs that far behind commits (the time is the writer's own, ``Manifest.timestamp``, field 7). The edit keeps the
    seconds' varint at its length, so no offset in the file moves."""
    versions = Path(location.removeprefix("file://")) / "_versions"
    manifest = next(path for path in versions.iterdir() if committed_version(path.name) == version)
    data = bytearray(manifest.read_bytes())
    (offset,) = struct.unpack_from("<Q", data, len(data) - 16)
    at, end = offset + 4, offset + 4 + struct.unpack_from("<I", data, offset)[0]
    while at < end:
        number, value, after = manifest_wire.next_field(bytes(data[:end]), at)
        if number == 7 and isinstance(value, bytes):
            inner = after - len(value)
            seconds_at = inner + 1
            seconds, seconds_end = manifest_wire.read_varint(bytes(data), seconds_at)
            moved = manifest_wire.encode_varint(seconds - int(behind.total_seconds()))
            assert len(moved) == seconds_end - seconds_at, "the moved stamp changes the manifest's length"
            data[seconds_at:seconds_end] = moved
            manifest.write_bytes(bytes(data))
            return
        at = after
    raise AssertionError(f"version {version}'s manifest records no commit time")


class _Counter:
    """The paging counter's instrument, recording what it is handed."""

    def __init__(self) -> None:
        self.added: list[tuple[int, Mapping[str, str] | None]] = []

    def add(self, amount: int, attributes: Mapping[str, str] | None = None) -> None:
        self.added.append((amount, attributes))


def test_a_lost_announcement_is_on_the_feed_after_one_tick_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, dex_issuer: Any, sa_issuer: Any
) -> None:
    from catalog.api import commit_reconcile
    from catalog.core.config import get_settings
    from catalog.services import commit_authors
    from lakehouse.records import commit_records

    counted, unrecorded = _Counter(), _Counter()
    monkeypatch.setattr(commit_reconcile, "_reannounced", counted)
    monkeypatch.setattr(commit_authors, "_unrecorded", unrecorded)
    alice = {"Authorization": f"Bearer {dex_issuer.mint('alice', connector='local', audience='lance-catalog')}"}
    bus = joined_bus(monkeypatch)
    bus.away = True
    env = {
        "LANCE_COMMIT_RECONCILE_GRACE_SECONDS": "0",
        # The reconciler runs, so doors file their commits; its own timer claims one window at the start and no other.
        "LANCE_COMMIT_RECONCILE_PERIOD_SECONDS": "86400",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_IDP_ID": "dex",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
        "RASK_SA_AUDIENCE": "rask-catalog",
    }
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **env) as catalog_url,
        httpx.Client(base_url=catalog_url, headers=alice, timeout=60) as catalog,
    ):

        def reconcile() -> commit_reconcile.CommitReconcileTick:
            return catalog_tick(catalog_url, commit_reconcile.tick)

        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        # The bus is away, so each write's own announcement is lost after its commit. Each write is made in the
        # caller's own trace, which its commit records as `rask.traceparent` (ADR 0209).
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW | _TRACED).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/insert", content=_rows(3, 5), headers=_ARROW | _TRACED).status_code == 200
        # A delete commits detached and is finalized by the catalog, where the create and the insert commit through Lance.
        assert catalog.post("/v1/table/lance_catalog$db$t/delete", json={"predicate": "id = 0"}, headers=_TRACED).status_code == 200
        # A table only declared holds no version: nothing to announce, and nothing the tick cannot read.
        assert catalog.post("/v1/table/lance_catalog$db$declared/declare", json={}).status_code == 200
        originals = [refused for refused in bus.refused if "run" in refused.event and refused.event["outputs"][0]["name"] == "lance_catalog$db$t"]
        assert len(originals) == 3, bus.refused
        # The caller the catalog authenticated, as each write's own announcement names it.
        author = originals[0].event["run"]["facets"]["rask_author"]["sub"]

        # Lineage reaches the catalog under its own service account, and itself authenticates nobody.
        lineage_token = tmp_path / "lineage-catalog-token"
        lineage_token.write_text(sa_issuer.mint("rask-sa-lineage", audience="rask-catalog"))
        monkeypatch.setenv("RASK_CATALOG_IDENTITY_TOKEN_FILE", str(lineage_token))
        monkeypatch.setenv("RASK_OIDC_ENABLED", "false")
        with live_lineage(catalog_url, monkeypatch) as lineage_url, httpx.Client(base_url=lineage_url, timeout=60) as lineage:
            bus.away = False
            assert _commits(_follow(lineage), "lance_catalog$db$t") == [], "the writes' own announcements reached the feed, so none was lost"

            # The feed, then the table, described while the store does not answer: the tick announces nothing, counts the
            # failure and logs its cause, and never reads an unanswered describe as a feed or a table that is not there.
            # A table its warehouse's deactivation quarantines (the resolver's PermissionDenied) is no live table: the
            # tick neither announces it nor counts it as a read it could not make.
            real_describe = DirectoryNamespace.describe_table

            def failing(described: list[str], error: Exception) -> Callable[[DirectoryNamespace, DescribeTableRequest], DescribeTableResponse]:
                def describe(ns: DirectoryNamespace, request: DescribeTableRequest) -> DescribeTableResponse:
                    if list(request.id or []) == described:
                        raise error
                    return real_describe(ns, request)

                return describe

            outage = InternalError('IO { source: Generic { store: "S3", source: RetryError { inner: Status { status: 503 } } } }')
            unannounced: list[commit_reconcile.CommitReconcileTick] = []
            for described, error, events in (
                (["lance_catalog", "provenance", "events"], outage, ["commit_reconcile_feed_unreadable"]),
                (["lance_catalog", "db", "t"], outage, ["commit_reconcile_table_unreadable"]),
                (["lance_catalog", "db", "t"], PermissionDeniedError("warehouse 'wh' is deactivated"), []),
            ):
                monkeypatch.setattr(DirectoryNamespace, "describe_table", failing(described, error))
                with caplog.at_level(logging.ERROR, logger="catalog.api.commit_reconcile"):
                    unannounced.append(reconcile())
                monkeypatch.setattr(DirectoryNamespace, "describe_table", real_describe)
                logged = [
                    (record.getMessage(), "503" in str(getattr(record, "error", "")))
                    for record in caplog.records
                    if record.name == "catalog.api.commit_reconcile"
                ]
                assert logged == [(event, True) for event in events], logged
                caplog.clear()
            assert [(tick.skipped, tick.reannounced, tick.unreadable) for tick in unannounced] == [(True, 0, 1), (False, 0, 1), (False, 0, 0)], unannounced

            # A retained version the store fails to open once: the tick keeps the ref's mark, and skips nothing for it.
            real_checkout = lance.LanceDataset.checkout_version

            def unreadable_once(dataset: lance.LanceDataset, version: int | str | tuple[str | None, int | None]) -> lance.LanceDataset:
                if version == 1 and str(dataset.uri).endswith(location_of_t):
                    monkeypatch.setattr(lance.LanceDataset, "checkout_version", real_checkout)
                    raise OSError("the store did not answer")
                return real_checkout(dataset, version)

            location_of_t = str(catalog.post("/v1/table/lance_catalog$db$t/describe", json={}).json()["location"]).rstrip("/").rsplit("/", 1)[-1]
            monkeypatch.setattr(lance.LanceDataset, "checkout_version", unreadable_once)
            blocked = reconcile()
            assert (blocked.reannounced, blocked.unreadable) == (0, 1), blocked

            tick = reconcile()
            assert tick.reannounced == 3, tick
            # Each re-announcement is made in the trace of the request whose commit it announces, not the tick's own.
            reannounced = [
                sent.envelope.get("traceparent") for sent in bus.sent if "run" in sent.event and sent.event["outputs"][0]["name"] == "lance_catalog$db$t"
            ]
            assert [trace_id_of(traceparent) if traceparent else None for traceparent in reannounced] == [_TRACE] * 3
            assert _commits(_follow(lineage), "lance_catalog$db$t") == [
                ("lance_catalog$db$t", "create_table", "1", author),
                ("lance_catalog$db$t", "insert", "2", author),
                ("lance_catalog$db$t", "delete", "3", author),
            ]

            # Each write's own announcement arriving late, as the outbox relay delivers one it had staged; the tick that
            # then finds them on the feed is refused one delete of a version's author record.
            for original in originals:
                assert bus.hand_late(original).outcome is Outcome.TAKEN
            real_forget = commit_records.forget_author

            def refused_for_version_2(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — the delete's own arguments
                if kwargs["version"] == 2:
                    raise OSError("the store refused the delete")
                real_forget(*args, **kwargs)

            monkeypatch.setattr(commit_records, "forget_author", refused_for_version_2)
            again = reconcile()
            monkeypatch.setattr(commit_records, "forget_author", real_forget)
            assert (again.reannounced, again.unreadable) == (0, 0), again
            # The record the store kept holds main's mark below its version, so the next tick reads it and forgets it.
            settings = get_settings()
            control, control_so = settings.registry_root, settings.storage_options()
            uuid_of_t = catalog.post("/v1/table/lance_catalog$db$t/describe", json={}).json()["properties"]["rask.uuid"]
            second = lance.dataset(catalog.post("/v1/table/lance_catalog$db$t/describe", json={"vend_credentials": False}).json()["location"]).read_transaction(
                2
            )
            assert second is not None

            def recorded_second() -> str | None:
                record = commit_records.recorded(control, control_so, uuid_of_t, transaction_uuid=second.uuid, version=2, ref="")
                return record.author if record is not None else None

            assert (commit_records.read_marks(control, control_so, uuid_of_t).refs, recorded_second()) == ({"": 1}, author)
            assert _commits(_follow(lineage), "lance_catalog$db$t") == [
                ("lance_catalog$db$t", "create_table", "1", author),
                ("lance_catalog$db$t", "insert", "2", author),
                ("lance_catalog$db$t", "delete", "3", author),
            ]

            # A write through a door whose author record the store refuses, its announcement lost too: the write lands,
            # and the miss is logged at ERROR and counted.
            real_record = commit_records.record_author

            def refused(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — the record's own arguments
                raise OSError("the store refused the write")

            bus.away = True
            monkeypatch.setattr(commit_records, "record_author", refused)
            with caplog.at_level(logging.ERROR, logger="catalog.services.commit_authors"):
                assert catalog.post("/v1/table/lance_catalog$db$t/insert", content=_rows(5, 6), headers=_ARROW).status_code == 200
            monkeypatch.setattr(commit_records, "record_author", real_record)
            bus.away = False
            missed = [(record.levelno, record.getMessage()) for record in caplog.records if record.name == "catalog.services.commit_authors"]
            assert missed == [(logging.ERROR, "commit_author_unrecorded")]

            # A write through a door, announced, then two versions committed straight to the store, past every door: one
            # carries the write's transaction uuid and stamps another estate's person, and the write's record names the
            # version it landed at; the other's transaction cannot be read, which no catalog commit leaves.
            assert catalog.post("/v1/table/lance_catalog$db$t/insert", content=_rows(6, 7), headers=_ARROW).status_code == 200
            location = catalog.post("/v1/table/lance_catalog$db$t/describe", json={"vend_credentials": False}).json()["location"]
            written = lance.dataset(location)
            reused = written.read_transaction(written.version)
            assert reused is not None
            planned = write_fragments(pa.table({"id": pa.array([7], pa.int64())}), location, return_transaction=True, data_storage_version="2.2")
            stamped = replace(planned, read_version=written.version, uuid=reused.uuid, transaction_properties={"rask.author": "dex~mallory"})
            forged = lance.LanceDataset.commit(location, stamped)
            carried = forged.read_transaction(forged.version)
            assert carried is not None and carried.uuid == reused.uuid
            unreadable = lance.write_dataset(pa.table({"id": pa.array([8], pa.int64())}), location, mode="append").version
            real_read = lance.LanceDataset.read_transaction

            def unreadable_transaction(dataset: lance.LanceDataset, version: int) -> Any:  # noqa: ANN401 — a lance Transaction
                if version == unreadable and str(dataset.uri).rstrip("/").endswith(location_of_t):
                    return None
                return real_read(dataset, version)

            monkeypatch.setattr(lance.LanceDataset, "read_transaction", unreadable_transaction)
            past_every_door = reconcile()
            monkeypatch.setattr(lance.LanceDataset, "read_transaction", real_read)
            assert (past_every_door.reannounced, past_every_door.unauthored) == (0, 3), past_every_door
            assert recorded_second() is None
            assert _commits(_follow(lineage), "lance_catalog$db$t") == [
                ("lance_catalog$db$t", "create_table", "1", author),
                ("lance_catalog$db$t", "insert", "2", author),
                ("lance_catalog$db$t", "delete", "3", author),
                ("lance_catalog$db$t", "insert", "5", author),
            ]

            # Config commits ([[LH-575]]): a floor raise through its door, its author recorded and announced on no feed;
            # a floor stamp set straight at the store, past every door, counted; and a disarm, the commit every door
            # makes before its own, neither.
            raised = catalog.post("/management/v1/table/lance_catalog$db$t/maintenance/floor", json={})
            assert (raised.status_code, raised.json()["version"] is not None) == (200, True), raised.text
            lance.dataset(location).update_config({FLOOR_KEY: "1"})
            lance.dataset(location).delete_config_keys(["lance.auto_cleanup.interval"])
            configs = reconcile()
            assert (configs.reannounced, configs.unauthored) == (0, 1), configs
            assert len(_commits(_follow(lineage), "lance_catalog$db$t")) == 4

            # A dataset written outside the catalog, registered, then written through the catalog while the bus is
            # away again: only that last write's announcement is the catalog's, and lost.
            bus.away = True
            outside = str(tmp_path / "catalog" / "outside" / "registered")
            stock = lance.write_dataset(pa.table({"id": pa.array([0, 1], pa.int64())}), outside, data_storage_version="2.2", enable_stable_row_ids=True)
            stock = lance.write_dataset(
                pa.table({"id": pa.array([2], pa.int64())}), stock, mode="append", transaction_properties={"rask.author": "dex~mallory"}
            )
            stock.create_branch("work")
            lance.dataset(outside).checkout_version(("work", None)).insert(pa.table({"id": pa.array([3], pa.int64())}))
            registered = catalog.post("/v1/table/lance_catalog$db$registered/register", json={"location": "outside/registered"})
            assert registered.status_code == 200, registered.text
            assert catalog.post("/v1/table/lance_catalog$db$registered/insert", content=_rows(4, 5), headers=_ARROW).status_code == 200
            bus.away = False

            after_register = reconcile()
            assert (after_register.reannounced, after_register.unauthored, after_register.unreadable) == (1, 0, 0), after_register
            assert _commits(_follow(lineage), "lance_catalog$db$registered") == [("lance_catalog$db$registered", "insert", "3", author)]
            # Lineage recorded every event above in the provenance namespace through the catalog's doors, and none of
            # those writes is announced, so none keeps an author record.
            uuid_of_registered = catalog.post("/v1/table/lance_catalog$db$registered/describe", json={}).json()["properties"]["rask.uuid"]
            assert {path.name for path in (Path(control) / commit_records.AUTHORS_PREFIX).iterdir()} == {uuid_of_t, uuid_of_registered}

            # A write whose writer's clock runs two days behind, past the horizon, its announcement lost: a version's age
            # is the store's stamp on its manifest, never the time its writer wrote into it, so it is looked for and
            # announced rather than taken as one the feed has long since dropped.
            assert catalog.post("/v1/table/lance_catalog$db$late/create", content=_rows(0, 1), headers=_ARROW).status_code == 200
            bus.away = True
            assert catalog.post("/v1/table/lance_catalog$db$late/insert", content=_rows(1, 2), headers=_ARROW).status_code == 200
            bus.away = False
            _stamped_behind(catalog.post("/v1/table/lance_catalog$db$late/describe", json={"vend_credentials": False}).json()["location"], 2, timedelta(days=2))
            assert reconcile().reannounced == 1
            assert _commits(_follow(lineage), "lance_catalog$db$late") == [
                ("lance_catalog$db$late", "create_table", "1", author),
                ("lance_catalog$db$late", "insert", "2", author),
            ]

    assert [amount for amount, _ in counted.added] == [1, 1, 1, 1, 1]
    # The create, both inserts the catalog recorded, the refused one, the registered table's insert, and the late table's
    # create and insert.
    assert [amount for amount, _ in unrecorded.added] == [0, 0, 1, 0, 0, 0, 0]
    assert [dict(attributes or {}) for _, attributes in counted.added] == [
        {"lance.catalog.operation": "create_table"},
        {"lance.catalog.operation": "insert"},
        {"lance.catalog.operation": "delete"},
        {"lance.catalog.operation": "insert"},
        {"lance.catalog.operation": "insert"},
    ]


@contextmanager
def _governed_estate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any, *, reconcile_period: str = "86400"
) -> Generator[tuple[httpx.Client, str]]:
    """A catalog alice is signed in to and the lineage that keeps its feed, the bus between them joined: the catalog's
    client and its url. The reconciler looks for a version at once (no grace)."""
    alice = {"Authorization": f"Bearer {dex_issuer.mint('alice', connector='local', audience='lance-catalog')}"}
    joined_bus(monkeypatch)
    env = {
        "LANCE_COMMIT_RECONCILE_GRACE_SECONDS": "0",
        # The reconciler runs, so doors file their commits; its own timer claims one window at the start and no other.
        "LANCE_COMMIT_RECONCILE_PERIOD_SECONDS": reconcile_period,
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_IDP_ID": "dex",
        "RASK_OIDC_CONNECTORS": json.dumps({"local": "dex"}),
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
        "RASK_SA_AUDIENCE": "rask-catalog",
    }
    with live_catalog(tmp_path / "catalog", monkeypatch, **env) as catalog_url, httpx.Client(base_url=catalog_url, headers=alice, timeout=60) as catalog:
        lineage_token = tmp_path / "lineage-catalog-token"
        lineage_token.write_text(sa_issuer.mint("rask-sa-lineage", audience="rask-catalog"))
        monkeypatch.setenv("RASK_CATALOG_IDENTITY_TOKEN_FILE", str(lineage_token))
        monkeypatch.setenv("RASK_OIDC_ENABLED", "false")
        with live_lineage(catalog_url, monkeypatch):
            yield catalog, catalog_url


def _pending_entries() -> list[Path]:
    """Every entry a door filed for a commit it was about to make, on the catalog's control root."""
    from catalog.core.config import get_settings
    from lakehouse.records import commit_records

    return sorted((Path(get_settings().registry_root.removeprefix("file://")) / commit_records.PENDING_PREFIX).rglob("*.json"))


def _lapse_every_pending_entry() -> None:
    """Stamp every filed entry older than a door's lease, as the store's clock would read it once the door has stopped."""
    from lakehouse.records import location_claims

    lapsed = (datetime.now(UTC) - location_claims.LEASE - timedelta(minutes=1)).timestamp()
    for entry in _pending_entries():
        os.utime(entry, (lapsed, lapsed))


def _filed_for() -> set[str]:
    """The uuid of every table an entry is filed for."""
    return {entry.parent.name for entry in _pending_entries()}


def _uuid_of(catalog: httpx.Client, table: str) -> str:
    return catalog.post(f"/v1/table/lance_catalog$db${table}/describe", json={}).json()["properties"]["rask.uuid"]


@contextmanager
def _a_store_that_does_not_describe(monkeypatch: pytest.MonkeyPatch, table: str) -> Generator[None]:
    """While the block runs, the describe of ``lance_catalog$db$<table>`` fails as a store that does not answer."""
    real_describe = DirectoryNamespace.describe_table

    def describe(ns: DirectoryNamespace, request: DescribeTableRequest) -> DescribeTableResponse:
        if list(request.id or []) == ["lance_catalog", "db", table]:
            raise InternalError('IO { source: Generic { store: "S3", source: RetryError { inner: Status { status: 503 } } } }')
        return real_describe(ns, request)

    monkeypatch.setattr(DirectoryNamespace, "describe_table", describe)
    try:
        yield
    finally:
        monkeypatch.setattr(DirectoryNamespace, "describe_table", real_describe)


def test_a_table_no_door_committed_to_since_its_entries_lapsed_is_not_read_by_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any
) -> None:
    from catalog.api import commit_reconcile

    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer) as (catalog, catalog_url):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
        filed = _filed_for()
        _lapse_every_pending_entry()
        read = catalog_tick(catalog_url, commit_reconcile.tick)

        idle = catalog_tick(catalog_url, commit_reconcile.tick)

        assert (_uuid_of(catalog, "t") in filed, read.tables, idle.tables, _pending_entries()) == (True, len(filed), 0, [])


def test_a_table_the_tick_could_not_read_keeps_its_entries_and_the_tables_it_read_lose_theirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any
) -> None:
    from catalog.api import commit_reconcile

    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer) as (catalog, catalog_url):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        for table in ("t", "u"):
            assert catalog.post(f"/v1/table/lance_catalog$db${table}/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
        unread = _uuid_of(catalog, "u")
        _lapse_every_pending_entry()

        with _a_store_that_does_not_describe(monkeypatch, "u"):
            ticked = catalog_tick(catalog_url, commit_reconcile.tick)

        assert (ticked.unreadable, _filed_for()) == (1, {unread})


def test_a_table_the_walk_could_not_read_is_filed_for_the_next_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any) -> None:
    from catalog.api import commit_reconcile

    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer) as (catalog, catalog_url):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
        unread = _uuid_of(catalog, "t")
        _lapse_every_pending_entry()
        catalog_tick(catalog_url, commit_reconcile.tick)

        with _a_store_that_does_not_describe(monkeypatch, "t"):
            walked = catalog_tick(catalog_url, commit_reconcile.walk)

        assert (walked.unreadable, _filed_for()) == (1, {unread})


def test_the_entries_of_a_table_no_claim_records_any_more_are_removed_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any
) -> None:
    from catalog.api import commit_reconcile
    from catalog.core.config import get_settings
    from lakehouse.lancekit.base_refs import decoded_path
    from lakehouse.records import location_claims

    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer) as (catalog, catalog_url):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
        # The claim on the table's location is gone, as it is once a dropped table is purged.
        location = catalog.post("/v1/table/lance_catalog$db$t/describe", json={"vend_credentials": False}).json()["location"]
        (Path(get_settings().registry_root.removeprefix("file://")) / location_claims.claim_key(decoded_path(location))).unlink()
        _lapse_every_pending_entry()

        catalog_tick(catalog_url, commit_reconcile.tick)

        assert _pending_entries() == []


def test_a_version_committed_past_every_door_is_counted_by_the_walk_and_never_read_by_the_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any
) -> None:
    from catalog.api import commit_reconcile

    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer) as (catalog, catalog_url):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
        _lapse_every_pending_entry()
        catalog_tick(catalog_url, commit_reconcile.tick)
        location = catalog.post("/v1/table/lance_catalog$db$t/describe", json={"vend_credentials": False}).json()["location"]
        lance.dataset(location).insert(pa.table({"id": pa.array([9], pa.int64())}))
        ticked = catalog_tick(catalog_url, commit_reconcile.tick)

        walked = catalog_tick(catalog_url, commit_reconcile.walk)

        assert (ticked.tables, ticked.unauthored, walked.unauthored) == (0, 0, 1)


@pytest.mark.parametrize(
    ("door", "request_of"),
    [
        pytest.param("insert", {"content": _rows(3, 5), "headers": _ARROW}, id="a-commit-lance-makes-in-one-call"),
        pytest.param("delete", {"json": {"predicate": "id = 0"}}, id="a-commit-the-catalog-finalizes"),
    ],
)
def test_a_doors_commit_files_its_table_for_the_next_tick(
    door: str, request_of: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any
) -> None:
    from catalog.api import commit_reconcile

    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer) as (catalog, catalog_url):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
        _lapse_every_pending_entry()
        catalog_tick(catalog_url, commit_reconcile.tick)
        uuid_of_t = _uuid_of(catalog, "t")

        written = catalog.post(f"/v1/table/lance_catalog$db$t/{door}", **request_of)

        assert (written.status_code, _filed_for()) == (200, {uuid_of_t})


def _files_beside_the_entries(root: Path) -> set[str]:
    """Every file under the catalog's root but the entries themselves, the namespace index a refused create's rollback
    rewrites, and the location claims, where any create whose first write fails leaves the record of the location it
    minted."""
    apart = {"_commit_pending", "__manifest", "_locations"}
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and not apart & set(path.parts)}


@pytest.mark.parametrize(
    ("table", "door", "request_of"),
    [
        pytest.param("t", "delete", {"json": {"predicate": "id = 0"}}, id="a-commit-the-catalog-finalizes"),
        pytest.param("t", "insert", {"content": _rows(3, 5), "headers": _ARROW}, id="a-commit-lance-makes-in-one-call"),
        pytest.param("new", "create", {"content": _rows(0, 3), "headers": _ARROW}, id="a-create"),
    ],
)
def test_a_door_that_cannot_file_its_commit_makes_none_and_leaves_nothing(
    table: str, door: str, request_of: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any
) -> None:
    from catalog.api import commit_reconcile
    from catalog.core.config import get_settings
    from lakehouse.records import commit_records

    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer) as (catalog, catalog_url):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
        _lapse_every_pending_entry()
        catalog_tick(catalog_url, commit_reconcile.tick)
        root = Path(get_settings().registry_root.removeprefix("file://"))
        # A store that refuses the entry: a file where the entries' directory goes.
        pending = root / commit_records.PENDING_PREFIX
        for leftover in sorted(pending.rglob("*"), reverse=True):
            leftover.rmdir()
        pending.rmdir()
        pending.write_text("")
        before = _files_beside_the_entries(root)

        answered = catalog.post(f"/v1/table/lance_catalog$db${table}/{door}", **request_of)

        assert (answered.status_code, _files_beside_the_entries(root) - before) == (503, set()), answered.text


def test_a_catalog_that_runs_no_reconciler_files_no_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dex_issuer: Any, sa_issuer: Any) -> None:
    with _governed_estate(tmp_path, monkeypatch, dex_issuer, sa_issuer, reconcile_period="0") as (catalog, _):
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post("/v1/table/lance_catalog$db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200

        deleted = catalog.post("/v1/table/lance_catalog$db$t/delete", json={"predicate": "id = 0"})

        assert (deleted.status_code, _pending_entries()) == (200, [])
