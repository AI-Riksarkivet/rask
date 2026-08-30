"""Fragment batching and blob placement — the two defects that made bronze unusable at scale.

Both lived in the same six lines of `worker.py`, and both were invisible with four fixtures: four
units produced four fragments, and four small TIFFs are under the inline threshold anyway. Only a
real volume would have shown either, which is exactly why they are pinned here instead.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from ingest.runtime import BRONZE_SCHEMA
from ingest.worker import _is_permanent, units_to_table


def _units(n: int, size: int = 64) -> list[tuple[str, bytes]]:
    return [(f"file:///pages/{i:05d}.tif", bytes([i % 256]) * size) for i in range(n)]


# ── the batch is ONE table, and one table is one fragment ─────────────────────────────


def test_a_batch_of_units_is_ONE_table(tmp_path: Path) -> None:
    """The shape the fix turns on. `units_to_table` took a list all along; the caller passed one.

    A table of N rows becomes one fragment, so the fragment count is decided entirely by how many
    units the worker accumulates before calling this.
    """
    table = units_to_table(_units(500))

    assert table.num_rows == 500


def test_ONE_fragment_per_batch_not_per_row(tmp_path: Path) -> None:
    """The regression guard, asserted where it actually bit: the fragment COUNT.

    Before the fix this produced 500 fragments for 500 pages — a 10k-page volume meant 10k fragments
    in a single commit, 10k staging manifests, and 10k FragmentMetadata blobs across a Dapr activity
    boundary. Lance's guidance is ~1M rows per fragment and its ingestion notes name the per-row
    write as the anti-pattern outright.
    """
    from ingest.lander import create_empty, write_unit_fragments

    uri = str(tmp_path / "bronze.lance")
    create_empty(uri, BRONZE_SCHEMA)

    written = write_unit_fragments(uri, units_to_table(_units(500)))

    assert len(written) == 1, f"500 units produced {len(written)} fragments — batching is not in effect"


def test_the_fragment_batch_stays_under_the_queues_ack_ceiling() -> None:
    """A batch larger than `max_ack_pending` deadlocks: the worker holds the batch UNACKED while it
    fills, JetStream stops delivering at the ceiling, and the drain waits for units it will never be
    sent. Asserted as a relation so raising one without the other fails here."""
    from ingest.queue import max_ack_pending
    from ingest.sizing import resolve

    assert resolve().fragment_rows < max_ack_pending()


def test_a_REQUESTED_row_target_over_the_ack_ceiling_is_REFUSED() -> None:
    """The deployment default being safe is not enough once the value is caller-supplied.

    Sizing is per-run now, so a caller can ask for Lance's own recommended 1M rows — which is a
    perfectly sensible number to read off the docs, and which would hang this plane: the batch is
    held unacked while it fills, JetStream stops at `max_ack_pending`, and the drain waits forever
    for units it will not be sent. No error, no crash, just a run that never finishes.

    So the refusal happens at ACCEPT with the ceiling named, not silently at 3am mid-harvest.
    """
    from ingest.queue import max_ack_pending
    from ingest.sizing import IngestSizing, SizingRefused, resolve

    with pytest.raises(SizingRefused, match=str(max_ack_pending())):
        resolve(IngestSizing(fragment_rows=1_000_000))

    # …and the boundary is exactly the ceiling, not "somewhere near it".
    with pytest.raises(SizingRefused):
        resolve(IngestSizing(fragment_rows=max_ack_pending()))
    assert resolve(IngestSizing(fragment_rows=max_ack_pending() - 1)).fragment_rows == max_ack_pending() - 1


def test_an_unset_sizing_field_falls_back_to_the_DEPLOYMENT_default() -> None:
    """Per-run sizing must not force a caller to specify all four.

    A request that names only `fetch_concurrency` (the common case — being polite to one endpoint)
    must leave the fragment layout exactly as the deployment has it, or every caller becomes
    responsible for numbers they have no basis to pick.
    """
    from ingest.sizing import IngestSizing, resolve

    base = resolve()
    only_one = resolve(IngestSizing(fetch_concurrency=2))

    assert only_one.fetch_concurrency == 2
    assert only_one.fragment_rows == base.fragment_rows
    assert only_one.fragment_bytes == base.fragment_bytes
    assert only_one.fetch_batch == base.fetch_batch


def test_a_byte_ceiling_exists_alongside_the_row_ceiling() -> None:
    """Rows alone is the wrong trigger for page images.

    A row-only rule at 1024 would let one fragment reach tens of gigabytes on a large-format volume
    — Lance puts the sane upper range at 10-100 GB per fragment with 1 TB a hard ceiling — so the
    batch flushes on whichever limit arrives first.
    """
    from ingest.sizing import resolve

    assert resolve().fragment_bytes >= 16 * 1024 * 1024, "a byte ceiling this small would fragment on every page"


# ── the payload is a BLOB column, with real placement tiers ───────────────────────────


def test_payload_is_a_BLOB_field_not_plain_binary() -> None:
    """`pa.binary()` forces every page image INLINE into the .lance data file.

    That gives up all three placement tiers — no dedicated `.blob` for a large page (REFERENCED
    rather than re-copied by every compaction), no packed sidecar protecting against small-file
    explosion — and leaves readers with no `read_blobs` / `take_blobs` / `read_blob_ranges` at all.
    The code this plane REPLACED already used `blob_field`, so this was a regression against
    knowledge the estate had already paid for.

    Asserted through the estate's own detector rather than by inspecting metadata, so it stays true
    if the encoding key moves.
    """
    from service_kit.lancekit.blobs import blob_field_names

    assert blob_field_names(BRONZE_SCHEMA) == ["payload"]


def test_a_blob_payload_round_trips_through_read_blobs(tmp_path: Path) -> None:
    """`read_blobs` is the documented path for complete byte payloads (training loaders, batch work).

    `take_blobs` is for a BlobFile handle — streaming, seeking, partial reads — and the Lance guide
    is explicit that you should not wrap `take_blobs` in a thread pool just to `read()` everything.
    """
    uri = str(tmp_path / "bronze.lance")
    payloads = [b"PAGE-ONE" * 4, b"PAGE-TWO" * 4]
    table = units_to_table([(f"file:///p/{i}.tif", p) for i, p in enumerate(payloads)])
    lance.write_dataset(table, uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)

    dataset = lance.dataset(uri)
    got = dataset.read_blobs("payload", indices=[0, 1])

    assert [blob for _, blob in got] == payloads


def test_bronze_CANNOT_EXPRESS_the_null_that_makes_readers_lie(tmp_path: Path) -> None:
    """The null-drop landmine, now closed at the schema instead of documented.

    This used to write a null through `BRONZE_SCHEMA` and assert `read_blobs` returned 2 rows for 3
    selected — pinning the trap so callers would not zip results positionally. Testing all three read
    APIs (`test_blob_read_apis.py`) showed the trap is worse than that: `take_blobs` returns a BARE
    LIST with no row identity on the handle, so `handles[i]` silently becomes a different row's
    bytes, and `read_blob_ranges` answers `[]` for a null with no error.

    A trap that three APIs fall into differently is not one to document — it is one to make
    unreachable. `payload` is non-nullable, so Lance refuses the write, and the plane could not
    produce such a row anyway (the worker parks an empty payload rather than writing it).

    The behaviour of the READ APIs against a null is still pinned, against a deliberately nullable
    schema, in `test_blob_read_apis.py::test_ALL_THREE_apis_silently_drop_a_null_row` — so a pylance
    upgrade that fixes it upstream still fails a test and gets noticed.
    """
    uri = str(tmp_path / "nulls.lance")
    from lance import blob_array

    table = pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "source_uri": pa.array(["a", "b", "c"], pa.string()),
            "payload": blob_array([b"aaa", None, b"ccc"]),
            "sha256": pa.array(["x", "y", "z"], pa.string()),
            "stage": pa.array(["bronze"] * 3, pa.string()),
            "etag": pa.array([None] * 3, pa.string()),
            # Irrelevant to the null-payload trap under test, but the schema declares it — omitting a
            # declared column raises a pyarrow KeyError, which would mask the OSError this asserts.
            "partition_key": pa.array([None] * 3, pa.string()),
        },
        schema=BRONZE_SCHEMA,
    )

    with pytest.raises(OSError, match="non-nullable"):
        lance.write_dataset(table, uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)


# ── permanent vs transient fetch failures ─────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response = _Resp(status)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 414, 451])
def test_a_permanent_status_is_permanent(status: int) -> None:
    """No amount of retrying turns a 404 into a page. These park on the first attempt."""
    assert _is_permanent(_HttpError(status)) is True


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_a_retryable_status_is_NOT_permanent(status: int) -> None:
    """429 is the one 4xx that means "try again" — parking it would discard a page the source was
    merely asking us to slow down about. 408 and 425 likewise invite a retry."""
    assert _is_permanent(_HttpError(status)) is False


def test_the_fetchers_own_refusals_are_permanent() -> None:
    """An unresolvable scheme or a missing local path cannot improve on retry either."""
    assert _is_permanent(ValueError("no fetcher for scheme 'gopher'")) is True
    assert _is_permanent(FileNotFoundError("/gone.tif")) is True


def test_an_UNRECOGNISED_failure_is_treated_as_transient() -> None:
    """The safe default, and the asymmetry is the reason.

    A wrongly-retried unit costs a few requests; a wrongly-parked one costs DATA — the page is
    dropped from the run and reported as an error that never happened. So anything this function
    cannot classify gets redelivered rather than discarded.
    """
    assert _is_permanent(TimeoutError("read timed out")) is False
    assert _is_permanent(ConnectionResetError()) is False


# ── fixity (#99) ──────────────────────────────────────────────────────────────────────


def test_the_sha256_column_digests_the_PAYLOAD_not_the_key() -> None:
    """#99: the fixity digest is over the bytes AS FETCHED. `id` already hashes the KEY — a
    plausible wrong implementation hashes it twice, which would make every fixity check vacuously
    green (the key never rots). Distinct inputs, distinct digests, asserted against hashlib
    directly."""
    import hashlib

    units = [("file:///pages/00001.tif", b"PAGE-ONE-BYTES"), ("file:///pages/00002.tif", b"PAGE-TWO-BYTES")]
    table = units_to_table(units)

    digests = table.column("sha256").to_pylist()
    assert digests == [hashlib.sha256(p).hexdigest() for _, p in units]
    assert digests != [hashlib.sha256(k.encode()).hexdigest() for k, _ in units], "the digest hashed the KEY — fixity over a filename detects nothing"
