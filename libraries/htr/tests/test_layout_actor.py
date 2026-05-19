import io

import numpy as np
import pytest
from PIL import Image


@pytest.mark.slow
def test_layout_actor_smoke():
    """Loads the real YOLO model — slow. Skipped unless `-m slow` is passed."""
    from htr.actors.layout import LayoutActor

    img = Image.new("RGB", (640, 480), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    actor = LayoutActor()
    batch = {
        "key": np.array(["test.jpg"], dtype=object),
        "image_bytes": np.array([img_bytes], dtype=object),
    }
    out = actor(batch)
    assert "regions" in out
    assert len(out["regions"]) == 1
    assert isinstance(out["regions"][0], list)


def test_layout_actor_signature():
    """Cheap test: actor exists, has the right shape (no model load)."""
    from htr.actors.layout import LayoutActor

    assert callable(LayoutActor)
    actor = LayoutActor.__new__(LayoutActor)  # bypass __init__
    actor.model_name = "Riksarkivet/yolov9-regions-1"
    assert "model" in LayoutActor.__init__.__code__.co_varnames
