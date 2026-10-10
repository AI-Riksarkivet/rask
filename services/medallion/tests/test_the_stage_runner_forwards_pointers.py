"""A tier costs a few KB, not another corpus — `docs/architecture/medallion-data-flow.md`, change 3.

The cascade used to store the corpus once per tier. bronze held the bytes, silver held them again,
gold held them again — three copies to express three readiness states of one thing. `_carry_forward`
materialised every payload and `blob_array` wrote them back out.

When the upstream declares an external base, the payload lives at a URI the dataset does not own, so
the pointer can be forwarded instead. The bytes are still READ where a model needs them; they are
never re-persisted, which is exactly the distinction §4.2 draws.

**The managed path is deliberately unchanged and is tested here too.** A dataset whose payloads exist
at no URI — an Arrow-IPC fragment landed by `lance-append`, a source whose lifecycle is not the
estate's — has nowhere to point, so copying is the only correct answer and must keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
from lance import blob_array, blob_field
from lance.blob import Blob

from medallion.services.compute import transform_stage
from service_kit.lakehouse import blobs


def _corpus(root: Path, count: int, size: int = 100_000) -> list[str]:
    root.mkdir(parents=True, exist_ok=True)
    uris = []
    for i in range(count):
        f = root / f"page-{i:03d}.bin"
        f.write_bytes(b"P" * size)
        uris.append(f.resolve().as_uri())
    return uris


def _disk(path: str) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())


def _resolves(uri: str) -> int:
    table = lance.dataset(uri).scanner(columns=["payload"], blob_handling="all_binary").to_table()
    return sum(1 for value in table.column("payload").to_pylist() if value)


def _bronze(uri: str, uris: list[str], base: str | None) -> None:
    """A bronze tier in either placement — external when `base` is given, managed otherwise."""
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload", nullable=False)])
    payloads: list[Any] = [Blob.from_uri(u) for u in uris] if base else [Path(u[7:]).read_bytes() for u in uris]
    table = pa.table({"id": pa.array(range(len(uris)), pa.int64()), "payload": blob_array(payloads)}, schema=schema)
    lance.write_dataset(
        table,
        uri,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        initial_bases=[lance.DatasetBasePath(base, "source")] if base else None,
    )


class TestAnExternalUpstreamIsForwardedNotCopied:
    def test_a_derived_tier_costs_kilobytes_not_a_second_corpus(self, tmp_path: Path) -> None:
        """The claim the whole change exists for, as a ratio so it survives a fixture resize."""
        source = tmp_path / "corpus"
        uris = _corpus(source, count=20)
        corpus_bytes = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

        bronze = str(tmp_path / "bronze.lance")
        _bronze(bronze, uris, base=str(source))
        silver = str(tmp_path / "silver.lance")
        transform_stage(bronze, silver, {}, stage="silver")

        assert _disk(silver) < corpus_bytes * 0.05, f"silver cost {_disk(silver):,} B against a {corpus_bytes:,} B corpus — it copied the payloads"
        # Cheap is only a win if it still reads. THIS is the assertion that would have caught a
        # descriptor carried forward verbatim: that write is refused loudly, but a mis-mapped one
        # (a zero `size` read back as a zero-length slice) resolves 0/20 in silence.
        assert _resolves(silver) == 20, "the carried pointers do not resolve — the payloads are unreachable from silver"

    def test_the_pointer_survives_a_SECOND_hop(self, tmp_path: Path) -> None:
        """silver → gold. The base has to be re-declared at every tier, not just inherited once.

        A tier that carried pointers but dropped the base metadata would produce a gold table whose
        every blob read returns nothing — and it would do it silently, because a resolved-to-nothing
        blob is indistinguishable from a null one on the read path.
        """
        source = tmp_path / "corpus"
        uris = _corpus(source, count=8)

        bronze = str(tmp_path / "bronze.lance")
        _bronze(bronze, uris, base=str(source))
        silver = str(tmp_path / "silver.lance")
        transform_stage(bronze, silver, {}, stage="silver")
        gold = str(tmp_path / "gold.lance")
        transform_stage(silver, gold, {}, stage="gold")

        assert blobs.external_base_of(lance.dataset(silver)) == str(source), "silver dropped the base it inherited"
        assert _resolves(gold) == 8
        assert set(lance.dataset(gold).to_table(columns=["stage"]).column("stage").to_pylist()) == {"gold"}


class TestTheManagedPathIsUnchanged:
    """No base means the bytes exist nowhere else. Copying is correct and must keep working."""

    def test_a_managed_upstream_still_carries_its_bytes(self, tmp_path: Path) -> None:
        source = tmp_path / "corpus"
        uris = _corpus(source, count=6, size=50_000)
        corpus_bytes = sum(f.stat().st_size for f in source.rglob("*") if f.is_file())

        bronze = str(tmp_path / "bronze.lance")
        _bronze(bronze, uris, base=None)
        silver = str(tmp_path / "silver.lance")
        transform_stage(bronze, silver, {}, stage="silver")

        assert _resolves(silver) == 6
        assert _disk(silver) > corpus_bytes * 0.9, "a managed upstream must still carry its payloads — they exist at no URI"

    def test_a_tabular_upstream_is_untouched_by_either_path(self, tmp_path: Path) -> None:
        """No blob column at all keeps the cheap straight-through read."""
        bronze = str(tmp_path / "bronze.lance")
        lance.write_dataset(
            pa.table({"id": pa.array(range(4), pa.int64()), "note": pa.array(list("abcd"), pa.string())}),
            bronze,
            mode="create",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
        silver = str(tmp_path / "silver.lance")
        transform_stage(bronze, silver, {}, stage="silver")

        out = lance.dataset(silver).to_table()
        assert out.num_rows == 4
        assert set(out.column("stage").to_pylist()) == {"silver"}


class TestTheDerivabilityProbeReadsOnePayload:
    """Deciding "is this derivable" must not cost the tier — §8 change 3, second half ([[CP-051]]).

    The deriver is chosen by the column's first NON-null payload, so the probe needs exactly that one
    payload. A failed harvest writes a null blob (R27), so a long prefix of nulls is a real shape, and a
    probe that reads forward through it, or falls back to reading the column, reads the corpus to answer
    a question about one row.
    """

    def test_a_null_prefix_is_probed_by_reading_only_the_first_non_null_payload(self, tmp_path: Path) -> None:
        """Every object after the first non-null row is deleted, so any read past that row fails the stage.

        The payloads are not images, so nothing derives and the stage forwards the external pointers without
        opening them: the probe is the only reader of payload bytes in this run. The prefix is longer than
        the 64-row window an earlier probe read before falling back to the whole column.
        """
        source = tmp_path / "corpus"
        uris = _corpus(source, count=4, size=2_000)
        nulls = 100
        payloads: list[Any] = [None] * nulls + [Blob.from_uri(u) for u in uris]
        schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload", nullable=True)])
        bronze = str(tmp_path / "bronze.lance")
        lance.write_dataset(
            pa.table({"id": pa.array(range(len(payloads)), pa.int64()), "payload": blob_array(payloads)}, schema=schema),
            bronze,
            mode="create",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
            initial_bases=[lance.DatasetBasePath(str(source), "source")],
        )
        for uri in uris[1:]:
            Path(uri[7:]).unlink()

        silver = str(tmp_path / "silver.lance")
        transform_stage(bronze, silver, {}, stage="silver")

        out = lance.dataset(silver).to_table(columns=["id", "payload"]).sort_by("id")
        assert out.num_rows == len(payloads)
        assert [row is None for row in out.column("payload").to_pylist()] == [True] * nulls + [False] * len(uris)
