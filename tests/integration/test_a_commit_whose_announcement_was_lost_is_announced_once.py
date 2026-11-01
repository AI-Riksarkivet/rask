"""A committed version whose announcement was lost reaches the feed within one reconcile tick, exactly once ([[LH-497]]).

The catalog announces a write after its commit, and a crash between the commit and the staging of its event loses the
announcement while the version stays committed. Here the loss is real rather than simulated: the catalog announces over
HTTP through a gate that refuses every event while it is down, so each write's own announcement fails and nothing is
staged. One tick of the catalog's reconcile cron then finds each version from the table's version log and announces it,
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

A publication commits no version, so the tick reads it off the table's ``published`` tag ([[LH-552]]): one whose
announcement was lost is announced for the caller the catalog recorded moving the tag, under the run id derived from the
move, so the original arriving late lands no second row. A ``published`` moved straight at the store names no mover the
catalog recorded: it is counted unauthored once and announced for nobody. A version published again after an erasure
dropped the tag is a move of its own, announced as a second publication.

Driven over real sockets against the real catalog and lineage apps on a real ``dir`` namespace, as
`test_the_commit_feed_is_a_lance_table_followed_forward.py` drives them: the claim is about what a pipeline following
``/events?since=`` reads. The catalog authenticates every caller, alice through the loopback Dex and lineage's catalog
calls through the loopback service-account issuer, as a deployment's does; lineage itself authenticates nobody, since the
gate forwards bare events. What stands in is the gate, a recording hop in front of lineage, the paging counters, recorded
by doubles of their instruments, and two faults, each injected for one step: the store refusing one author record, and
one version's transaction reading as none.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi import Body, FastAPI, Response
from lance.fragment import write_fragments

from tests.integration.lineage_app import free_port, live_catalog, live_lineage, serve


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
_TICK = "catalog-commit-reconcile-cron"


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
            if output["name"] == table and event["run"]["facets"]["rask_lance"]["operation"] != "publish_table":
                facets = event["run"]["facets"]
                author = (facets.get("rask_author") or {}).get("sub")
                found.append((output["name"], facets["rask_lance"]["operation"], output["facets"]["version"]["datasetVersion"], author))
    return found


def _publications(rows: list[dict[str, Any]], table: str) -> list[tuple[int, str | None]]:
    """``(version, author)`` of each feed row announcing that ``published`` moved on ``table``."""
    found = []
    for row in rows:
        event: Mapping[str, Any] = row["event"]
        facets = (event.get("run") or {}).get("facets") or {}
        if (facets.get("rask_lance") or {}).get("operation") == "publish_table" and any(output["name"] == table for output in event.get("outputs") or []):
            found.append((facets["rask_lance"]["version"], (facets.get("rask_author") or {}).get("sub")))
    return found


class _Gate:
    """The hop the catalog announces through: it records every event, refuses it while down, and forwards it once up.

    It forwards under ``bearer``, a caller who may describe every table here: lineage's HTTP door resolves each table an
    event names under the poster's bearer, where the bus a deployment announces on carries the catalog's signed uuids.
    """

    def __init__(self, bearer: str) -> None:
        self.refused: list[dict[str, Any]] = []
        self.lineage: str | None = None
        self.bearer = bearer
        self.app = FastAPI()
        self.app.add_api_route("/api/v1/lineage", self._take, methods=["POST"])

    async def _take(self, event: dict[str, Any] = Body()) -> Response:  # noqa: B008 — FastAPI's own body declaration
        if self.lineage is None:
            self.refused.append(event)
            return Response(status_code=503)
        async with httpx.AsyncClient(base_url=self.lineage, timeout=60) as client:
            forwarded = await client.post("/api/v1/lineage", json=event, headers={"Authorization": self.bearer})
        return Response(status_code=forwarded.status_code, content=forwarded.content)


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
    from service_kit.lakehouse import commit_records

    counted, unrecorded = _Counter(), _Counter()
    monkeypatch.setattr(commit_reconcile, "_reannounced", counted)
    monkeypatch.setattr(commit_authors, "_unrecorded", unrecorded)
    alice = {"Authorization": f"Bearer {dex_issuer.mint('alice', connector='local', audience='lance-catalog')}"}
    gate = _Gate(alice["Authorization"])
    with serve(gate.app) as gate_url:
        env = {
            "LANCE_LINEAGE_EMIT_ENABLED": "true",
            "LANCE_LINEAGE_TRANSPORT": "http",
            "LANCE_LINEAGE_URL": f"{gate_url}/api/v1/lineage",
            "LANCE_COMMIT_RECONCILE_GRACE_SECONDS": "0",
            "RASK_OIDC_ENABLED": "true",
            "RASK_OIDC_ISSUER": dex_issuer.issuer,
            "RASK_OIDC_AUDIENCE": "lance-catalog",
            "RASK_OIDC_ALLOW_INSECURE": "true",
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
            # Mounted as the chart's binding name mounts it at boot (`main.py`), on the app this worker already imported.
            from catalog.main import app

            if not any(getattr(route, "path", None) == f"/{_TICK}" for route in app.routes):
                commit_reconcile.mount_commit_reconcile(app, _TICK)
            assert catalog.post("/v1/namespace/db/create", json={}).status_code == 200
            # The gate is down, so each write's own announcement is lost after its commit.
            assert catalog.post("/v1/table/db$t/create", content=_rows(0, 3), headers=_ARROW).status_code == 200
            assert catalog.post("/v1/table/db$t/insert", content=_rows(3, 5), headers=_ARROW).status_code == 200
            # A delete commits detached and is finalized by the catalog, where the create and the insert commit through Lance.
            assert catalog.post("/v1/table/db$t/delete", json={"predicate": "id = 0"}).status_code == 200
            # A table only declared holds no version: nothing to announce, and nothing the tick cannot read.
            assert catalog.post("/v1/table/db$declared/declare", json={}).status_code == 200
            originals = [event for event in gate.refused if "run" in event and event["outputs"][0]["name"] == "db$t"]
            assert len(originals) == 3, gate.refused
            # The caller the catalog authenticated, as each write's own announcement names it.
            author = originals[0]["run"]["facets"]["rask_author"]["sub"]

            # Lineage reaches the catalog under its own service account, and itself authenticates nobody.
            lineage_token = tmp_path / "lineage-catalog-token"
            lineage_token.write_text(sa_issuer.mint("rask-sa-lineage", audience="rask-catalog"))
            monkeypatch.setenv("RASK_CATALOG_IDENTITY_TOKEN_FILE", str(lineage_token))
            monkeypatch.setenv("RASK_OIDC_ENABLED", "false")
            with live_lineage(catalog_url, monkeypatch, port=free_port()) as lineage_url, httpx.Client(base_url=lineage_url, timeout=60) as lineage:
                gate.lineage = lineage_url
                assert _commits(_follow(lineage), "db$t") == [], "the writes' own announcements reached the feed, so none was lost"

                # A retained version the store fails to open once: the tick keeps the ref's mark, and skips nothing for it.
                real_checkout = lance.LanceDataset.checkout_version

                def unreadable_once(dataset: lance.LanceDataset, version: int | str | tuple[str | None, int | None]) -> lance.LanceDataset:
                    if version == 1 and str(dataset.uri).endswith(location_of_t):
                        monkeypatch.setattr(lance.LanceDataset, "checkout_version", real_checkout)
                        raise OSError("the store did not answer")
                    return real_checkout(dataset, version)

                location_of_t = str(catalog.post("/v1/table/db$t/describe", json={}).json()["location"]).rstrip("/").rsplit("/", 1)[-1]
                monkeypatch.setattr(lance.LanceDataset, "checkout_version", unreadable_once)
                blocked = catalog.post(f"/{_TICK}")
                assert (blocked.json()["reannounced"], blocked.json()["unreadable"]) == (0, 1), blocked.json()

                tick = catalog.post(f"/{_TICK}")
                assert tick.status_code == 200, tick.text
                assert tick.json()["reannounced"] == 3, tick.json()
                assert _commits(_follow(lineage), "db$t") == [
                    ("db$t", "create_table", "1", author),
                    ("db$t", "insert", "2", author),
                    ("db$t", "delete", "3", author),
                ]

                # Each write's own announcement arriving late, as the outbox relay delivers one it had staged; the tick that
                # then finds them on the feed is refused one delete of a version's author record.
                for original in originals:
                    late = lineage.post("/api/v1/lineage", json=original, headers=alice)
                    assert late.status_code == 201, late.text
                real_forget = commit_records.forget_author

                def refused_for_version_2(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — the delete's own arguments
                    if kwargs["version"] == 2:
                        raise OSError("the store refused the delete")
                    real_forget(*args, **kwargs)

                monkeypatch.setattr(commit_records, "forget_author", refused_for_version_2)
                again = catalog.post(f"/{_TICK}")
                monkeypatch.setattr(commit_records, "forget_author", real_forget)
                assert (again.json()["reannounced"], again.json()["unreadable"]) == (0, 0), again.json()
                # The record the store kept holds main's mark below its version, so the next tick reads it and forgets it.
                settings = get_settings()
                control, control_so = settings.registry_root, settings.storage_options()
                uuid_of_t = catalog.post("/v1/table/db$t/describe", json={}).json()["properties"]["rask.uuid"]
                second = lance.dataset(catalog.post("/v1/table/db$t/describe", json={"vend_credentials": False}).json()["location"]).read_transaction(2)
                assert second is not None

                def recorded_second() -> str | None:
                    record = commit_records.recorded(control, control_so, uuid_of_t, transaction_uuid=second.uuid, version=2, ref="")
                    return record.author if record is not None else None

                assert (commit_records.read_marks(control, control_so, uuid_of_t).refs, recorded_second()) == ({"": 1}, author)
                assert _commits(_follow(lineage), "db$t") == [
                    ("db$t", "create_table", "1", author),
                    ("db$t", "insert", "2", author),
                    ("db$t", "delete", "3", author),
                ]

                # A write through a door whose author record the store refuses, its announcement lost too: the write lands,
                # and the miss is logged at ERROR and counted.
                real_record = commit_records.record_author

                def refused(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — the record's own arguments
                    raise OSError("the store refused the write")

                gate.lineage = None
                monkeypatch.setattr(commit_records, "record_author", refused)
                with caplog.at_level(logging.ERROR, logger="catalog.services.commit_authors"):
                    assert catalog.post("/v1/table/db$t/insert", content=_rows(5, 6), headers=_ARROW).status_code == 200
                monkeypatch.setattr(commit_records, "record_author", real_record)
                gate.lineage = lineage_url
                missed = [(record.levelno, record.getMessage()) for record in caplog.records if record.name == "catalog.services.commit_authors"]
                assert missed == [(logging.ERROR, "commit_author_unrecorded")]

                # A write through a door, announced, then two versions committed straight to the store, past every door: one
                # carries the write's transaction uuid and stamps another estate's person, and the write's record names the
                # version it landed at; the other's transaction cannot be read, which no catalog commit leaves.
                assert catalog.post("/v1/table/db$t/insert", content=_rows(6, 7), headers=_ARROW).status_code == 200
                location = catalog.post("/v1/table/db$t/describe", json={"vend_credentials": False}).json()["location"]
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
                past_every_door = catalog.post(f"/{_TICK}")
                monkeypatch.setattr(lance.LanceDataset, "read_transaction", real_read)
                assert (past_every_door.json()["reannounced"], past_every_door.json()["unauthored"]) == (0, 3), past_every_door.json()
                assert recorded_second() is None
                assert _commits(_follow(lineage), "db$t") == [
                    ("db$t", "create_table", "1", author),
                    ("db$t", "insert", "2", author),
                    ("db$t", "delete", "3", author),
                    ("db$t", "insert", "5", author),
                ]

                # A dataset written outside the catalog, registered, then written through the catalog while the gate is
                # down again: only that last write's announcement is the catalog's, and lost.
                gate.lineage = None
                outside = str(tmp_path / "catalog" / "outside" / "registered")
                stock = lance.write_dataset(pa.table({"id": pa.array([0, 1], pa.int64())}), outside, data_storage_version="2.2", enable_stable_row_ids=True)
                stock = lance.write_dataset(
                    pa.table({"id": pa.array([2], pa.int64())}), stock, mode="append", transaction_properties={"rask.author": "dex~mallory"}
                )
                stock.create_branch("work")
                lance.dataset(outside).checkout_version(("work", None)).insert(pa.table({"id": pa.array([3], pa.int64())}))
                registered = catalog.post("/v1/table/db$registered/register", json={"location": "outside/registered"})
                assert registered.status_code == 200, registered.text
                assert catalog.post("/v1/table/db$registered/insert", content=_rows(4, 5), headers=_ARROW).status_code == 200
                gate.lineage = lineage_url

                after_register = catalog.post(f"/{_TICK}")
                assert (after_register.json()["reannounced"], after_register.json()["unauthored"], after_register.json()["unreadable"]) == (1, 0, 0), (
                    after_register.json()
                )
                assert _commits(_follow(lineage), "db$registered") == [("db$registered", "insert", "3", author)]
                # Lineage recorded every event above in the provenance namespace through the catalog's doors, and none of
                # those writes is announced, so none keeps an author record.
                uuid_of_registered = catalog.post("/v1/table/db$registered/describe", json={}).json()["properties"]["rask.uuid"]
                assert {path.name for path in (Path(control) / commit_records.AUTHORS_PREFIX).iterdir()} == {uuid_of_t, uuid_of_registered}

                # A version published while the gate is down: the tag moves, and its announcement is lost after the move.
                gate.lineage = None
                published = catalog.post("/management/v1/table/db$t/publish", json={"version": 3, "key_column": "id"})
                assert (published.status_code, published.json()["published"]) == (200, True), published.text
                lost = [event for event in gate.refused if _publications([{"event": event}], "db$t")]
                assert len(lost) == 1, gate.refused
                gate.lineage = lineage_url
                republished = catalog.post(f"/{_TICK}")
                assert (republished.json()["reannounced"], republished.json()["unauthored"]) == (1, 0), republished.json()
                assert _publications(_follow(lineage), "db$t") == [(3, author)]
                late = lineage.post("/api/v1/lineage", json=lost[0], headers=alice)
                assert late.status_code == 201, late.text
                assert _publications(_follow(lineage), "db$t") == [(3, author)]
                # A publication the feed holds is not announced again, nor is a retry of it, which moves nothing.
                assert catalog.post(f"/{_TICK}").json()["reannounced"] == 0
                retried = catalog.post("/management/v1/table/db$t/publish", json={"version": 3, "key_column": "id"})
                assert (retried.status_code, retried.json()["published"]) == (200, True), retried.text
                after_the_retry = catalog.post(f"/{_TICK}").json()
                assert (after_the_retry["reannounced"], after_the_retry["unauthored"]) == (0, 0), after_the_retry

                # `published` moved straight at the store, past every door: counted unauthored once, announced for nobody,
                # and the record of the publication before it forgotten.
                lance.dataset(location).tags.update("published", 4)
                # A mover record that cannot be read stops the judgement rather than reading as no record.
                unreadable_mover = Path(control) / commit_records.AUTHORS_PREFIX / uuid_of_t / "tags" / "published" / "9.json"
                unreadable_mover.parent.mkdir(parents=True, exist_ok=True)
                unreadable_mover.write_text("not a record")
                held_back = catalog.post(f"/{_TICK}").json()
                unreadable_mover.unlink()
                assert (held_back["unauthored"], held_back["unreadable"]) == (0, 1), held_back
                past_the_tag_door = catalog.post(f"/{_TICK}")
                assert (past_the_tag_door.json()["reannounced"], past_the_tag_door.json()["unauthored"]) == (0, 1), past_the_tag_door.json()
                assert catalog.post(f"/{_TICK}").json()["unauthored"] == 0
                assert _publications(_follow(lineage), "db$t") == [(3, author)]
                assert commit_records.tag_movers(control, control_so, uuid_of_t, tag="published") == []

                # An erasure drops `published`, and the version it named before is published again while the gate is down: a
                # move of its own, so the tick announces it as a run of its own, and its original arriving late is that run.
                lance.dataset(location).tags.delete("published")
                gate.lineage = None
                again = catalog.post("/management/v1/table/db$t/publish", json={"version": 3, "key_column": "id"})
                assert (again.status_code, again.json()["published"]) == (200, True), again.text
                lost_again = [event for event in gate.refused if _publications([{"event": event}], "db$t")][1:]
                gate.lineage = lineage_url
                assert catalog.post(f"/{_TICK}").json()["reannounced"] == 1
                assert lineage.post("/api/v1/lineage", json=lost_again[0], headers=alice).status_code == 201
                assert _publications(_follow(lineage), "db$t") == [(3, author), (3, author)]

    assert [amount for amount, _ in counted.added] == [1, 1, 1, 1, 1, 1]
    # The create, both inserts the catalog recorded, the refused one, and the registered table's insert.
    assert [amount for amount, _ in unrecorded.added] == [0, 0, 1, 0, 0]
    assert [dict(attributes or {}) for _, attributes in counted.added] == [
        {"lance.catalog.operation": "create_table"},
        {"lance.catalog.operation": "insert"},
        {"lance.catalog.operation": "delete"},
        {"lance.catalog.operation": "insert"},
        {"lance.catalog.operation": "publish_table"},
        {"lance.catalog.operation": "publish_table"},
    ]
