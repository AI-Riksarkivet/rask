"""ALTO → gold payload (#88 step 2) — and the moment GOLD_CONTRACT_COLUMNS becomes load-bearing.

The fixtures are built from the REAL templates' shapes — the /htrflow lane's ``block<i>_line<j>``
layout (word- and line-level Strings, Shape polygons, ReadingOrder groups, htrflow's repr-style
step settings) and the actor lane's flat model/build Processing blocks — not from an idealized
ALTO. A parser green against a tidied fixture and wrong against its own serializer would be the
usual way this fails.
"""

from __future__ import annotations

import pytest
from medallion.services.htr_parse import PARSED_COLUMNS, parse_alto


NS = 'xmlns="http://www.loc.gov/standards/alto/ns-v4#"'

#: The /htrflow lane's shape: two blocks, three lines; line 0 word-split (two Strings), the others
#: line-level; block 1 carries a Shape polygon; ReadingOrder puts block 1 first.
HTRFLOW_ALTO = f"""<?xml version="1.0" encoding="UTF-8"?>
<alto {NS}>
  <Description>
    <MeasurementUnit>pixel</MeasurementUnit>
    <sourceImageInformation><fileName>00012.jpg</fileName></sourceImageInformation>
    <Processing ID="processing">
      <processingDateTime>2026-08-04T20:00:00</processingDateTime>
      <processingStepDescription>Segmentation(model=yolo, model_settings={{'model': 'Riksarkivet/yolov9-regions-1', 'revision': 'abc123'}})</processingStepDescription>
      <processingStepDescription>TextRecognition(model=trocr, model_settings={{'model': 'Riksarkivet/trocr-base-handwritten-hist-swe-2', 'revision': 'abc123'}}, generation_settings={{'batch_size': 8}})</processingStepDescription>
      <processingSoftware><softwareName>htrflow</softwareName></processingSoftware>
    </Processing>
    <Processing ID="build"><processingStepDescription>runner-build</processingStepDescription><processingStepSettings>commit=deadbeef</processingStepSettings></Processing>
  </Description>
  <ReadingOrder>
    <OrderedGroup ID="ro">
      <OrderedGroup ID="ro_block1">
        <ElementRef ID="a" REF="block1_line0" />
      </OrderedGroup>
      <OrderedGroup ID="ro_block0">
        <ElementRef ID="b" REF="block0_line0" />
        <ElementRef ID="c" REF="block0_line1" />
      </OrderedGroup>
    </OrderedGroup>
  </ReadingOrder>
  <Layout>
    <Page WIDTH="2400" HEIGHT="3200" PHYSICAL_IMG_NR="0" ID="_00012.jpg" PC="0.9000">
      <PrintSpace>
        <TextBlock ID="block_0" HPOS="100" VPOS="200" HEIGHT="400" WIDTH="800" CS="false">
          <TextLine ID="block0_line0" HPOS="110" VPOS="210" HEIGHT="60" WIDTH="700">
            <String ID="w0" HPOS="110" VPOS="210" HEIGHT="60" WIDTH="300" CONTENT="Anno" WC="0.9000" />
            <String ID="w1" HPOS="420" VPOS="210" HEIGHT="60" WIDTH="300" CONTENT="1734" WC="0.7000" />
          </TextLine>
          <TextLine ID="block0_line1" HPOS="110" VPOS="300" HEIGHT="60" WIDTH="700">
            <String CONTENT="den 12 Maji" WC="0.8500" />
          </TextLine>
        </TextBlock>
        <TextBlock ID="block_1" HPOS="100" VPOS="700" HEIGHT="200" WIDTH="800" CS="false">
          <Shape><Polygon POINTS="100,700 900,700 850,900 100,880"/></Shape>
          <TextLine ID="block1_line0" HPOS="110" VPOS="710" HEIGHT="60" WIDTH="700">
            <Shape><Polygon POINTS="110,710 810,712 808,770 110,768"/></Shape>
            <String CONTENT="Sockenstämma" WC="0.9200" />
          </TextLine>
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>"""


def test_the_htrflow_lane_document_parses_to_the_contract_shape() -> None:
    page = parse_alto(HTRFLOW_ALTO)

    assert page.page_key == "00012.jpg"
    assert (page.page_width, page.page_height) == (2400, 3200)
    assert page.text == ["Anno 1734", "den 12 Maji", "Sockenstämma"]
    assert page.confidences == [pytest.approx(0.8), pytest.approx(0.85), pytest.approx(0.92)]
    # Shape polygon where present, bbox corners where not — the serializer's own convention.
    assert page.region_polygons[1] == "100,700 900,700 850,900 100,880"
    assert page.region_polygons[0] == "100,200 900,200 900,600 100,600"
    assert page.line_polygons[2] == "110,710 810,712 808,770 110,768"


def test_reading_order_comes_from_the_document_not_layout_order() -> None:
    """The template writes ReadingOrder as its own tree; a parser that ignored it would silently
    reorder every multi-column page into column-interleaved nonsense."""
    page = parse_alto(HTRFLOW_ALTO)

    assert page.reading_order == [2, 0, 1]  # block1_line0 first, per the OrderedGroup


def test_provenance_is_read_from_the_document_itself() -> None:
    """Models AND build commit come from the run's artefact — a config could lie, this cannot."""
    page = parse_alto(HTRFLOW_ALTO)

    assert page.models == [
        "Riksarkivet/yolov9-regions-1@abc123",
        "Riksarkivet/trocr-base-handwritten-hist-swe-2@abc123",
    ]
    assert page.commit_sha == "deadbeef"


def test_the_actor_lane_blocks_parse_too() -> None:
    """The other serializer we own: flat model=repo@rev / commit= Processing blocks (#89/#88)."""
    xml = f"""<alto {NS}><Description>
      <sourceImageInformation><fileName>p.jpg</fileName></sourceImageInformation>
      <Processing ID="model_text-recognition"><processingStepSettings>model=org/m@v1</processingStepSettings></Processing>
      <Processing ID="build"><processingStepSettings>commit=cafe01</processingStepSettings></Processing>
    </Description><Layout><Page WIDTH="10" HEIGHT="10"/></Layout></alto>"""

    page = parse_alto(xml)

    assert page.models == ["org/m@v1"]
    assert page.commit_sha == "cafe01"


def test_absent_provenance_parses_as_absent() -> None:
    """Silence stays honest on the way back in: no blocks → no models, no sha — never a guess."""
    xml = f'<alto {NS}><Layout><Page WIDTH="10" HEIGHT="10"/></Layout></alto>'

    page = parse_alto(xml)

    assert page.models == []
    assert page.commit_sha is None
    assert page.reading_order == []


def test_an_empty_line_keeps_its_row() -> None:
    """Cardinality is the invariant (the blob-v2 lesson): a line that transcribed to nothing keeps
    its slot in text/confidences/line_polygons, or every later line misattributes."""
    xml = f"""<alto {NS}><Layout><Page WIDTH="10" HEIGHT="10"><PrintSpace>
      <TextBlock ID="b" HPOS="0" VPOS="0" HEIGHT="10" WIDTH="10">
        <TextLine ID="l0" HPOS="0" VPOS="0" HEIGHT="5" WIDTH="10"><String CONTENT="" /></TextLine>
        <TextLine ID="l1" HPOS="0" VPOS="5" HEIGHT="5" WIDTH="10"><String CONTENT="x" WC="0.5" /></TextLine>
      </TextBlock></PrintSpace></Page></Layout></alto>"""

    page = parse_alto(xml)

    assert page.text == ["", "x"]
    assert page.confidences == [0.0, 0.5]
    assert len(page.line_polygons) == 2


def test_a_non_alto_document_is_refused_not_half_parsed() -> None:
    with pytest.raises(ValueError, match="not parseable"):
        parse_alto("this is not xml")
    with pytest.raises(ValueError, match="no <Page>"):
        parse_alto(f"<alto {NS}><Description/></alto>")


def test_the_contract_is_covered_at_import_time() -> None:
    """PARSED + caller-supplied == GOLD_CONTRACT_COLUMNS, checked at IMPORT in the module — this
    test only witnesses that the guard exists by re-deriving it."""
    from medallion.schemas.htr import GOLD_CONTRACT_COLUMNS

    assert set(PARSED_COLUMNS) | {"volume_id", "source_rowid", "lineage"} == set(GOLD_CONTRACT_COLUMNS)
