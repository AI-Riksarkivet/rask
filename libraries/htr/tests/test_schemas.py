def test_page_image_holds_bytes_and_key():
    from htr.schemas import PageImage

    p = PageImage(key="vol/page1.jpg", image_bytes=b"jpg-bytes")
    assert p.key == "vol/page1.jpg"
    assert p.image_bytes == b"jpg-bytes"
    assert p.metadata == {}


def test_region_construction():
    from htr.schemas import Region

    r = Region(x=10.0, y=20.0, w=300.0, h=50.0, confidence=0.95, label="text")
    assert r.x == 10.0
    assert r.label == "text"
    assert r.polygon is None


def test_line_construction():
    from htr.schemas import Line

    line = Line(x=0, y=0, w=100, h=20, confidence=0.9, abs_x=10, abs_y=20)
    assert line.abs_x == 10
    assert line.abs_polygon is None


def test_word_construction():
    from htr.schemas import Word

    w = Word(x=10, y=20, w=30, h=15, text="hej", confidence=0.92)
    assert w.text == "hej"


def test_transcribed_line_default_words_empty():
    from htr.schemas import Line, TranscribedLine

    line = Line(x=0, y=0, w=100, h=20, confidence=0.9, abs_x=0, abs_y=0)
    tl = TranscribedLine(line=line, text="hello", confidence=0.91)
    assert tl.words == []
    assert tl.text == "hello"


def test_page_with_regions_holds_page_and_regions():
    from htr.schemas import PageImage, PageWithRegions, Region

    page = PageImage(key="x", image_bytes=b"")
    regions = [Region(x=0, y=0, w=10, h=10, confidence=1.0)]
    p = PageWithRegions(page=page, regions=regions)
    assert p.page.key == "x"
    assert len(p.regions) == 1


def test_page_with_lines_groups_lines_by_region():
    from htr.schemas import Line, PageImage, PageWithLines, Region

    page = PageImage(key="x", image_bytes=b"")
    region = Region(x=0, y=0, w=100, h=200, confidence=1.0)
    lines = [Line(x=0, y=0, w=100, h=20, confidence=0.9, abs_x=0, abs_y=0)]
    p = PageWithLines(page=page, region_lines=[(region, lines)])
    assert len(p.region_lines) == 1


def test_page_with_text_holds_transcribed_lines_per_region():
    from htr.schemas import Line, PageImage, PageWithText, Region, TranscribedLine

    page = PageImage(key="x", image_bytes=b"")
    region = Region(x=0, y=0, w=100, h=200, confidence=1.0)
    line = Line(x=0, y=0, w=100, h=20, confidence=0.9, abs_x=0, abs_y=0)
    tl = TranscribedLine(line=line, text="hej", confidence=0.95)
    p = PageWithText(page=page, region_lines=[(region, [tl])])
    assert p.region_lines[0][1][0].text == "hej"
