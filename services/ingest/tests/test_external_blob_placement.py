"""Bronze stores the corpus ONCE, by reference — `docs/architecture/medallion-data-flow.md`, changes 1 and 2.

The managed placement copies every source byte into the bronze dataset, and then the cascade copies
them again into silver and again into gold. Measured on a real corpus that is 100.1% / 100% / 100%:
three copies of the bytes to express three readiness states of one thing.

External placement stores the URI instead. The bytes never move, and the descriptor still resolves
after being carried into a second dataset — which is what lets changes 3 and 4 stop the mover
copying at all.

**The two halves cannot be tested apart, which is why they are one file.** `initial_bases` is
CREATE-MODE ONLY, so a dataset that did not register its base at create can never accept an external
descriptor afterwards; and Lance refuses an external URI outside a registered base
(`allow_external_blob_outside_bases` defaults False, and this estate must never set it True). So
"ingest writes External" and "the base is registered at create" are one change wearing two numbers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pytest
from ingest.adapters import register_builtin_sources
from ingest.lander import create_empty
from ingest.runtime import approved_external_base
from ingest.sources import SourceSpec, external_base_for
from ingest.worker import units_to_table


register_builtin_sources()


def _payloads(root: Path, count: int, size: int = 50_000) -> list[tuple[str, bytes]]:
    """`count` real files, returned as the (key, bytes) units a worker holds after fetching."""
    root.mkdir(parents=True, exist_ok=True)
    units: list[tuple[str, bytes]] = []
    for i in range(count):
        f = root / f"page-{i:03d}.bin"
        f.write_bytes(b"X" * size)
        units.append((f.resolve().as_uri(), f.read_bytes()))
    return units


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _resolves(uri: str) -> int:
    """How many rows hand back real payload bytes.

    `blob_handling="all_binary"` rather than `read_blobs`/`take_blobs`: those two silently DROP null
    rows, so a count taken through them cannot distinguish "the pointer is dead" from "the row was
    skipped" — the exact ambiguity this test exists to remove.
    """
    table = lance.dataset(uri).scanner(columns=["payload"], blob_handling="all_binary").to_table()
    return sum(1 for value in table.column("payload").to_pylist() if value)


class TestThePlacementIsAdapterDeclared:
    """Only the adapter knows what contains its unit keys, so only the adapter may say."""

    def test_s3_prefix_declares_its_BUCKET_not_its_prefix(self) -> None:
        """Every key is `s3://<bucket>/<object>`, so the bucket is the one root that holds all of them.

        The prefix would be wrong in both directions: a run with no prefix would derive the bucket
        anyway, and two runs over two prefixes of one bucket would register two bases for one store.
        """
        spec = SourceSpec(kind="s3-prefix", project="p", dataset="d", options={"bucket": "corpus", "prefix": "volumes/A/"})
        assert external_base_for(spec) == "s3://corpus"

    def test_lance_append_declares_NO_base_because_its_bytes_exist_at_no_uri(self) -> None:
        """Not an omission — the one kind that genuinely must own its bytes.

        Its fetcher SYNTHESISES each unit as Arrow IPC from dataset fragments. There is no object
        anywhere for a descriptor to point at, so managed is the only correct placement and `None` is
        the honest answer rather than a fallback.
        """
        spec = SourceSpec(kind="lance-append", project="p", dataset="d", options={"uri": "/data/x.lance"})
        assert external_base_for(spec) is None

    def test_an_unknown_kind_degrades_to_managed(self) -> None:
        """An older build's chunk can name a kind this process no longer registers.

        Managed is the conservative direction: it costs storage, where the opposite mistake — claiming
        a base the keys do not live under — is refused per unit AFTER the fetch has already been paid.
        """
        assert external_base_for(SourceSpec(kind="a-kind-that-was-never-registered", project="p", dataset="d")) is None


class TestTheOperatorGatesIt:
    """A source root is CLIENT-SUPPLIED, so an adapter's answer is an untrusted value."""

    def test_an_unapproved_base_degrades_to_managed(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """THE SSRF GUARD. `options.bucket` comes off the ingest request.

        Writing it into a manifest unchecked would make the cascade's own `read_blobs` a server-side
        read primitive for any URI a caller can name — cloud metadata, an internal host — which is
        exactly what `chart/values.yaml`'s `vending.externalBlobBases` comment describes and refuses.

        It degrades rather than raising, for the reason `medallion_stage_output_UNGOVERNED` does: an
        unapproved base is a DEPLOYMENT gap, and raising would turn a missing env var into a run no
        number of retries can complete. But it degrades LOUDLY — silence is this estate's failure mode.
        """
        monkeypatch.setenv("LANCE_EXTERNAL_BLOB_BASES", "s3://approved-corpus")
        with caplog.at_level("WARNING", logger="ingest.runtime"):
            assert approved_external_base("s3://someone-elses-bucket") is None
        assert any(r.message == "ingest_external_base_not_approved" for r in caplog.records), (
            f"an unapproved base was refused SILENTLY: {[r.message for r in caplog.records]}"
        )

    def test_an_approved_base_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LANCE_EXTERNAL_BLOB_BASES", "s3://approved-corpus,s3://second")
        assert approved_external_base("s3://approved-corpus") == "s3://approved-corpus"
        assert approved_external_base("s3://second") == "s3://second"

    def test_a_prefix_of_an_approved_base_passes_but_a_LOOKALIKE_does_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`s3://corpus/vol/A` is under `s3://corpus`. `s3://corpusx` is a different bucket.

        The separator is what separates them, and a naive `startswith` treats the second as approved —
        which hands an attacker every bucket whose name merely begins with an approved one.
        """
        monkeypatch.setenv("LANCE_EXTERNAL_BLOB_BASES", "s3://corpus")
        assert approved_external_base("s3://corpus/vol/A") == "s3://corpus/vol/A"
        assert approved_external_base("s3://corpusx/secret") is None

    def test_no_allowlist_approves_NOTHING(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty is the default, and it must mean "none", never "all"."""
        monkeypatch.delenv("LANCE_EXTERNAL_BLOB_BASES", raising=False)
        assert approved_external_base("s3://anything") is None


class TestBronzeStoresTheCorpusOnce:
    """The measurement the whole change exists for, run as a test rather than quoted as a number."""

    def test_external_bronze_does_not_copy_the_corpus_and_managed_does(self, tmp_path: Path) -> None:
        source = tmp_path / "corpus"
        units = _payloads(source, count=20)
        corpus_bytes = _dir_bytes(source)
        schema = units_to_table(units[:1], external_base=str(source)).schema

        external = str(tmp_path / "bronze_external.lance")
        create_empty(external, schema, external_base=str(source))
        lance.write_dataset(units_to_table(units, external_base=str(source)), external, mode="append")

        managed = str(tmp_path / "bronze_managed.lance")
        create_empty(managed, schema)
        lance.write_dataset(units_to_table(units), managed, mode="append")

        external_bytes = _dir_bytes(Path(external))
        managed_bytes = _dir_bytes(Path(managed))

        # The claim, stated as a ratio so it survives a change in fixture size.
        assert external_bytes < corpus_bytes * 0.05, f"external bronze cost {external_bytes:,} B against a {corpus_bytes:,} B corpus — it copied"
        assert managed_bytes > corpus_bytes * 0.9, f"managed bronze cost {managed_bytes:,} B — it was expected to hold the whole {corpus_bytes:,} B corpus"

        # BOTH must still hand back the bytes. A cheap dataset that cannot read is not the win.
        assert _resolves(external) == 20
        assert _resolves(managed) == 20

    def test_a_dataset_created_WITHOUT_a_base_refuses_an_external_descriptor(self, tmp_path: Path) -> None:
        """Why changes 1 and 2 are one change.

        `initial_bases` is create-mode only, so this dataset can never accept an external pointer —
        and the refusal is loud, at write, rather than a dangling pointer discovered later. That
        refusal is the reason `allow_external_blob_outside_bases` must stay False.
        """
        source = tmp_path / "corpus"
        units = _payloads(source, count=3)
        schema = units_to_table(units[:1], external_base=str(source)).schema

        no_base = str(tmp_path / "no_base.lance")
        create_empty(no_base, schema)  # the base is NOT registered

        with pytest.raises(OSError, match="outside registered external bases"):
            lance.write_dataset(units_to_table(units, external_base=str(source)), no_base, mode="append")

    def test_the_descriptor_names_the_source_and_carries_the_fixity_hash(self, tmp_path: Path) -> None:
        """Bronze stays faithful to source (§3.5) under either placement.

        The bytes are still FETCHED — validation and the `sha256` fixity column both read them — so
        External changes where the payload LIVES, not what bronze knows about it. Fetching to hash is
        not copying into the lakehouse.
        """
        source = tmp_path / "corpus"
        units = _payloads(source, count=4)
        table = units_to_table(units, external_base=str(source))

        assert table.column("source_uri").to_pylist() == [key for key, _ in units]
        assert all(len(h) == 64 for h in table.column("sha256").to_pylist()), "fixity was lost with the bytes"

        uri = str(tmp_path / "bronze.lance")
        create_empty(uri, table.schema, external_base=str(source))
        lance.write_dataset(table, uri, mode="append")

        descriptor: dict[str, Any] = lance.dataset(uri).to_table(columns=["payload"]).column("payload")[0].as_py()
        # kind=3 is EXTERNAL. `blob_uri` is BASE-RELATIVE, which is the mechanic that makes a
        # carry-forward a real mapping rather than a copy of the struct — see change 3.
        assert descriptor["kind"] == 3, f"expected an EXTERNAL descriptor, got kind={descriptor['kind']}"
        assert descriptor["blob_uri"] == "page-000.bin", f"expected a base-relative uri, got {descriptor['blob_uri']!r}"
