"""The governed HTR lane (#88 step 4) — real Lance in, real Lance out, respx at the ONE seam.

The bronze fixture is the ingest plane's real page shape (`{id, source_uri, payload(blob), stage}`,
blob-v2 at 2.2, stable row ids), the transcribe boundary is respx returning the parser suite's
realistic ALTO, and the assertions read the gold table BACK with lance — the write flags, the
contract coverage and the row-level provenance are all checked against what landed on disk, not
against the table object that was about to be written.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import lance
import pyarrow as pa
import pytest
import respx
from lance import blob_array, blob_field
from medallion.schemas.htr import GOLD_CONTRACT_COLUMNS
from medallion.services.htr_stage import transcribe_stage
from medallion.services.htr_transcribe import TranscribeError


SERVE = "http://serve.test/htrflow"

NS = 'xmlns="http://www.loc.gov/standards/alto/ns-v4#"'


def _alto_for(name: str, text: str) -> str:
    return f"""<alto {NS}><Description>
      <sourceImageInformation><fileName>{name}</fileName></sourceImageInformation>
      <Processing ID="model_text-recognition"><processingStepSettings>model=org/m@v1</processingStepSettings></Processing>
      <Processing ID="build"><processingStepSettings>commit=cafe01</processingStepSettings></Processing>
    </Description><Layout><Page WIDTH="100" HEIGHT="200"><PrintSpace>
      <TextBlock ID="b" HPOS="0" VPOS="0" HEIGHT="10" WIDTH="10">
        <TextLine ID="b_l0" HPOS="0" VPOS="0" HEIGHT="5" WIDTH="10"><String CONTENT="{text}" WC="0.9" /></TextLine>
      </TextBlock></PrintSpace></Page></Layout></alto>"""


def _bronze_pages(tmp_path: Path, uris: list[str]) -> str:
    """The ingest plane's real bronze page shape, written the way the lander writes it."""
    uri = str(tmp_path / "bronze_pages")
    table = pa.table(
        {
            "id": pa.array(range(len(uris)), pa.int64()),
            "source_uri": pa.array(uris, pa.string()),
            "payload": blob_array([f"IMG-{u}".encode() for u in uris]),
            "stage": pa.array(["bronze"] * len(uris), pa.string()),
        },
        schema=pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), blob_field("payload"), pa.field("stage", pa.string())]),
    )
    lance.write_dataset(table, uri, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
    return uri


@respx.mock
def test_bronze_pages_become_a_contract_shaped_gold_table(tmp_path: Path) -> None:
    bronze = _bronze_pages(tmp_path, ["iiif://volA/0001.jpg", "iiif://volA/0002.jpg"])
    gold = str(tmp_path / "gold_htr")
    route = respx.post(f"{SERVE}/").mock(
        side_effect=lambda request: httpx.Response(200, text=_alto_for(request.url.params["name"], f"rad {request.url.params['name']}"))
    )

    result = transcribe_stage(bronze, gold, {}, stage="gold-htr", htrflow_url=SERVE)

    ds = lance.dataset(gold)
    # THE GOAL'S SENTENCE, verified on disk: every gold-contract column except the optional
    # lineage stamp (None passed here; same shared helper as the generic stage) is IN the table.
    missing = set(GOLD_CONTRACT_COLUMNS) - set(ds.schema.names) - {"lineage"}
    assert missing == set(), f"gold table missing contract columns: {missing}"

    got = ds.to_table()
    assert got.column("volume_id").to_pylist() == ["iiif://volA", "iiif://volA"]
    assert got.column("page_key").to_pylist() == ["0001.jpg", "0002.jpg"]
    assert got.column("text").to_pylist() == [["rad 0001.jpg"], ["rad 0002.jpg"]]
    assert got.column("stage").to_pylist() == ["gold-htr", "gold-htr"]
    assert (result.version, result.row_count) == (ds.version, 2)
    assert result.column_map, "no columnLineage edges declared"
    # Each page went to Serve exactly once, as raw bytes, self-identified by its leaf key.
    assert route.call_count == 2
    assert route.calls[0].request.content == b"IMG-iiif://volA/0001.jpg"


@respx.mock
def test_source_rowid_roots_each_gold_row_at_its_bronze_row(tmp_path: Path) -> None:
    """Row-level provenance is the contract's whole point: a transcription nobody can trace to its
    page bytes is #88's defect with extra steps."""
    bronze = _bronze_pages(tmp_path, ["iiif://v/1.jpg", "iiif://v/2.jpg", "iiif://v/3.jpg"])
    # DELETE the first row so surviving _rowids are [1, 2] — DIFFERENT from positions [0, 1].
    # Without this the fixture's _rowids equal positional indices and the assertion is VACUOUS: a
    # mutation substituting range(n) for _rowid passed it. Found by mutation, fixed by fixture.
    lance.dataset(bronze).delete("id = 0")
    gold = str(tmp_path / "gold_htr")
    respx.post(f"{SERVE}/").mock(side_effect=lambda request: httpx.Response(200, text=_alto_for(request.url.params["name"], "x")))

    transcribe_stage(bronze, gold, {}, stage="gold-htr", htrflow_url=SERVE)

    bronze_rowids = lance.dataset(bronze).to_table(with_row_id=True).column("_rowid").to_pylist()
    gold_rowids = lance.dataset(gold).to_table(columns=["source_rowid"]).column("source_rowid").to_pylist()
    assert gold_rowids == bronze_rowids
    assert gold_rowids != list(range(len(gold_rowids))), "fixture regression: _rowids equal positions again"


@respx.mock
def test_a_failing_page_writes_NO_gold_table(tmp_path: Path) -> None:
    """All-or-nothing per stage: a mid-batch Serve failure must not leave a half-written gold table
    that reads as complete. The mover's redelivery re-runs the WHOLE stage (overwrite-idempotent)."""
    bronze = _bronze_pages(tmp_path, ["iiif://v/ok.jpg", "iiif://v/boom.jpg"])
    gold = str(tmp_path / "gold_htr")

    def _answer(request: httpx.Request) -> httpx.Response:
        if request.url.params["name"] == "boom.jpg":
            return httpx.Response(500, text="replica died")
        return httpx.Response(200, text=_alto_for("ok.jpg", "x"))

    respx.post(f"{SERVE}/").mock(side_effect=_answer)

    with pytest.raises(TranscribeError):
        transcribe_stage(bronze, gold, {}, stage="gold-htr", htrflow_url=SERVE)

    assert not Path(gold).exists(), "a failed stage left a partial gold table on disk"


@respx.mock
def test_the_dispatch_constant_matches_the_charts_operation() -> None:
    """The chart routes by string; the code dispatches by constant. One fact, two homes — pinned."""
    from medallion.services.htr_stage import HTR_OPERATION

    chart = Path("chart/values.yaml").read_text()
    # Comma-terminated so a PREFIX of the real value cannot pass — "transcribe_page" is a substring
    # of "transcribe_pages", and the bare `in` check accepted exactly that mutation.
    assert f"operation: {HTR_OPERATION}," in chart, "the chart's mover entry and HTR_OPERATION disagree"
