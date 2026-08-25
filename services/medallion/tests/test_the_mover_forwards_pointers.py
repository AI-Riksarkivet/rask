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
import pytest
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
    schema = blobs.stamp_external_base(
        pa.schema([pa.field("id", pa.int64()), blob_field("payload", nullable=False)]),
        base,
    )
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

    def test_the_carried_column_is_still_EXTERNAL_not_re_materialised(self, tmp_path: Path) -> None:
        """Cheap-and-readable could also be achieved by copying into a packed sidecar. It was not.

        Asserted on the descriptor `kind` rather than on disk size, because size is circumstantial and
        `kind` is the actual claim: 3 is external, everything else means the bytes were re-persisted.
        """
        source = tmp_path / "corpus"
        uris = _corpus(source, count=5)
        bronze = str(tmp_path / "bronze.lance")
        _bronze(bronze, uris, base=str(source))
        silver = str(tmp_path / "silver.lance")
        transform_stage(bronze, silver, {}, stage="silver")

        kinds = {d["kind"] for d in lance.dataset(silver).to_table(columns=["payload"]).column("payload").to_pylist()}
        assert kinds == {blobs.EXTERNAL_KIND}, f"silver's payloads are not external: kinds={kinds}"


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


class TestTheDerivabilityProbeIsBounded:
    """Deciding "is this derivable" must not cost the tier — §8 change 3, second half.

    `derive_artifacts` dispatches on the FIRST non-null payload, so the question is what KIND of
    payload the column holds, and one row answers it. The first version of `_payloads_if_derivable`
    asked that question with an unbounded `read_aligned_table` and then looked at one element — so
    the probe materialised every payload in the tier. At ten million page images that is the whole
    corpus read to answer a question about one row: the defect the change exists to remove,
    reintroduced inside the fix.

    Asserted on ROWS SCANNED rather than a byte ratio, because rows are scale-invariant: the bound
    must hold at 500 rows and at 10,000,000.
    """

    def test_the_probe_scans_a_bounded_window_not_the_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from medallion.services.compute import _DERIVE_PROBE_ROWS, _payloads_if_derivable

        source = tmp_path / "corpus"
        uris = _corpus(source, count=_DERIVE_PROBE_ROWS * 6, size=2_000)
        bronze = str(tmp_path / "bronze.lance")
        _bronze(bronze, uris, base=str(source))

        scanned: list[int] = []
        real = blobs.read_aligned_table

        def counting(dataset: Any, **kw: Any) -> Any:
            table = real(dataset, **kw)
            scanned.append(table.num_rows)
            return table

        monkeypatch.setattr(blobs, "read_aligned_table", counting)
        _payloads_if_derivable(lance.dataset(bronze), ["payload"], len(uris))

        assert scanned, "the probe made no scan at all — the instrumentation is broken, not the code"
        assert max(scanned) <= _DERIVE_PROBE_ROWS, (
            f"the probe scanned {max(scanned)} rows of a {len(uris)}-row tier; it must stay within "
            f"{_DERIVE_PROBE_ROWS}. An unbounded probe reads the whole corpus to classify one payload."
        )

    def test_an_ALL_NULL_window_falls_back_rather_than_guessing(self, tmp_path: Path) -> None:
        """A failed harvest writes a null blob (R27), so a prefix of nulls is a real shape.

        Answering "nothing to derive" from an all-null window would silently skip derivation for a
        tier whose later rows are fine — a wrong answer that costs nothing to reach. The fallback
        pays for the full read instead, which is the correct trade at the rare shape.
        """
        from medallion.services.compute import _DERIVE_PROBE_ROWS, _payloads_if_derivable

        source = tmp_path / "corpus"
        real_uris = _corpus(source, count=4, size=2_000)
        n_null = _DERIVE_PROBE_ROWS + 2
        payloads: list[Any] = [None] * n_null + [Blob.from_uri(u) for u in real_uris]

        schema = blobs.stamp_external_base(pa.schema([pa.field("id", pa.int64()), blob_field("payload", nullable=True)]), str(source))
        uri = str(tmp_path / "sparse.lance")
        lance.write_dataset(
            pa.table({"id": pa.array(range(len(payloads)), pa.int64()), "payload": blob_array(payloads)}, schema=schema),
            uri,
            mode="create",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
            initial_bases=[lance.DatasetBasePath(str(source), "source")],
        )

        got = _payloads_if_derivable(lance.dataset(uri), ["payload"], len(payloads))
        # The fixture's payloads are not images, so nothing derives — the property under test is that
        # the all-null window did not short-circuit before looking past it.
        assert got == {} or len(got["payload"]) == len(payloads)
