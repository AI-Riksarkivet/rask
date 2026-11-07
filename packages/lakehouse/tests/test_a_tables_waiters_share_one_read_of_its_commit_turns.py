"""A table's waiters share one read of its commit turns, and every decision of who holds a turn reads the store ([[LH-259]]).

Every writer waiting on a table's commit turn asks the turn record whether the turn is still held. Read per waiter, the
reads grew with the waiters: measured on the estate 2026-10-31, 53 writers waiting on one held turn pegged the catalog's
1-CPU limit. So the waiters of a table in one process share one read (``commit_turns._SHARED_READS``), the waiters queued
behind a read taking its answer however long the store takes to give it, and an answer is shared only among waiters
signing as one identity, so the store judges every credential the record is read under.

The process's own write of the record clears the view, and a read in flight across that write keeps nothing. Another
process's write (another catalog replica's, or one made under another signing identity, which keeps a view of its own)
reaches this view only once it is renewed, so the view answers only whether to keep waiting: whether the record keeps a
writer's own turn, which decides its release (``TableTurns.keeps``), and whether a door's take may write its turn, are
read from the store, and a take whose write another replica beat reads the record again rather than failing.

Driven on a real local control root, with the record's reads wrapped to make them slow or to hold one in flight. A view
signed as another identity stands in for another replica's.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from lakehouse import commit_turns, records
from lakehouse.commit_turns import TableTurns, Writer
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import StorageOptions


TABLE_UUID = "33333333-3333-4333-8333-333333333333"
OTHER_TABLE_UUID = "44444444-4444-4444-8444-444444444444"

#: Longer than an answer is shared for (``commit_turns._SHARED_READ_SECONDS``, 0.1 s), as a loaded store's reads are.
SLOW_READ_SECONDS = 0.3

#: Long enough that an answer from before a write cannot lapse before the next poll, however loaded the host.
HELD_FOR_THE_TEST_SECONDS = 60.0

WAITERS = 20

WRITER = Writer(caller="alice", transaction="0f6c1a52-8a39-4a46-9a3e-2f0d6f8b9c11")


def _turns(tmp_path: Path, signer: str, table_uuid: str = TABLE_UUID) -> TableTurns:
    """A table's turns on a local control root, read as the identity ``signer`` (a local root signs nothing)."""
    key_pair = S3KeyPair(access_key_id=signer, secret_access_key=SecretStr("unused-on-a-local-root"))
    return TableTurns(control_root=str(tmp_path / "control"), storage_options=StorageOptions(key_pair=key_pair), table_uuid=table_uuid)


def test_waiters_reading_as_one_identity_share_one_slow_read_while_another_table_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty waiters ask at once, half signing as one identity and half as another, while each read of the record takes
    longer than an answer is shared for and another table's turn is taken and released over and over: the store is asked
    once per identity, and every waiter is answered."""
    identities = [_turns(tmp_path, "AKIDALICE"), _turns(tmp_path, "AKIDBOB")]
    other = _turns(tmp_path, "AKIDALICE", OTHER_TABLE_UUID)
    read = records.read_json_at
    signers: list[str] = []

    def _slow(root_uri: str, storage_options: StorageOptions, key: str) -> records.StoreRead:
        if key == identities[0].key:
            signers.append(storage_options.key_pair.access_key_id if storage_options.key_pair is not None else "")
            time.sleep(SLOW_READ_SECONDS)
        return read(root_uri, storage_options, key)

    monkeypatch.setattr(records, "read_json_at", _slow)
    stop = threading.Event()

    def _commit_elsewhere() -> None:
        while not stop.is_set():
            with other.held(None):
                pass

    elsewhere = threading.Thread(target=_commit_elsewhere, daemon=True)
    elsewhere.start()
    start = threading.Barrier(WAITERS)
    held: list[bool] = []

    def _ask(turns: TableTurns) -> None:
        start.wait(timeout=10)
        held.append(turns.poll(None))

    waiters = [threading.Thread(target=_ask, args=(identities[i % 2],)) for i in range(WAITERS)]
    for waiter in waiters:
        waiter.start()
    for waiter in waiters:
        waiter.join(timeout=60)
    stop.set()
    elsewhere.join(timeout=10)

    assert held == [False] * WAITERS, f"{len(held)} of {WAITERS} waiters were answered"
    assert set(signers) == {"AKIDALICE", "AKIDBOB"} and len(signers) <= 4, f"the store was asked as {signers}"


def test_a_read_in_flight_across_the_processs_own_hand_leaves_its_waiters_no_view_from_before_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiter's read of the record starts before the process hands a stock writer the turn and lands after it: the
    process's next waiter still finds the turn held, rather than passing its wait on the answer from before the hand."""
    monkeypatch.setattr(commit_turns, "_SHARED_READ_SECONDS", HELD_FOR_THE_TEST_SECONDS)
    turns = _turns(tmp_path, "AKIDCATALOG")
    read = records.read_json_at
    in_flight, handed = threading.Event(), threading.Event()

    def _held_in_flight(root_uri: str, storage_options: StorageOptions, key: str) -> records.StoreRead:
        found = read(root_uri, storage_options, key)
        if threading.current_thread().name == "waiter":
            in_flight.set()
            handed.wait(timeout=10)
        return found

    monkeypatch.setattr(records, "read_json_at", _held_in_flight)
    waiter = threading.Thread(target=turns.poll, args=(None,), name="waiter")
    waiter.start()
    assert in_flight.wait(timeout=10), "the waiter's read never started"
    turns.hand(None, WRITER)
    handed.set()
    waiter.join(timeout=10)

    assert turns.poll(None) is True, "a waiter passed on a view from before the process's own hand"


def test_a_writer_handed_its_turn_by_another_replica_finds_it_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another replica hands a stock writer the turn while this process's view still holds an answer from before the
    hand: the writer's wait here answers that the record keeps its turn, so its commit releases it rather than leaving it
    held to its lapse."""
    monkeypatch.setattr(commit_turns, "_SHARED_READ_SECONDS", HELD_FOR_THE_TEST_SECONDS)
    here, elsewhere = _turns(tmp_path, "AKIDHERE"), _turns(tmp_path, "AKIDELSEWHERE")
    assert here.poll(None) is False
    elsewhere.hand(None, WRITER)

    assert asyncio.run(here.wait_in_loop(None, mine=WRITER.token, handed=False)) is True


@pytest.mark.parametrize("beaten_to", [pytest.param("create_json", id="the-tables-first-turn"), pytest.param("replace_json", id="a-later-turn")])
def test_a_doors_take_beaten_to_the_record_by_another_replica_waits_out_that_turn_and_holds_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, beaten_to: str
) -> None:
    """Another replica takes the turn between a door's read of the record and its write, so the store refuses the door's
    write: its create-if-absent on the table's first turn, its replace under the ETag it read on a later one. This
    process's view still holds no turn, so the take's next wait passes on it, and the store, read again, holds the
    winner's: the take waits that turn out, released a second later, and then holds the next, rather than failing,
    writing over it, or spending its rounds on the view. An answer is held half a second here, longer than a take's
    rounds take however loaded the host, and short enough that the release is seen."""
    monkeypatch.setattr(commit_turns, "_SHARED_READ_SECONDS", 0.5)
    here, elsewhere = _turns(tmp_path, "AKIDHERE"), _turns(tmp_path, "AKIDELSEWHERE")
    if beaten_to == "replace_json":
        here.release(here.take(None))
    assert here.poll(None) is False
    create, replace = records.create_json, records.replace_json
    beaten = threading.Event()
    theirs: list[str] = []
    released: list[threading.Timer] = []

    def _beaten(write: str) -> None:
        if write == beaten_to and not beaten.is_set():
            beaten.set()
            theirs.append(elsewhere.take(None))
            released.append(threading.Timer(1.0, elsewhere.release, args=(theirs[0],)))
            released[0].start()

    def _create(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, Any]) -> None:
        _beaten("create_json")
        create(root_uri, storage_options, key, record)

    def _replace(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, Any], *, etag: str) -> None:
        _beaten("replace_json")
        replace(root_uri, storage_options, key, record, etag=etag)

    monkeypatch.setattr(records, "create_json", _create)
    monkeypatch.setattr(records, "replace_json", _replace)
    began = time.monotonic()
    try:
        ours = here.take(None)
        took = time.monotonic() - began
    finally:
        for timer in released:
            timer.join()

    assert (len(theirs), here.keeps(ours), took >= 0.9) == (1, True, True), f"the take came back after {took:.2f} s"
