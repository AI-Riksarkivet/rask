import io
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image


def test_line_actor_signature():
    from htr.actors.lines import LineActor

    actor = LineActor.__new__(LineActor)
    actor.model_name = "Riksarkivet/yolov9-lines-within-regions-1"
    assert "model" in LineActor.__init__.__code__.co_varnames


@pytest.fixture
def small_jpeg() -> bytes:
    img = Image.new("RGB", (200, 100), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_line_actor_constructs_lines_with_all_required_fields(small_jpeg):
    """Smoke test that catches Line dataclass field mismatches."""
    from htr._columns import pack, unpack
    from htr.actors.lines import LineActor
    from htr.schemas import Region

    actor = LineActor()

    # Fake YOLO output: one bounding box at (5, 10) with size 50x20 within the crop.
    fake_box = MagicMock()
    fake_box.xyxy = [MagicMock()]
    fake_box.xyxy[0].cpu().numpy.return_value.tolist.return_value = [5.0, 10.0, 55.0, 30.0]
    fake_box.conf = [MagicMock(__float__=lambda _self: 0.9)]
    fake_result = MagicMock()
    fake_result.boxes = [fake_box]

    fake_yolo = MagicMock(return_value=[fake_result])
    actor._model = fake_yolo  # bypass _ensure_model

    # small_jpeg is 200x100, so define a region that fits within bounds
    region = Region(x=10.0, y=20.0, w=100.0, h=60.0, confidence=0.95)
    batch = {
        "image_bytes": np.array([small_jpeg], dtype=object),
        "regions": np.array([pack([region])], dtype=object),
    }

    out = actor(batch)
    assert "lines" in out
    assert len(out["lines"]) == 1
    page_lines = unpack(out["lines"][0])
    assert len(page_lines) == 1
    line = page_lines[0]
    # Region-relative coords
    assert line.x == 5.0
    assert line.y == 10.0
    assert line.w == 50.0
    assert line.h == 20.0
    # Page-absolute coords (region top-left was 10, 20)
    assert line.abs_x == 15.0
    assert line.abs_y == 30.0
