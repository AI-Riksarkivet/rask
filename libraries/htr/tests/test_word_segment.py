def test_geometric_word_segmentation_splits_by_proportion():
    from htr.alto.word_segment import segment_words
    from htr.schemas import Line

    line = Line(x=0, y=0, w=300, h=40, confidence=0.9, abs_x=10, abs_y=20)
    words = segment_words(line, text="hello world ok", confidence=0.9)
    assert len(words) == 3
    assert [w.text for w in words] == ["hello", "world", "ok"]
    # Words should sum to roughly the line width.
    assert sum(w.w for w in words) <= line.w


def test_empty_text_produces_no_words():
    from htr.alto.word_segment import segment_words
    from htr.schemas import Line

    line = Line(x=0, y=0, w=300, h=40, confidence=0.9, abs_x=10, abs_y=20)
    words = segment_words(line, text="", confidence=0.9)
    assert words == []


def test_single_word_uses_full_line_width():
    from htr.alto.word_segment import segment_words
    from htr.schemas import Line

    line = Line(x=0, y=0, w=200, h=30, confidence=0.9, abs_x=5, abs_y=5)
    words = segment_words(line, text="hello", confidence=0.9)
    assert len(words) == 1
    assert words[0].text == "hello"
    assert words[0].w == 200
    assert words[0].x == 5
