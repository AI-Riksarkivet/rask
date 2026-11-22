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
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio.to_thread
import pytest
from pydantic import SecretStr

from lakehouse.process.own_threads import ThreadSet, on_own_threads
from lakehouse.records import commit_turns
from lakehouse.records.commit_turns import TableTurns, Writer
from lakehouse.storage import conditional_records
from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.options import StorageOptions


TABLE_UUID = "33333333-3333-4333-8333-333333333333"
OTHER_TABLE_UUID = "44444444-4444-4444-8444-444444444444"

#: Longer than an answer is shared for (``commit_turns._SHARED_READ_SECONDS``, 0.1 s), as a loaded store's reads are.
SLOW_READ_SECONDS = 0.3

#: Long enough that an answer from before a write cannot lapse before the next poll, however loaded the host.
HELD_FOR_THE_TEST_SECONDS = 60.0

WAITERS = 20

#: How long a call on the process's own threads is given to answer while writers wait: far past a thread's start.
ANSWERED_WITHIN_SECONDS = 2.0

#: How long writers arriving after those that gave up are watched for the threads they must not start.
WATCHED_SECONDS = 0.3

#: How the threads of the turn reads' set are named (`lakehouse.process.own_threads.ThreadSet`).
TURN_READ_THREADS = "lakehouse-commit-turn-reads_"

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
    read = conditional_records.read_json_at
    signers: list[str] = []

    def _slow(root_uri: str, storage_options: StorageOptions, key: str) -> conditional_records.StoreRead:
        if key == identities[0].key:
            signers.append(storage_options.key_pair.access_key_id if storage_options.key_pair is not None else "")
            time.sleep(SLOW_READ_SECONDS)
        return read(root_uri, storage_options, key)

    monkeypatch.setattr(conditional_records, "read_json_at", _slow)
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
    read = conditional_records.read_json_at
    in_flight, handed = threading.Event(), threading.Event()

    def _held_in_flight(root_uri: str, storage_options: StorageOptions, key: str) -> conditional_records.StoreRead:
        found = read(root_uri, storage_options, key)
        if threading.current_thread().name == "waiter":
            in_flight.set()
            handed.wait(timeout=10)
        return found

    monkeypatch.setattr(conditional_records, "read_json_at", _held_in_flight)
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
    create, replace = conditional_records.create_json, conditional_records.replace_json
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
        create(root_uri, storage_options, key, record=record)

    def _replace(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, Any], *, etag: str) -> None:
        _beaten("replace_json")
        replace(root_uri, storage_options, key, record=record, etag=etag)

    monkeypatch.setattr(conditional_records, "create_json", _create)
    monkeypatch.setattr(conditional_records, "replace_json", _replace)
    began = time.monotonic()
    try:
        ours = here.take(None)
        took = time.monotonic() - began
    finally:
        for timer in released:
            timer.join()

    assert (len(theirs), here.keeps(ours), took >= 0.9) == (1, True, True), f"the take came back after {took:.2f} s"


@pytest.fixture
def slow_read(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[threading.Event, threading.Event]]:
    """Every read of a record held in flight, as a slow store holds it: the first event is set once a read has begun, and
    the second lets the reads answer (set at teardown too, so no thread is left waiting)."""
    read = conditional_records.read_json_at
    in_flight, answered = threading.Event(), threading.Event()

    def _held_in_flight(root_uri: str, storage_options: StorageOptions, key: str) -> conditional_records.StoreRead:
        in_flight.set()
        answered.wait(timeout=30)
        return read(root_uri, storage_options, key)

    monkeypatch.setattr(conditional_records, "read_json_at", _held_in_flight)
    yield in_flight, answered
    answered.set()


async def _begun(in_flight: threading.Event) -> None:
    """Return once a waiter's read of the record is in flight."""
    async with asyncio.timeout(10):
        while not in_flight.is_set():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_waiters_held_on_one_slow_read_hold_no_request_thread_and_none_of_the_processs_own(
    tmp_path: Path, slow_read: tuple[threading.Event, threading.Event]
) -> None:
    """As many writers as their thread set seats wait for one table's turn while a slow store holds the record's read in
    flight: they hold no thread of the request pool, and a call on the process's own set still answers, so neither a door
    nor the store session's renewal nor a readiness read queues behind them."""
    in_flight, answered = slow_read
    turns = _turns(tmp_path, "AKIDCATALOG")
    waiters = [asyncio.create_task(turns.wait_in_loop(None)) for _ in range(ThreadSet.seats)]
    own: list[asyncio.Task[int]] = []
    try:
        await _begun(in_flight)
        request_threads_held = anyio.to_thread.current_default_thread_limiter().borrowed_tokens
        own = [asyncio.create_task(on_own_threads(threading.get_ident))]
        seated, _ = await asyncio.wait(own, timeout=ANSWERED_WITHIN_SECONDS)
    finally:
        answered.set()
        await asyncio.gather(*waiters, *own, return_exceptions=True)

    assert request_threads_held == 0, f"writers waiting for a turn held {request_threads_held} of the request pool's threads"
    assert seated == set(own), "a call on the process's own threads waited behind the writers waiting for a turn"


@pytest.mark.asyncio
async def test_writers_that_give_up_waiting_leave_their_set_no_thread_past_its_seats(
    tmp_path: Path, slow_read: tuple[threading.Event, threading.Event]
) -> None:
    """Writers waiting on a slow read give up, their requests cancelled, and as many more arrive: each that gave up stops
    waiting while the read is still in flight, and its thread keeps its seat until the read answers, so the waiters' set
    runs no more threads than it seats however many writers come and go against a store that is not answering."""
    in_flight, answered = slow_read
    turns = _turns(tmp_path, "AKIDCATALOG")
    gave_up = [asyncio.create_task(turns.wait_in_loop(None)) for _ in range(ThreadSet.seats)]
    arrived_after: list[asyncio.Task[bool]] = []
    try:
        await _begun(in_flight)
        for waiter in gave_up:
            waiter.cancel()
        _, still_waiting = await asyncio.wait(gave_up, timeout=ANSWERED_WITHIN_SECONDS)
        arrived_after = [asyncio.create_task(turns.wait_in_loop(None)) for _ in range(ThreadSet.seats)]
        await asyncio.sleep(WATCHED_SECONDS)
        threads = [thread.name for thread in threading.enumerate() if thread.name.startswith(TURN_READ_THREADS)]
    finally:
        answered.set()
        await asyncio.gather(*gave_up, *arrived_after, return_exceptions=True)

    assert len(still_waiting) == 0, f"{len(still_waiting)} writers that gave up still waited on the read {ANSWERED_WITHIN_SECONDS} s later"
    assert [waiter.cancelled() for waiter in gave_up] == [True] * ThreadSet.seats
    assert len(threads) == ThreadSet.seats, f"the waiters' set ran {len(threads)} threads on its {ThreadSet.seats} seats"
