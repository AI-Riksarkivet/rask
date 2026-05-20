"""HTR domain schemas — flow types between pipeline stages."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PageImage:
    """Source page — image bytes plus the source identifier (S3 key, file path, etc.)."""

    key: str
    image_bytes: bytes
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Region:
    """Text region (block) — output of layout detection."""

    x: float
    y: float
    w: float
    h: float
    confidence: float
    label: str | None = None
    polygon: list[tuple[float, float]] | None = None


@dataclass(frozen=True)
class Line:
    """Text line within a region — output of line detection.

    `x, y, w, h` are coordinates relative to the region crop;
    `abs_x, abs_y` are absolute coordinates on the page.
    """

    x: float
    y: float
    w: float
    h: float
    confidence: float
    abs_x: float
    abs_y: float
    abs_polygon: list[tuple[float, float]] | None = None


@dataclass(frozen=True)
class Word:
    """A word within a transcribed line — output of word-level segmentation."""

    x: float  # absolute on page
    y: float
    w: float
    h: float
    text: str
    confidence: float


@dataclass(frozen=True)
class TranscribedLine:
    """A line plus its recognized text. `words` is empty when word-level emission is off."""

    line: Line
    text: str
    confidence: float
    words: list[Word] = field(default_factory=list)


@dataclass(frozen=True)
class PageWithRegions:
    """Page plus its detected regions — output of layout stage."""

    page: PageImage
    regions: list[Region]


@dataclass(frozen=True)
class PageWithLines:
    """Page plus lines grouped by region — output of line-segmentation stage."""

    page: PageImage
    region_lines: list[tuple[Region, list[Line]]]


@dataclass(frozen=True)
class PageWithText:
    """Page plus transcribed lines grouped by region — output of TextRecognition stage."""

    page: PageImage
    region_lines: list[tuple[Region, list[TranscribedLine]]]
