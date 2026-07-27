"""Geometric word segmentation — proportional split of a line bbox by character count.

This matches htrflow's `simple_word_segmentation` approach: no model, no attention —
just allocate horizontal width proportional to each word's character count plus a
single space between words.
"""

from htr.schemas import Line, Word


def segment_words(line: Line, text: str, confidence: float) -> list[Word]:
    """Split a line into word-level Word objects by proportional horizontal slicing.

    Returns [] when `text` is empty. The y/h are inherited from the line; x/w are
    computed from the text's character distribution.
    """
    text = text.strip()
    if not text:
        return []

    words = text.split()
    if len(words) == 1:
        return [
            Word(
                x=line.abs_x,
                y=line.abs_y,
                w=line.w,
                h=line.h,
                text=words[0],
                confidence=confidence,
            )
        ]

    char_counts = [len(w) for w in words]
    total = sum(char_counts) + len(words) - 1  # one space between consecutive words
    space_w = line.w / total if total else 0.0

    out: list[Word] = []
    cursor = line.abs_x
    for i, (w_text, count) in enumerate(zip(words, char_counts, strict=True)):
        word_width = count * space_w
        out.append(
            Word(
                x=cursor,
                y=line.abs_y,
                w=word_width,
                h=line.h,
                text=w_text,
                confidence=confidence,
            )
        )
        cursor += word_width
        if i < len(words) - 1:
            cursor += space_w  # advance past the inter-word space
    return out
