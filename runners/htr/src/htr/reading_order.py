"""Reading-order computation for 2-page spreads + marginalia.

Ported from htrflow (`utils/layout.py` + `postprocess/reading_order.py`).
Given an image and a list of region bounding boxes, this module:
  1. Estimates the page's printspace (main-text area) from pixel intensity
  2. Detects whether the page is a two-page spread
  3. Returns a permutation that sorts regions into correct reading order:
     per-page → (top margin, printspace, bottom margin, left, right) → y-top

Each region dict is expected to have integer keys: 'x', 'y', 'w', 'h'.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np
from PIL import Image


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrintspaceBbox:
    xmin: int
    ymin: int
    xmax: int
    ymax: int

    @property
    def center_x(self) -> float:
        return (self.xmin + self.xmax) / 2


class RegionLocation(Enum):
    MARGIN_TOP = 0
    PRINTSPACE = 1
    MARGIN_BOTTOM = 2
    MARGIN_LEFT = 3
    MARGIN_RIGHT = 4


def estimate_printspace(image: Image.Image, window: int = 150) -> PrintspaceBbox:
    """Estimate printspace bbox of a page from pixel intensities."""
    arr = np.asarray(image)
    if arr.ndim > 2:
        arr = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    _, arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, arr, *_ = cv2.floodFill(arr, None, (0, 0), (255, 255, 255))

    bbox = [0, 0, 0, 0]
    h, w = arr.shape[:2]
    for axis in (0, 1):
        levels = arr.sum(axis=axis).astype(np.float64)
        max_level = np.max(levels)
        if max_level == 0:
            i, j = 0, (w if axis == 0 else h)
            bbox[axis] = i
            bbox[axis + 2] = j
            continue
        levels = levels / max_level
        levels_sorted = np.sort(levels)
        a = 0.1
        mids = levels_sorted[int(len(levels) * a) : int((1 - a) * len(levels))]
        gray = float(np.mean(mids)) if len(mids) else 0.5

        i: int | None = None
        j: int | None = None
        for ii in range(window, len(levels) - window):
            if np.median(levels[ii - window : ii]) > gray > np.median(levels[ii : ii + window]):
                i = ii
                break
        for jj in range(len(levels) - window, window, -1):
            if np.median(levels[jj - window : jj]) < gray < np.median(levels[jj : jj + window]):
                j = jj
                break

        if i is None or j is None or i > j:
            i = 0
            j = arr.shape[1 - axis]
            logger.debug("Could not find printspace along axis %d", axis)

        bbox[axis] = int(i)
        bbox[axis + 2] = int(j)

    return PrintspaceBbox(xmin=bbox[0], ymin=bbox[1], xmax=bbox[2], ymax=bbox[3])


def is_twopage(image: Image.Image, strip_width: float = 0.1, threshold: float = 0.2) -> int | None:
    """Detect dark vertical divider down the middle. Returns its x-coordinate if present."""
    arr = np.asarray(image)
    if arr.ndim == 3:
        arr = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    w = arr.shape[1]
    middle = int(w / 2)
    half_strip = int(strip_width * w / 2)
    if half_strip == 0:
        return None
    levels = arr.sum(axis=0)
    strip = levels[middle - half_strip : middle + half_strip]
    if np.min(strip) < np.sort(levels)[int(w * threshold)]:
        return int(middle - half_strip + np.argmin(strip))
    return None


def _region_bbox(region: dict) -> tuple[int, int, int, int]:
    """Extract (xmin, ymin, xmax, ymax) from a region dict with 'x','y','w','h'."""
    x = int(region["x"])
    y = int(region["y"])
    return x, y, x + int(region["w"]), y + int(region["h"])


def _get_region_location(ps: PrintspaceBbox, region: dict) -> RegionLocation:
    xmin, ymin, xmax, ymax = _region_bbox(region)
    # Intersection area with printspace
    ix_min = max(xmin, ps.xmin)
    iy_min = max(ymin, ps.ymin)
    ix_max = min(xmax, ps.xmax)
    iy_max = min(ymax, ps.ymax)
    if ix_min < ix_max and iy_min < iy_max:
        overlap_area = (ix_max - ix_min) * (iy_max - iy_min)
        region_area = max(0, (xmax - xmin) * (ymax - ymin))
        if region_area > 0 and overlap_area / region_area > 0.5:
            return RegionLocation.PRINTSPACE
    if xmax >= ps.xmax:
        return RegionLocation.MARGIN_RIGHT
    if xmin <= ps.xmin:
        return RegionLocation.MARGIN_LEFT
    if ymin <= ps.ymin:
        return RegionLocation.MARGIN_TOP
    if ymax >= ps.ymax:
        return RegionLocation.MARGIN_BOTTOM
    return RegionLocation.PRINTSPACE


def order_regions(regions: list[dict], ps: PrintspaceBbox, two_page: bool) -> list[int]:
    """Return an index permutation that sorts regions into reading order.

    Sort key per region:
      (page: left=0/right=1 if two_page else 0,
       location: top=0, printspace=1, bottom=2, left=3, right=4,
       y-top)
    """

    def key(i: int) -> tuple[int, int, int]:
        region = regions[i]
        xmin, ymin, xmax, _ = _region_bbox(region)
        center_x = (xmin + xmax) / 2
        page = int(two_page and center_x > ps.center_x)
        location = _get_region_location(ps, region).value
        return (page, location, ymin)

    return sorted(range(len(regions)), key=key)


def sort_page_in_reading_order(
    image: Image.Image,
    page_lines: list[dict],
    two_page: bool | str = "auto",
) -> list[dict]:
    """Reorder page_lines (list of {"region", "lines"} dicts) into reading order.

    Args:
        image: The full page as PIL RGB.
        page_lines: Output of line detector (one item per detected region).
        two_page: True / False / "auto" (detect via is_twopage).

    Returns:
        The same list of dicts, re-sorted.
    """
    if not page_lines:
        return page_lines
    ps = estimate_printspace(image)
    two_page_flag = is_twopage(image) is not None if two_page == "auto" else bool(two_page)
    regions = [g["region"] for g in page_lines]
    order = order_regions(regions, ps, two_page_flag)
    return [page_lines[i] for i in order]
